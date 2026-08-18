"""关键词规则版偏好解析。

千帆不可用、或模型输出解析不出来时的降级路径。
覆盖不了自然语言的全部表达，但能兜住食堂选餐最常见的那几句话
（「想吃辣的」「便宜点」「减脂」「不要香菜」），保证系统不瘫痪。

刻意做得保守：宁可少解析出一个条件，也不要猜错。
猜错口味只是推荐不准，猜错过敏原就是安全问题——所以过敏原只认明确的表述。
"""

from __future__ import annotations

import re

from app.models.enums import (
    Allergen,
    Category,
    Cuisine,
    DietaryTag,
    Flavor,
    MealPeriod,
)

# 口味词表：一个口味对应多种说法，命中任意一个即算
FLAVOR_WORDS: dict[Flavor, tuple[str, ...]] = {
    Flavor.SPICY: ("辣", "香辣", "麻辣", "重口"),
    Flavor.NUMBING: ("麻", "花椒"),
    Flavor.SOUR: ("酸", "醋"),
    Flavor.SWEET: ("甜", "糖"),
    Flavor.SALTY: ("咸",),
    Flavor.UMAMI: ("鲜", "鲜香"),
    Flavor.LIGHT: ("清淡", "淡", "不油", "少油", "养胃", "口味轻"),
    Flavor.OILY: ("油腻", "重油"),
    Flavor.GARLIC: ("蒜香", "蒜"),
    Flavor.SAUCE: ("酱香", "酱"),
}

# 有些口味词是别的常用词的一部分，单独出现才算口味。
# 「海鲜过敏」里的「鲜」是食材名不是口味诉求，
# 「糖醋排骨」里的「糖」也不代表用户想吃甜的。
FLAVOR_EXCLUSIONS: dict[str, tuple[str, ...]] = {
    "鲜": ("海鲜", "生鲜", "鲜奶", "新鲜"),
    "糖": ("低糖", "控糖", "无糖", "糖尿"),
    "淡": ("淡水",),
    "酸": ("氨基酸", "酸奶"),
    "蒜": ("蒜苗",),
    "麻": ("芝麻", "麻酱", "麻烦"),
    "酱": ("蒜香酱",),
}

# 否定表述 → 归到 disliked_flavors
NEGATIONS = ("不", "别", "不要", "不想", "讨厌", "受不了", "怕", "少", "无")

CUISINE_WORDS: dict[Cuisine, tuple[str, ...]] = {
    Cuisine.SICHUAN: ("川菜", "四川", "川味"),
    Cuisine.HUNAN: ("湘菜", "湖南"),
    Cuisine.CANTONESE: ("粤菜", "广东", "广式"),
    Cuisine.SHANDONG: ("鲁菜", "山东"),
    Cuisine.HUAIYANG: ("淮扬菜", "淮扬", "江浙"),
    Cuisine.NORTHEAST: ("东北菜", "东北"),
    Cuisine.NORTHWEST: ("西北菜", "西北", "兰州"),
    Cuisine.HOME: ("家常菜", "家常"),
    Cuisine.WESTERN: ("西餐", "西式", "意面", "汉堡"),
    Cuisine.JAPANESE_KOREAN: ("日料", "日式", "韩式", "韩国", "寿司", "拌饭"),
    Cuisine.FASTFOOD: ("快餐", "速食"),
}

CATEGORY_WORDS: dict[Category, tuple[str, ...]] = {
    Category.STAPLE: ("主食", "米饭", "面食", "馒头"),
    Category.MEAT: ("荤菜", "肉", "吃肉"),
    Category.VEGETABLE: ("素菜", "青菜", "蔬菜"),
    Category.SOUP: ("汤", "汤品", "粥"),
    Category.SNACK: ("小吃", "零嘴"),
    Category.DRINK: ("饮品", "喝的", "豆浆", "饮料"),
    Category.COMBO: ("套餐", "盖饭", "简餐"),
}

