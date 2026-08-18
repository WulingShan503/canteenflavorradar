"""偏好解析：自然语言 → :class:`UserPreference`。

主路径交给大模型，让它输出 JSON 再用 Pydantic 校验；
模型不可用、输出不是合法 JSON、或校验不过时降级到关键词规则。

**这一层的输出只是「用户说了什么」，不是「可以吃什么」。**
解析出来的过敏原和忌口必须再经过 :class:`DishRepository` 的规则过滤，
模型说「我对花生过敏」只是把条件记下来，真正把含花生的菜挡住是规则层的事。
"""

from __future__ import annotations

import json
import logging
import re

from pydantic import ValidationError

from app.agent import keyword_rules as kr
from app.models.enums import Flavor
from app.models.preference import UserPreference
from app.services.qianfan_client import QianfanClient, QianfanError

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是高校食堂选餐助手的偏好解析模块。
把用户的自然语言用餐需求转成 JSON，只输出 JSON，不要任何解释或代码块标记。

字段（全部可选，用户没提到的字段直接省略，不要编造）：
- liked_flavors: 数组，取值仅限 ["辣","麻","酸","甜","咸","鲜","清淡","油腻","蒜香","酱香"]
- disliked_flavors: 同上取值，用户明确不想要的口味
- spicy_tolerance: 整数 0-5，0 表示完全不吃辣，5 表示越辣越好
- liked_cuisines: 数组，取值仅限 ["川菜","湘菜","粤菜","鲁菜","淮扬菜","东北菜","西北菜","家常菜","西式","日韩","快餐"]
- categories: 数组，取值仅限 ["主食","荤菜","素菜","汤品","小吃","饮品","套餐"]
- budget_min / budget_max: 数字，单位元
- dietary_tags: 数组，取值仅限 ["素食","纯素","清真","低脂","低碳水","高蛋白","低糖"]
- avoid_allergens: 数组，取值仅限 ["花生","坚果","海鲜","贝类","蛋类","乳制品","大豆","麸质"]
  仅在用户明确表示过敏或不能吃时才填，说「想吃虾」不算
- disliked_ingredients: 字符串数组，忌口食材，如 ["香菜"]
- calorie_limit: 数字，单餐热量上限，千卡
- min_protein: 数字，单餐蛋白质下限，克
- meal_period: 字符串，取值仅限 ["早餐","午餐","晚餐","夜宵"]
- preferred_canteens: 字符串数组，如 ["二食堂"]
- max_wait_minutes: 整数，能接受的最长排队分钟数

注意：
- 枚举取值必须严格用上面给的中文词，不要自创或翻译成英文。
- 「减脂」「减肥」对应 dietary_tags 里的 "低脂"；「增肌」「健身」对应 "高蛋白"。
- 「便宜点」这类模糊表述折算成 budget_max，不要留空。
- 拿不准的字段就省略，宁缺勿造。

