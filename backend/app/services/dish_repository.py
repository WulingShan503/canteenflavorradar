"""菜品数据仓库：负责加载数据并按硬性条件筛选候选菜。

职责边界：
- 这一层只做「能不能吃」的硬性过滤（过敏、忌口、预算上限、辣度上限等）；
- 「有多想吃」的排序交给后面的打分器（scorer），两者分开便于单独调参和测试。
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from app.models.dish import Canteen, Dish
from app.models.preference import UserPreference

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# 逐级放宽的顺序：越靠前越先被放弃。
# 过敏原与饮食限制（素食/清真等）不在此列，属于安全底线，任何情况下都不放宽。
RELAX_STEPS: tuple[str, ...] = (
    "max_wait_minutes",
    "categories",
    "preferred_canteens",
    "spicy_tolerance",
    "budget_max",
    "calorie_limit",
)

RELAX_LABELS: dict[str, str] = {
    "max_wait_minutes": "放宽了排队时长要求",
    "categories": "扩大了菜品品类范围",
    "preferred_canteens": "扩大到了其他食堂",
    "spicy_tolerance": "略微放宽了辣度上限",
    "budget_max": "略微上调了预算上限",
    "calorie_limit": "略微上调了热量上限",
}


class DishRepository:
    """菜品数据的内存仓库。

    目前从 JSON 文件读取，后续换成数据库或学校接口时只需替换 ``_load``，
    上层的过滤和打分逻辑不用动。
    """

    def __init__(self, dishes: list[Dish], canteens: list[Canteen] | None = None):
        self._dishes = dishes
        self._canteens = canteens or []

    # ---------- 构造 ----------

    @classmethod
    def from_json(
        cls,
        dishes_path: Path | None = None,
        canteens_path: Path | None = None,
    ) -> DishRepository:
        dishes_file = dishes_path or DATA_DIR / "dishes.json"
        canteens_file = canteens_path or DATA_DIR / "canteens.json"

        dishes = [Dish(**item) for item in _read_json(dishes_file)]
        canteens = (
            [Canteen(**item) for item in _read_json(canteens_file)]
            if canteens_file.exists()
            else []
        )
        return cls(dishes, canteens)

    # ---------- 基础查询 ----------

    def all_dishes(self, include_unavailable: bool = False) -> list[Dish]:
        if include_unavailable:
            return list(self._dishes)
        return [d for d in self._dishes if d.available]

    def canteens(self) -> list[Canteen]:
        return list(self._canteens)

    def canteen_names(self) -> list[str]:
        seen: list[str] = []
        for dish in self._dishes:
            if dish.canteen not in seen:
                seen.append(dish.canteen)
        return seen

    def get(self, dish_id: str) -> Dish | None:
        return next((d for d in self._dishes if d.id == dish_id), None)

    def search(self, keyword: str) -> list[Dish]:
        """按菜名/食材/简介做关键词模糊匹配。"""
        kw = keyword.strip()
        if not kw:
            return []
        return [
            d
            for d in self._dishes
            if kw in d.name
            or kw in d.description
            or any(kw in ing for ing in d.ingredients)
        ]

    # ---------- 候选筛选 ----------

    def find_candidates(
        self, pref: UserPreference, min_results: int = 5
    ) -> tuple[list[Dish], list[str]]:
        """按偏好筛出候选菜。

        结果不足 ``min_results`` 时，按 ``RELAX_STEPS`` 顺序逐级放宽并记录说明，
        这样用户提了一堆苛刻条件也不会得到空列表。

        Returns:
            (候选菜列表, 放宽说明列表)
        """
        candidates = self._filter(pref, relaxed=set())
        if len(candidates) >= min_results:
            return candidates, []

        relaxed: set[str] = set()
        notes: list[str] = []
        for step in RELAX_STEPS:
            if not _step_applies(pref, step):
                continue
            relaxed.add(step)
            widened = self._filter(pref, relaxed=relaxed)
            # 只有真的多筛出菜才算「放宽过」。否则会出现「已上调热量上限」
            # 但结果里每道菜都在原上限之内的怪提示，用户会以为系统没听懂。
            if len(widened) > len(candidates):
                notes.append(RELAX_LABELS[step])
            candidates = widened
            if len(candidates) >= min_results:
                break

        # 全部放宽后仍然为空，说明是过敏/饮食限制卡住了，直接返回空由上层提示。
        return candidates, notes

    def _filter(self, pref: UserPreference, relaxed: set[str]) -> list[Dish]:
        return [d for d in self._dishes if self._matches(d, pref, relaxed)]

    def _matches(self, dish: Dish, pref: UserPreference, relaxed: set[str]) -> bool:
        if not dish.available:
            return False

        # --- 安全底线，不参与放宽 ---
        if pref.avoid_allergens and any(
            a in dish.allergens for a in pref.avoid_allergens
        ):
            return False

        if pref.dietary_tags and not all(
            tag in dish.dietary_tags for tag in pref.dietary_tags
        ):
            return False

        if pref.disliked_ingredients and _contains_ingredient(
            dish, pref.disliked_ingredients
        ):
            return False

        # 餐段不匹配等于吃不到，同样不放宽
        if pref.meal_period and dish.meal_periods:
            if pref.meal_period not in dish.meal_periods:
                return False

        # --- 以下可逐级放宽 ---
        # 注意：budget_min 不在这里拦截。用户说「想吃 15 块以上的」通常是
        # 「今天想吃好点」，把便宜菜全滤掉反而不合预期，交给打分层降权处理。

        if "budget_max" not in relaxed:
            if pref.budget_max is not None and dish.price > pref.budget_max:
                return False
        elif pref.budget_max is not None and dish.price > pref.budget_max * 1.2:
            return False

        if "spicy_tolerance" not in relaxed:
            if (
                pref.spicy_tolerance is not None
                and dish.spicy_level > pref.spicy_tolerance
            ):
                return False
        elif (
            pref.spicy_tolerance is not None
            and dish.spicy_level > pref.spicy_tolerance + 1
        ):
            return False

        if "categories" not in relaxed:
            if pref.categories and dish.category not in pref.categories:
                return False

        if "preferred_canteens" not in relaxed:
            if pref.preferred_canteens and dish.canteen not in pref.preferred_canteens:
                return False

        if "max_wait_minutes" not in relaxed:
            if (
                pref.max_wait_minutes is not None
                and dish.wait_minutes > pref.max_wait_minutes
            ):
                return False

        if "calorie_limit" not in relaxed:
            if (
                pref.calorie_limit is not None
                and dish.nutrition.calories > pref.calorie_limit
            ):
                return False
        elif (
            pref.calorie_limit is not None
            and dish.nutrition.calories > pref.calorie_limit * 1.15
        ):
            return False

        return True


# ---------- 辅助函数 ----------


def _read_json(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"{path} 顶层应为数组")
    return data


def _contains_ingredient(dish: Dish, keywords: list[str]) -> bool:
    """忌口食材匹配。

    用双向包含判断，「香菜」能命中「香菜末」，用户输入「牛肉面」也能命中「牛肉」。
    """
    for kw in keywords:
        kw = kw.strip()
        if not kw:
            continue
        for ing in dish.ingredients:
            if kw in ing or ing in kw:
                return True
        if kw in dish.name:
            return True
    return False


def _step_applies(pref: UserPreference, step: str) -> bool:
    """该放宽步骤对当前偏好是否有意义，避免放宽一个用户根本没设的条件。"""
    return {
        "max_wait_minutes": pref.max_wait_minutes is not None,
        "categories": bool(pref.categories),
        "preferred_canteens": bool(pref.preferred_canteens),
        "spicy_tolerance": pref.spicy_tolerance is not None,
        "budget_max": pref.budget_max is not None,
        "calorie_limit": pref.calorie_limit is not None,
    }.get(step, False)


@lru_cache(maxsize=1)
def get_repository() -> DishRepository:
    """全局单例，FastAPI 依赖注入用。数据文件在进程启动时只读一次。"""
    return DishRepository.from_json()
