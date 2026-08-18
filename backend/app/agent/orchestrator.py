"""推荐流程编排：把四层串起来。

流程：偏好解析（模型）→ 硬性过滤（规则）→ 打分排序（规则）→ 推荐语（模型）

**红线：模型解析出的偏好只是输入，不是许可。**
第一步的输出必须原封不动交给 :class:`DishRepository` 过滤，
过敏原、饮食限制这些安全条件由规则层判断。哪怕模型把「花生过敏」
解析成了别的东西，规则层拿到的也只是一份普通条件，不会放行任何东西——
真正的保障是过滤逻辑本身确定可复现，而不是信任模型的解析质量。
"""

from __future__ import annotations

import logging

from app.agent.comment_writer import CommentWriter
from app.agent.preference_parser import PreferenceParser
from app.config import Settings, get_settings
from app.models.preference import UserPreference
from app.models.recommendation import MealPlan, RecommendResponse
from app.services.dish_repository import DishRepository, get_repository
from app.services.qianfan_client import QianfanClient, get_client
from app.services.scorer import DishScorer

logger = logging.getLogger(__name__)

# 凑整餐时每个品类各取一道，按这个顺序拼
MEAL_PLAN_CATEGORIES = ("主食", "荤菜", "素菜")

EMPTY_MESSAGE = (
    "按你的条件没找到能吃的菜。过敏原和饮食限制是硬性要求不会放宽，"
    "可以试着放宽预算或换个食堂看看。"
)


class RecommendAgent:
    """推荐流程的入口。

    Args:
        repo: 菜品仓库。
        scorer: 打分器。
        client: 千帆客户端，None 表示全程走规则模式。
    """

    def __init__(
        self,
        repo: DishRepository,
        scorer: DishScorer,
        client: QianfanClient | None = None,
        settings: Settings | None = None,
    ):
        self._repo = repo
        self._scorer = scorer
        self._settings = settings or get_settings()
        self._parser = PreferenceParser(client)
        self._writer = CommentWriter(client)

    async def recommend(
        self,
        text: str = "",
        preference: UserPreference | None = None,
        limit: int | None = None,
        with_meal_plan: bool = False,
    ) -> RecommendResponse:
        """走完整个推荐流程。

        Args:
            text: 用户的自然语言需求。
            preference: 前端表单直接给的结构化偏好。给了就不调模型解析，
                但仍会用 text 生成推荐语。
            limit: 返回几道菜，默认读配置。
            with_meal_plan: 是否额外凑一份完整餐。
        """
        limit = limit or self._settings.recommend_limit
        fallback_used = False
        notes: list[str] = []

        # ---- 第一层：偏好解析 ----
        if preference is not None:
            pref = preference
            if text and not pref.raw_text:
                pref = pref.model_copy(update={"raw_text": text})
        else:
            pref, parse_fallback = await self._parser.parse(text)
            if parse_fallback:
                fallback_used = True
                if text:
                    notes.append("智能解析暂时不可用，已按关键词理解你的需求")

        # ---- 第二层：硬性过滤（规则，安全底线在这里）----
        candidates, relax_notes = self._repo.find_candidates(
            pref, min_results=self._settings.recommend_min_candidates
        )
        notes.extend(relax_notes)

        if not candidates:
            return RecommendResponse(
                recommendations=[],
                parsed_preference=pref.model_dump(mode="json", exclude_defaults=True),
                total_candidates=0,
                fallback_used=fallback_used,
                message=EMPTY_MESSAGE,
            )

        # ---- 第三层：打分排序（规则）----
        picked = self._scorer.rank_diverse(
            candidates,
            pref,
            limit=limit,
            max_per_window=self._settings.max_per_window,
        )

        # ---- 第四层：推荐语（模型，失败用规则理由兜底）----
        comment_fallback = await self._writer.write(picked, raw_text=pref.raw_text)
        if comment_fallback:
            fallback_used = True

        meal_plan = self._build_meal_plan(candidates, pref) if with_meal_plan else None

        return RecommendResponse(
            recommendations=picked,
            meal_plan=meal_plan,
            parsed_preference=pref.model_dump(mode="json", exclude_defaults=True),
            total_candidates=len(candidates),
            fallback_used=fallback_used,
            message="；".join(notes),
        )

    def _build_meal_plan(
        self, candidates: list, pref: UserPreference
    ) -> MealPlan | None:
        """凑一份主食 + 荤菜 + 素菜的完整餐。

        从同一批候选里各品类挑分最高的一道。缺哪个品类就跳过——
        食堂当天没素菜是常事，不该因此不给建议。
        """
        by_category: dict[str, list] = {}
        for dish in candidates:
            by_category.setdefault(dish.category.value, []).append(dish)

        items = []
        for category in MEAL_PLAN_CATEGORIES:
            dishes = by_category.get(category)
            if not dishes:
                continue
            best = self._scorer.rank(dishes, pref, limit=1)
            if best:
                items.append(best[0])

        if len(items) < 2:
            # 只凑出一道菜算不上「一份餐」，不如不给
            return None

        total_price = sum(item.dish.price for item in items)
        total_calories = sum(item.dish.nutrition.calories for item in items)

        return MealPlan(
            items=items,
            total_price=round(total_price, 2),
            total_calories=round(total_calories, 1),
            summary=(
                f"{len(items)} 道菜共 {total_price:g} 元、"
                f"{total_calories:g} 千卡，主食配菜都齐了。"
            ),
        )


_agent: RecommendAgent | None = None


def get_agent() -> RecommendAgent:
    """全局单例，FastAPI 依赖注入用。

    没配千帆密钥时也照样构造：客户端的 ``available`` 会是 False，
    解析和推荐语自动走规则路径。
    """
    global _agent
    if _agent is None:
        repo = get_repository()
        _agent = RecommendAgent(
            repo=repo,
            scorer=DishScorer.from_repository(repo),
            client=get_client(),
        )
    return _agent