示例
输入：想吃点辣的，别太贵，最近在减脂，不要香菜
输出：{"liked_flavors":["辣"],"budget_max":12,"dietary_tags":["低脂"],"disliked_ingredients":["香菜"]}"""

USER_TEMPLATE = "用户需求：{text}\n输出 JSON："

# 模型有时会套 ```json ``` 或在 JSON 前后加话，捞出最外层大括号即可
JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)

# 只保留 UserPreference 认识的字段，模型多给的一律丢掉，
# 免得 Pydantic 因为未知字段直接报错。
ALLOWED_FIELDS = frozenset(UserPreference.model_fields.keys())


class PreferenceParser:
    """偏好解析器。

    Args:
        client: 千帆客户端。传 None 表示不用模型，直接走关键词规则。
    """

    def __init__(self, client: QianfanClient | None = None):
        self._client = client

    async def parse(self, text: str) -> tuple[UserPreference, bool]:
        """解析自然语言偏好。

        Returns:
            (偏好对象, 是否用了规则降级)。降级标记要一路传到
            ``RecommendResponse.fallback_used``，让用户知道解析可能不够准。
        """
        text = (text or "").strip()
        if not text:
            return UserPreference(), False

        if self._client is not None and self._client.available:
            try:
                pref = await self._parse_by_model(text)
            except (QianfanError, ValueError, ValidationError) as exc:
                # 模型不可用或输出没法用，降级但不让请求失败
                logger.warning("偏好解析降级到关键词规则: %s", exc)
            else:
                return pref, False

        return parse_by_keywords(text), True

    async def _parse_by_model(self, text: str) -> UserPreference:
        assert self._client is not None
        raw = await self._client.chat(
            USER_TEMPLATE.format(text=text),
            system=SYSTEM_PROMPT,
            # 解析要的是确定性，温度压到最低，别让它发挥
            temperature=0.01,
        )
        data = extract_json(raw)
        return build_preference(data, raw_text=text)


def extract_json(raw: str) -> dict:
    """从模型输出里捞出 JSON 对象。

    模型时不时会套代码块或者在前面加一句「好的，解析结果如下」，
    直接 json.loads 会炸，所以先用正则截出最外层大括号。
    """
    match = JSON_BLOCK.search(raw or "")
    if not match:
        raise ValueError(f"模型输出里找不到 JSON: {raw[:200]!r}")
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise ValueError(f"模型输出的 JSON 不合法: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"模型输出的 JSON 不是对象: {data!r}")
    return data


def build_preference(data: dict, raw_text: str = "") -> UserPreference:
    """把模型给的 dict 转成 UserPreference。

    做两件清洗：丢掉不认识的字段、丢掉枚举取值非法的项。
    模型偶尔会把「辣」写成「spicy」或自创「香辣」这种不在词表里的值，
    整条请求不该因为一个词就失败——能救的部分先救下来。
    """
    cleaned: dict = {}
    for key, value in data.items():
        if key not in ALLOWED_FIELDS or value is None:
            continue
        cleaned[key] = value

    cleaned["raw_text"] = raw_text

    try:
        return UserPreference(**cleaned)
    except ValidationError:
        # 逐字段重试，把校验不过的字段剔掉再来一次
        salvaged = {"raw_text": raw_text}
        for key, value in cleaned.items():
            if key == "raw_text":
                continue
            try:
                UserPreference(**{**salvaged, key: value})
            except ValidationError:
                logger.debug("丢弃模型给出的非法字段 %s=%r", key, value)
                continue
            salvaged[key] = value
        return UserPreference(**salvaged)


# ---------- 关键词规则降级路径 ----------


def parse_by_keywords(text: str) -> UserPreference:
    """纯规则解析，千帆不可用时的降级路径。

    覆盖不了所有说法，但食堂选餐的常见表达（辣/清淡/便宜/减脂/忌口/食堂/餐段）
    都能兜住。刻意保守：拿不准就不填，宁可推荐不够精准，
    也不要凭猜测给用户加上一个他没提的条件。
    """
    text = (text or "").strip()
    if not text:
        return UserPreference()

    liked_flavors: list[Flavor] = []
    disliked_flavors: list[Flavor] = []
    for flavor, words in kr.FLAVOR_WORDS.items():
        for word in words:
            if not kr.word_hit(text, word):
                continue
            if kr.find_negation_window(text, word):
                if flavor not in disliked_flavors:
                    disliked_flavors.append(flavor)
            elif flavor not in liked_flavors:
                liked_flavors.append(flavor)
            break

    data: dict = {
        "liked_flavors": liked_flavors,
        "disliked_flavors": disliked_flavors,
        "liked_cuisines": _collect(text, kr.CUISINE_WORDS),
        "categories": _collect(text, kr.CATEGORY_WORDS),
        "dietary_tags": _collect(text, kr.DIETARY_WORDS),
        "avoid_allergens": _collect_allergens(text),
        "disliked_ingredients": _collect_dislikes(text),
        "raw_text": text,
    }

    spicy = _spicy_tolerance(text)
    if spicy is not None:
        data["spicy_tolerance"] = spicy

    budget_max = kr.match_first(text, kr.BUDGET_MAX_PATTERNS)
    if budget_max is None and any(w in text for w in kr.CHEAP_WORDS):
        budget_max = kr.CHEAP_BUDGET_MAX
    if budget_max is not None:
        data["budget_max"] = budget_max

    budget_min = kr.match_first(text, kr.BUDGET_MIN_PATTERNS)
    if budget_min is None and any(w in text for w in kr.TREAT_WORDS):
        budget_min = kr.TREAT_BUDGET_MIN
    # 下限比上限还高说明解析拧了，以上限为准把下限丢掉
    if budget_min is not None and (budget_max is None or budget_min <= budget_max):
        data["budget_min"] = budget_min

    calorie = kr.match_first(text, kr.CALORIE_PATTERNS)
    if calorie:
        data["calorie_limit"] = calorie

    protein = kr.match_first(text, kr.PROTEIN_PATTERNS)
    if protein:
        data["min_protein"] = protein

    wait = kr.match_first(text, kr.WAIT_PATTERNS)
    if wait:
        data["max_wait_minutes"] = int(wait)

    for period, words in kr.MEAL_PERIOD_WORDS.items():
        if any(w in text for w in words):
            data["meal_period"] = period
            break

    canteens = [f"{m.group(1)}食堂" for m in kr.CANTEEN_PATTERN.finditer(text)]
    if canteens:
        data["preferred_canteens"] = list(dict.fromkeys(canteens))

    return UserPreference(**data)


def _collect(text: str, table: dict) -> list:
    """词表命中收集，跳过被否定的项。"""
    found = []
    for key, words in table.items():
        for word in words:
            if kr.word_hit(text, word) and not kr.find_negation_window(text, word):
                if key not in found:
                    found.append(key)
                break
    return found


def _collect_allergens(text: str) -> list:
    """过敏原只在出现明确过敏标记时才认，且要求标记与过敏原在同一分句。

    「想吃虾」和「虾过敏」都含「虾」，光凭名词判断会把想吃的菜全滤掉。
    按分句判断是为了避免跨句误判：「海鲜过敏，想吃鸡蛋」里
    「鸡蛋」不该因为前半句有「过敏」就被当成过敏原。
    """
    found: list = []
    for clause in kr.split_clauses(text):
        if not any(marker in clause for marker in kr.ALLERGY_MARKERS):
            continue
        for allergen, words in kr.ALLERGEN_WORDS.items():
            if any(word in clause for word in words) and allergen not in found:
                found.append(allergen)
    return found


def _collect_dislikes(text: str) -> list[str]:
    """忌口食材：词表里的常见项，且前面带否定词。"""
    found: list[str] = []
    for item in kr.COMMON_DISLIKES:
        if item in text and kr.find_negation_window(text, item):
            if item not in found:
                found.append(item)
    return found


def _spicy_tolerance(text: str) -> int | None:
    """辣度容忍度。

    词表按「具体程度优先于泛化表述」的顺序匹配：
    「微辣」比「辣」精确，先命中就不再往下看。
    """
    for words, level in kr.SPICY_TOLERANCE_WORDS:
        if any(word in text for word in words):
            return level
    # 只说了「辣」但没说程度，不设上限——设了反而可能把特辣菜挡掉
    return None