DIETARY_WORDS: dict[DietaryTag, tuple[str, ...]] = {
    DietaryTag.VEGETARIAN: ("素食", "吃素", "斋"),
    DietaryTag.VEGAN: ("纯素", "全素"),
    DietaryTag.HALAL: ("清真", "穆斯林"),
    DietaryTag.LOW_FAT: ("低脂", "减脂", "减肥", "低油", "刷脂"),
    DietaryTag.LOW_CARB: ("低碳水", "低碳", "断碳"),
    DietaryTag.HIGH_PROTEIN: ("高蛋白", "增肌", "练完", "健身", "补蛋白"),
    DietaryTag.LOW_SUGAR: ("低糖", "控糖", "无糖"),
}

# 过敏原只认明确表述：必须同时出现过敏原名和「过敏/忌/不能吃」这类词，
# 光提到「花生」可能是「想吃花生」。宁可漏掉交给用户在表单里补，
# 也不能凭一个名词就断定人家过敏——反过来把菜错误放行才是真风险，
# 而这里漏解析只会让用户自己发现并补填。
ALLERGEN_WORDS: dict[Allergen, tuple[str, ...]] = {
    Allergen.PEANUT: ("花生",),
    Allergen.NUTS: ("坚果", "核桃", "杏仁", "腰果"),
    Allergen.SEAFOOD: ("海鲜", "鱼", "虾"),
    Allergen.SHELLFISH: ("贝类", "扇贝", "蛤"),
    Allergen.EGG: ("鸡蛋", "蛋类", "蛋"),
    Allergen.MILK: ("乳制品", "牛奶", "奶", "芝士", "乳糖"),
    Allergen.SOY: ("大豆", "豆制品", "黄豆"),
    Allergen.GLUTEN: ("麸质", "面筋", "小麦"),
}

ALLERGY_MARKERS = ("过敏", "忌", "不能吃", "吃不了", "不耐受", "禁忌")

MEAL_PERIOD_WORDS: dict[MealPeriod, tuple[str, ...]] = {
    MealPeriod.BREAKFAST: ("早餐", "早饭", "早上"),
    MealPeriod.LUNCH: ("午餐", "午饭", "中午"),
    MealPeriod.DINNER: ("晚餐", "晚饭", "晚上"),
    MealPeriod.LATE_NIGHT: ("夜宵", "宵夜", "半夜"),
}

# 忌口食材：食堂场景里最常被点名的几个
COMMON_DISLIKES = (
    "香菜",
    "葱",
    "姜",
    "蒜",
    "内脏",
    "肥肉",
    "苦瓜",
    "胡萝卜",
    "洋葱",
    "芹菜",
    "羊肉",
    "辣椒",
)

# 辣度：把口语映射成 0-5
SPICY_TOLERANCE_WORDS: tuple[tuple[tuple[str, ...], int], ...] = (
    (("微辣", "小辣", "一点辣", "轻辣"), 2),
    (("中辣", "适中"), 3),
    (("特辣", "变态辣", "巨辣", "越辣越好", "重辣"), 5),
    (("不吃辣", "不能吃辣", "怕辣", "不要辣", "不辣", "忌辣"), 0),
)

CHEAP_WORDS = ("便宜", "省钱", "实惠", "划算", "穷", "预算紧", "便宜点")
CHEAP_BUDGET_MAX = 10.0  # 「便宜点」按 10 元以内理解
TREAT_WORDS = ("吃好点", "奖励自己", "改善", "不差钱", "贵点")
TREAT_BUDGET_MIN = 15.0

# 金额相关的正则**必须带上「块」或「元」**。
# 早期版本把单位写成可选，结果「排队不超过 10 分钟」被解析成预算 10 元、
# 「蛋白质至少 30 克」被解析成最低预算 30 元——凭空给用户加了个没提的条件。
# 宁可漏掉「不超过 20」这种省略单位的说法，也不能跨维度误判。
MONEY = r"(?:块钱|块|元)"

