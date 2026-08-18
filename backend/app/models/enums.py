"""系统共享词表。

菜品数据、用户偏好、Agent 输出三者都引用这里的枚举，
避免出现「辣」「重辣」「spicy」混用导致匹配不上的问题。
"""

from enum import Enum


class Flavor(str, Enum):
    """基础口味标签。"""

    SPICY = "辣"
    NUMBING = "麻"
    SOUR = "酸"
    SWEET = "甜"
    SALTY = "咸"
    UMAMI = "鲜"
    LIGHT = "清淡"
    OILY = "油腻"
    GARLIC = "蒜香"
    SAUCE = "酱香"


class Cuisine(str, Enum):
    """菜系。"""

    SICHUAN = "川菜"
    HUNAN = "湘菜"
    CANTONESE = "粤菜"
    SHANDONG = "鲁菜"
    HUAIYANG = "淮扬菜"
    NORTHEAST = "东北菜"
    NORTHWEST = "西北菜"
    HOME = "家常菜"
    WESTERN = "西式"
    JAPANESE_KOREAN = "日韩"
    FASTFOOD = "快餐"


class Category(str, Enum):
    """菜品品类，用于凑成一份完整的餐。"""

    STAPLE = "主食"
    MEAT = "荤菜"
    VEGETABLE = "素菜"
    SOUP = "汤品"
    SNACK = "小吃"
    DRINK = "饮品"
    COMBO = "套餐"


class MealPeriod(str, Enum):
    """餐段。"""

    BREAKFAST = "早餐"
    LUNCH = "午餐"
    DINNER = "晚餐"
    LATE_NIGHT = "夜宵"


class DietaryTag(str, Enum):
    """饮食限制/目标标签。"""

    VEGETARIAN = "素食"
    VEGAN = "纯素"
    HALAL = "清真"
    LOW_FAT = "低脂"
    LOW_CARB = "低碳水"
    HIGH_PROTEIN = "高蛋白"
    LOW_SUGAR = "低糖"


class Allergen(str, Enum):
    """常见过敏原。"""

    PEANUT = "花生"
    NUTS = "坚果"
    SEAFOOD = "海鲜"
    SHELLFISH = "贝类"
    EGG = "蛋类"
    MILK = "乳制品"
    SOY = "大豆"
    GLUTEN = "麸质"
