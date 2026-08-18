"""菜品与食堂窗口的数据模型。"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from app.models.enums import (
    Allergen,
    Category,
    Cuisine,
    DietaryTag,
    Flavor,
    MealPeriod,
)


class Nutrition(BaseModel):
    """每份菜品的营养成分，单位见字段说明。"""

    calories: float = Field(..., ge=0, description="热量，千卡")
    protein: float = Field(..., ge=0, description="蛋白质，克")
    fat: float = Field(..., ge=0, description="脂肪，克")
    carbs: float = Field(..., ge=0, description="碳水化合物，克")
    sodium: float | None = Field(None, ge=0, description="钠，毫克")


class Dish(BaseModel):
    """一道菜。

    这是整个系统的核心实体：过滤、打分、推荐理由都围绕它展开。
    """

    id: str = Field(..., description="全局唯一菜品编号，如 D1001")
    name: str = Field(..., min_length=1, description="菜名")
    canteen: str = Field(..., description="所属食堂，如 一食堂")
    window: str = Field(..., description="所属窗口，如 3号窗口·川味小炒")
    price: float = Field(..., gt=0, description="单价，元")
    category: Category
    cuisine: Cuisine

    flavors: list[Flavor] = Field(default_factory=list, description="口味标签")
    spicy_level: int = Field(0, ge=0, le=5, description="辣度，0 不辣至 5 特辣")

    ingredients: list[str] = Field(default_factory=list, description="主要食材")
    allergens: list[Allergen] = Field(default_factory=list, description="含有的过敏原")
    dietary_tags: list[DietaryTag] = Field(
        default_factory=list, description="满足的饮食标签"
    )

    nutrition: Nutrition
    meal_periods: list[MealPeriod] = Field(
        default_factory=list, description="供应餐段"
    )

    rating: float = Field(0.0, ge=0, le=5, description="学生平均评分")
    rating_count: int = Field(0, ge=0, description="评价人数")
    popularity: int = Field(0, ge=0, description="近七日销量，用于热度排序")
    wait_minutes: int = Field(0, ge=0, description="该窗口预计排队分钟数")

    available: bool = Field(True, description="今日是否供应")
    signature: bool = Field(False, description="是否为窗口招牌菜")
    description: str = Field("", description="菜品简介，供大模型生成推荐语参考")
    image_url: str | None = None

    @field_validator("flavors", "ingredients", "allergens", "dietary_tags", "meal_periods")
    @classmethod
    def _dedupe(cls, value: list) -> list:
        """去重但保持原顺序，示例数据里手写重复标签时不至于影响打分。"""
        seen = []
        for item in value:
            if item not in seen:
                seen.append(item)
        return seen

    @property
    def display_name(self) -> str:
        """给用户看的完整位置描述。"""
        return f"{self.canteen} {self.window} · {self.name}"

    def has_flavor(self, flavor: Flavor) -> bool:
        return flavor in self.flavors

    def protein_ratio(self) -> float:
        """蛋白质占热量的比例，高蛋白需求打分时用。"""
        if self.nutrition.calories <= 0:
            return 0.0
        return (self.nutrition.protein * 4) / self.nutrition.calories


class Canteen(BaseModel):
    """食堂基础信息。"""

    name: str
    location: str = Field("", description="校区/楼栋位置")
    open_periods: list[MealPeriod] = Field(default_factory=list)
    crowd_level: int = Field(0, ge=0, le=5, description="当前拥挤度，0 空闲至 5 爆满")