BUDGET_MAX_PATTERNS = (
    re.compile(rf"(\d+(?:\.\d+)?)\s*{MONEY}\s*(?:以内|以下|之内|左右|封顶)"),
    re.compile(
        rf"(?:不超过|不高于|最多|别超过|控制在)\s*(\d+(?:\.\d+)?)\s*{MONEY}"
    ),
    re.compile(rf"预算\s*(?:是|在)?\s*(\d+(?:\.\d+)?)\s*{MONEY}"),
)
BUDGET_MIN_PATTERNS = (
    re.compile(rf"(\d+(?:\.\d+)?)\s*{MONEY}\s*(?:以上|往上|起步)"),
    re.compile(rf"(?:不低于|至少|起码)\s*(\d+(?:\.\d+)?)\s*{MONEY}"),
)

CALORIE_PATTERNS = (
    re.compile(r"(\d+(?:\.\d+)?)\s*(?:千卡|大卡|卡|kcal)\s*(?:以内|以下)?"),
)
PROTEIN_PATTERNS = (
    re.compile(r"蛋白(?:质)?\s*(?:至少|不低于)?\s*(\d+(?:\.\d+)?)\s*(?:克|g)"),
)
WAIT_PATTERNS = (
    re.compile(r"(?:排队|等)\s*(?:不超过|少于|别超过)?\s*(\d+)\s*(?:分钟|分)"),
    re.compile(r"(\d+)\s*(?:分钟|分)\s*(?:以内|之内|以下)"),
)
CANTEEN_PATTERN = re.compile(r"([一二三四五六七八九十\d]+)\s*食堂")


def find_negation_window(text: str, keyword: str, window: int = 4) -> bool:
    """关键词前面一小段里有没有否定词。

    中文否定词基本都在被否定的对象之前（「不要太辣」「别放香菜」），
    所以只往前看一个窗口。窗口取 4 个字：太长会把上一个分句的否定词
    错误地算进来（「不要香菜，要辣的」里的「辣」不该被判成否定）。
    """
    idx = text.find(keyword)
    while idx != -1:
        start = max(0, idx - window)
        segment = text[start:idx]
        # 遇到分句边界就不再往前看
        for sep in "，,。；;！!？?":
            if sep in segment:
                segment = segment.rsplit(sep, 1)[1]
        if any(neg in segment for neg in NEGATIONS):
            return True
        idx = text.find(keyword, idx + 1)
    return False


CLAUSE_SEPARATORS = re.compile(r"[，,。；;！!？?、\s]+")


def split_clauses(text: str) -> list[str]:
    """按标点切分句子。

    过敏原判断需要「标记词和过敏原在同一分句」，否则
    「海鲜过敏，想吃鸡蛋」会把蛋类也当成过敏原。
    """
    return [c for c in CLAUSE_SEPARATORS.split(text or "") if c]


def word_hit(text: str, word: str) -> bool:
    """关键词是否真的作为独立诉求出现。

    单字口味词容易被更长的词包住（「鲜」在「海鲜」里），
    命中的位置如果全都落在排除词内部，就不算命中。
    """
    exclusions = FLAVOR_EXCLUSIONS.get(word)
    if not exclusions:
        return word in text

    idx = text.find(word)
    while idx != -1:
        covered = False
        for bad in exclusions:
            bad_idx = text.find(bad)
            while bad_idx != -1:
                if bad_idx <= idx < bad_idx + len(bad):
                    covered = True
                    break
                bad_idx = text.find(bad, bad_idx + 1)
            if covered:
                break
        if not covered:
            return True
        idx = text.find(word, idx + 1)
    return False


def match_first(text: str, patterns: tuple[re.Pattern[str], ...]) -> float | None:
    """按顺序试一组正则，返回第一个匹配到的数字。"""
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            try:
                return float(match.group(1))
            except (TypeError, ValueError):
                continue
    return None
