"""接口请求体与响应体。

和 `app/models/` 里的领域模型分开：领域模型是内部结构，
接口模型是对外契约。分开后改内部字段不会直接破坏前端，
也能在这里加只对 HTTP 有意义的校验（比如 limit 上限）。
"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

from app.models.dish import Canteen, Dish
from app.models.preference import UserPreference

MAX_LIMIT = 20  # 一次最多返回 20 道，防止有人传 limit=10000 把响应撑爆
MAX_TEXT_LENGTH = 300  # 选餐需求不会写论文，超长的基本是误用或攻击


class RecommendRequest(BaseModel):
    """推荐请求。

    ``text`` 和 ``preference`` 至少给一个：
    - 只给 text：走模型解析（不可用时降级关键词规则）；
    - 只给 preference：前端表单已经填好了，跳过解析直接过滤打分；
    - 都给：以 preference 为准，text 仅用于生成推荐语时参考用户原话。
    - 都不给：走热门兜底，也能返回结果。
    """

    text: str = Field(
        "",
        max_length=MAX_TEXT_LENGTH,
        description="自然语言用餐需求，如「想吃辣的，别太贵，最近在减脂」",
    )
    preference: UserPreference | None = Field(
        None, description="结构化偏好，前端表单直接填好时用"
    )
    limit: int = Field(5, ge=1, le=MAX_LIMIT, description="返回几道菜")
    with_meal_plan: bool = Field(False, description="是否额外凑一份完整餐")

    @model_validator(mode="after")
    def _strip_text(self) -> RecommendRequest:
        self.text = self.text.strip()
        return self


class DishListResponse(BaseModel):
    """菜品查询结果。"""

    dishes: list[Dish] = Field(default_factory=list)
    total: int = Field(0, description="返回的菜品数量")


class CanteenListResponse(BaseModel):
    """食堂列表。"""

    canteens: list[Canteen] = Field(default_factory=list)
    total: int = 0


class HealthResponse(BaseModel):
    """探活。

    把千帆是否可用暴露出来，部署后一眼能看出是「没配密钥」
    还是「配了但连不上」，省掉一轮排查。
    """

    status: str = Field("ok")
    app_name: str = ""
    version: str = ""
    dish_count: int = Field(0, description="已加载的在售菜品数")
    qianfan_configured: bool = Field(False, description="是否配置了千帆密钥")
    qianfan_available: bool = Field(
        False, description="千帆当前是否可调用，熔断中会是 False"
    )
    mode: str = Field(
        "", description="rule-only 表示纯规则模式，full 表示模型可用"
    )


class ErrorResponse(BaseModel):
    """统一错误体。"""

    detail: str
    code: str = Field("internal_error", description="机器可读的错误类型")
