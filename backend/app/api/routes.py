"""API 路由。

每个处理函数都尽量短：校验参数 → 调一层业务 → 返回。
异常处理也很少，因为编排层已经把千帆的失败兜住了——
`QianfanError` 不会漏到这里，它在 agent 里就被降级成规则结果了。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from app.agent.orchestrator import RecommendAgent, get_agent
from app.api.schemas import (
    CanteenListResponse,
    DishListResponse,
    HealthResponse,
    RecommendRequest,
)
from app.config import Settings, get_settings
from app.models.dish import Dish
from app.models.enums import Category, Cuisine, MealPeriod
from app.models.recommendation import RecommendResponse
from app.services.dish_repository import DishRepository, get_repository
from app.services.qianfan_client import get_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["recommend"])


@router.post(
    "/recommend",
    response_model=RecommendResponse,
    summary="按偏好推荐菜品",
)
async def recommend(
    payload: RecommendRequest,
    agent: RecommendAgent = Depends(get_agent),
) -> RecommendResponse:
    """推荐接口。

    千帆不可用时不会报错，而是返回规则结果并把 ``fallback_used`` 置 True，
    前端可以据此提示用户「智能解析暂时不可用」。
    """
    return await agent.recommend(
        text=payload.text,
        preference=payload.preference,
        limit=payload.limit,
        with_meal_plan=payload.with_meal_plan,
    )


@router.get("/dishes", response_model=DishListResponse, summary="查询菜品")
async def list_dishes(
    keyword: str | None = Query(None, max_length=50, description="菜名/食材关键词"),
    canteen: str | None = Query(None, max_length=50, description="按食堂筛选"),
    category: Category | None = Query(None, description="按品类筛选"),
    cuisine: Cuisine | None = Query(None, description="按菜系筛选"),
    meal_period: MealPeriod | None = Query(None, description="按餐段筛选"),
    include_unavailable: bool = Query(False, description="是否包含今日停供的菜"),
    repo: DishRepository = Depends(get_repository),
) -> DishListResponse:
    """菜品查询。

    这是给前端做浏览和搜索用的，不涉及偏好打分——
    纯粹按条件列菜，条件之间是「与」的关系。
    """
    dishes = (
        repo.search(keyword)
        if keyword
        else repo.all_dishes(include_unavailable=include_unavailable)
    )

    # 关键词搜索走的是全量数据，可用性得在这里补一刀
    if keyword and not include_unavailable:
        dishes = [d for d in dishes if d.available]

    if canteen:
        dishes = [d for d in dishes if d.canteen == canteen]
    if category:
        dishes = [d for d in dishes if d.category == category]
    if cuisine:
        dishes = [d for d in dishes if d.cuisine == cuisine]
    if meal_period:
        dishes = [d for d in dishes if meal_period in d.meal_periods]

    return DishListResponse(dishes=dishes, total=len(dishes))


@router.get("/dishes/{dish_id}", response_model=Dish, summary="查询单道菜")
async def get_dish(
    dish_id: str,
    repo: DishRepository = Depends(get_repository),
) -> Dish:
    """按编号查一道菜。查不到给 404，不要返回空对象让前端猜。"""
    dish = repo.get(dish_id)
    if dish is None:
        raise HTTPException(status_code=404, detail=f"没有编号为 {dish_id} 的菜品")
    return dish


@router.get("/canteens", response_model=CanteenListResponse, summary="食堂列表")
async def list_canteens(
    repo: DishRepository = Depends(get_repository),
) -> CanteenListResponse:
    canteens = repo.canteens()
    return CanteenListResponse(canteens=canteens, total=len(canteens))


@router.get("/health", response_model=HealthResponse, summary="探活")
async def health(
    settings: Settings = Depends(get_settings),
    repo: DishRepository = Depends(get_repository),
) -> HealthResponse:
    """探活。

    顺带回报千帆状态：配置了没、当前能不能调（熔断中会是 False）。
    部署后一眼能区分「没配密钥」和「配了但连不上」。
    """
    from app import __version__

    configured = settings.qianfan_configured()
    available = get_client().available if configured else False

    return HealthResponse(
        status="ok",
        app_name=settings.app_name,
        version=__version__,
        dish_count=len(repo.all_dishes()),
        qianfan_configured=configured,
        qianfan_available=available,
        mode="full" if available else "rule-only",
    )
