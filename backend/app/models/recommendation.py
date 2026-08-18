"""推荐结果模型。"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.models.dish import Dish


class ScoreBreakdown(BaseModel):
    """各维度得分明细。

    保留明细而不是只给一个总分，是为了让推荐理由有据可依，
    同时方便调参时看出是哪一维把菜顶上来的。
    """

    flavor: float = Field(0.0, description="口味匹配得分")
    cuisine: float = Field(0.0, description="菜系匹配得分")
    price: float = Field(0.0, description="预算契合得分")
    nutrition: float = Field(0.0, description="营养目标得分")
    popularity: float = Field(0.0, description="口碑热度得分")
    convenience: float = Field(0.0, description="排队与食堂便利度得分")

    def total(self) -> float:
        return (
            self.flavor
            + self.cuisine
            + self.price
            + self.nutrition
            + self.popularity
            + self.convenience
        )


class Recommendation(BaseModel):
    """单道菜的推荐结果。"""

    dish: Dish
    score: float = Field(..., description="综合得分，0-100")
    breakdown: ScoreBreakdown
    reasons: list[str] = Field(
        default_factory=list, description="规则层生成的结构化推荐理由"
    )
    comment: str = Field("", description="大模型生成的推荐语，失败时为空")


class MealPlan(BaseModel):
    """一份配好的餐：主食 + 荤菜 + 素菜等。"""

    items: list[Recommendation] = Field(default_factory=list)
    total_price: float = 0.0
    total_calories: float = 0.0
    summary: str = Field("", description="整餐点评")


class RecommendResponse(BaseModel):
    """推荐接口的返回体。"""

    recommendations: list[Recommendation] = Field(default_factory=list)
    meal_plan: MealPlan | None = Field(None, description="凑整餐建议，可选")
    parsed_preference: dict = Field(
        default_factory=dict, description="Agent 解析出的偏好，便于前端回显确认"
    )
    total_candidates: int = Field(0, description="过滤后进入打分的候选菜数量")
    fallback_used: bool = Field(
        False, description="是否因条件过严或模型不可用而走了兜底策略"
    )
    message: str = Field("", description="给用户的提示，如条件过严已放宽")
