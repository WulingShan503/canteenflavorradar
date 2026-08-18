"""用户口味偏好模型。

两种来路：
1. 前端表单直接填好结构化字段；
2. 用户一句自然语言（如「想吃辣的，别太贵，最近在减脂」），
   由 Agent 的偏好解析环节转成同一个 UserPreference 对象。
"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

from app.models.enums import (
    Allergen,
    Category,
    Cuisine,
    DietaryTag,
    Flavor,
    MealPeriod,
)


class UserPreference(BaseModel):
    """一次选餐请求所携带的全部偏好条件。

    字段全部可选：用户只说「想吃辣的」也应该能推荐，
    缺失的维度在打分时按中性处理，不做惩罚。
    """

    # ---- 口味 ----
    liked_flavors: list[Flavor] = Field(default_factory=list, description="偏好口味")
    disliked_flavors: list[Flavor] = Field(default_factory=list, description="不喜欢的口味")
    spicy_tolerance: int | None = Field(
        None, ge=0, le=5, description="可接受的最高辣度，None 表示不限"
    )

    # ---- 菜系与品类 ----
    liked_cuisines: list[Cuisine] = Field(default_factory=list)
    categories: list[Category] = Field(
        default_factory=list, description="想吃的品类，为空表示不限"
    )

    # ---- 预算 ----
    budget_min: float | None = Field(None, ge=0, description="最低预算，元")
    budget_max: float | None = Field(None, gt=0, description="最高预算，元")

    # ---- 饮食限制 ----
    dietary_tags: list[DietaryTag] = Field(
        default_factory=list, description="必须满足的饮食要求"
    )
    avoid_allergens: list[Allergen] = Field(
        default_factory=list, description="必须规避的过敏原，硬性条件"
    )
    disliked_ingredients: list[str] = Field(
        default_factory=list, description="忌口食材，如 香菜、内脏"
    )

    # ---- 营养目标 ----
    calorie_limit: float | None = Field(
        None, gt=0, description="单餐热量上限，千卡"
    )
    min_protein: float | None = Field(None, ge=0, description="单餐蛋白质下限，克")

    # ---- 场景 ----
    meal_period: MealPeriod | None = Field(None, description="用餐时段")
    preferred_canteens: list[str] = Field(
        default_factory=list, description="偏好食堂，为空表示不限"
    )
    max_wait_minutes: int | None = Field(
        None, ge=0, description="能接受的最长排队时间"
    )

    # ---- 原始输入 ----
    raw_text: str = Field("", description="用户的自然语言原话，供大模型生成推荐语时引用")

    @model_validator(mode="after")
    def _check_budget_range(self) -> UserPreference:
        if (
            self.budget_min is not None
            and self.budget_max is not None
            and self.budget_min > self.budget_max
        ):
            raise ValueError("budget_min 不能大于 budget_max")
        return self

    @model_validator(mode="after")
    def _drop_conflicting_flavors(self) -> UserPreference:
        """同一口味既喜欢又讨厌时，以「喜欢」为准，避免打分自相矛盾。"""
        if self.liked_flavors and self.disliked_flavors:
            self.disliked_flavors = [
                f for f in self.disliked_flavors if f not in self.liked_flavors
            ]
        return self

    def is_empty(self) -> bool:
        """是否没有提供任何有效偏好，此时应走热门推荐兜底。"""
        return not any(
            [
                self.liked_flavors,
                self.disliked_flavors,
                self.spicy_tolerance is not None,
                self.liked_cuisines,
                self.categories,
                self.budget_min is not None,
                self.budget_max is not None,
                self.dietary_tags,
                self.avoid_allergens,
                self.disliked_ingredients,
                self.calorie_limit is not None,
                self.min_protein is not None,
                self.preferred_canteens,
                self.max_wait_minutes is not None,
            ]
        )
