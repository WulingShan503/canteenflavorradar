"""推荐语生成：给排好序的菜写一句人话。

规则层的 ``reasons`` 已经把「为什么推荐」讲清楚了，但读起来像清单。
这一层让模型把它们串成一句自然的话。

关键做法：**把规则理由当事实依据喂进 prompt**，模型只负责组织语言，
不负责判断该不该推荐。这样既有语感又不容易编造——
模型看不到「这道菜没有的东西」，自然编不出来。

失败时直接用规则理由拼成 comment，用户体验降级但不缺内容。
"""

from __future__ import annotations

import json
import logging
import re

from app.models.recommendation import Recommendation
from app.services.qianfan_client import QianfanClient, QianfanError

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是高校食堂的选餐推荐助手，说话像个熟悉食堂的学长学姐。

我会给你若干道已经筛选并排好序的菜，以及每道菜「为什么被推荐」的事实依据。
请为每道菜写一句推荐语。

要求：
- 每句 20-45 字，一句话说完，不要分点。
- 只能使用我给出的事实依据和菜品信息，**不许编造**没提到的口味、食材、价格或营养数据。
- 语气自然亲切，别用「本店」「本菜品」这类生硬说法，也别夸张到「人间美味」。
- 如果用户有原话诉求，回应他的诉求，但不要复述他的话。
- 不要重复菜名开头，每句换个说法。

输出严格的 JSON 数组，每个元素形如 {"id":"菜品编号","comment":"推荐语"}，
数组顺序和我给的顺序一致，不要输出任何其他内容。"""

JSON_ARRAY = re.compile(r"\[.*\]", re.DOTALL)

MAX_COMMENT_CHARS = 80  # 模型偶尔会写长，超了就截断而不是整批丢弃


class CommentWriter:
    """推荐语生成器。

    Args:
        client: 千帆客户端。传 None 或客户端不可用时全部走规则理由兜底。
    """

    def __init__(self, client: QianfanClient | None = None):
        self._client = client

    async def write(
        self, recommendations: list[Recommendation], raw_text: str = ""
    ) -> bool:
        """就地给每个推荐项填 ``comment``。

        Returns:
            是否用了规则兜底。True 表示模型没用上，上层要置 fallback_used。
        """
        if not recommendations:
            return False

        if self._client is not None and self._client.available:
            try:
                comments = await self._write_by_model(recommendations, raw_text)
            except (QianfanError, ValueError) as exc:
                logger.warning("推荐语生成降级到规则理由: %s", exc)
            else:
                # 模型可能少写几条，缺的那几条单独用规则理由补上
                missing = False
                for rec in recommendations:
                    comment = comments.get(rec.dish.id, "").strip()
                    if comment:
                        rec.comment = comment[:MAX_COMMENT_CHARS]
                    else:
                        rec.comment = fallback_comment(rec)
                        missing = True
                return missing

        for rec in recommendations:
            rec.comment = fallback_comment(rec)
        return True

    async def _write_by_model(
        self, recommendations: list[Recommendation], raw_text: str
    ) -> dict[str, str]:
        assert self._client is not None
        prompt = build_prompt(recommendations, raw_text)
        raw = await self._client.chat(prompt, system=SYSTEM_PROMPT)
        return parse_comments(raw)


def build_prompt(recommendations: list[Recommendation], raw_text: str) -> str:
    """拼 prompt。

    只给模型「组织语言需要的信息」：菜名、位置、价格、口味、简介、规则理由。
    不给得分和权重明细——那是调参用的，写进 prompt 只会让模型去解释数字。
    """
    lines: list[str] = []
    if raw_text.strip():
        lines.append(f"用户原话：{raw_text.strip()}")
    lines.append(f"共 {len(recommendations)} 道菜，请为每道各写一句推荐语。\n")

    for index, rec in enumerate(recommendations, start=1):
        dish = rec.dish
        flavors = "、".join(f.value for f in dish.flavors) or "无特别标注"
        parts = [
            f"{index}. 编号 {dish.id}｜{dish.name}",
            f"   位置：{dish.canteen}{dish.window}｜价格：{dish.price:g} 元",
            f"   口味：{flavors}｜辣度：{dish.spicy_level} 级",
        ]
        if dish.description:
            parts.append(f"   简介：{dish.description}")
        if rec.reasons:
            parts.append(f"   推荐依据：{'；'.join(rec.reasons)}")
        lines.append("\n".join(parts))

    return "\n".join(lines)


def parse_comments(raw: str) -> dict[str, str]:
    """解析模型返回的 JSON 数组，转成 {菜品编号: 推荐语}。

    模型可能套代码块或漏掉某几条，这里只做结构解析，
    缺失和越界的处理交给调用方——它才知道该给哪些菜兜底。
    """
    match = JSON_ARRAY.search(raw or "")
    if not match:
        raise ValueError(f"模型输出里找不到 JSON 数组: {raw[:200]!r}")

    try:
        items = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise ValueError(f"推荐语 JSON 不合法: {exc}") from exc

    if not isinstance(items, list):
        raise ValueError(f"推荐语输出不是数组: {items!r}")

    comments: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        dish_id = item.get("id")
        comment = item.get("comment")
        if isinstance(dish_id, str) and isinstance(comment, str):
            comments[dish_id] = comment
    return comments


def fallback_comment(rec: Recommendation) -> str:
    """规则兜底推荐语。

    把结构化理由拼成一句话。读起来比模型生成的生硬，但信息量不少，
    而且绝对不会编造——每一条都是从菜品数据里算出来的。
    """
    if rec.reasons:
        return "，".join(rec.reasons[:2]) + "。"

    # 连理由都没有（各维度都不突出），退到最基本的客观描述
    dish = rec.dish
    return f"{dish.canteen}{dish.window}，{dish.price:g} 元，{dish.rating:g} 分。"
