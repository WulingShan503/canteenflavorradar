"""菜品打分排序器：回答「有多想吃」。

职责边界：
- 进到这一层的菜都已通过 :class:`DishRepository` 的硬性过滤，
  「能不能吃」已经由确定性代码兜住，这里只负责排序，不再做任何安全判断；
- 六个维度各自算出 0-1 的原始分，乘权重后写进 :class:`ScoreBreakdown`，
  权重之和为 100，所以 ``breakdown.total()`` 直接就是 0-100 的综合分；
- 用户没提的维度按中性分处理，不做惩罚——只说「想吃辣的」的用户
  不该因为没提预算而被扣分；
- 这一层不碰大模型，纯规则、可复现，方便调参和写断言。
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from app.models.dish import Dish
from app.models.enums import DietaryTag
from app.models.preference import UserPreference
from app.models.recommendation import Recommendation, ScoreBreakdown
from app.services.dish_repository import DishRepository, get_repository

NEUTRAL = 0.5  # 用户未表达该维度偏好时的中性分，不奖不罚

# 评分贝叶斯收缩的先验：评价人数少的高分不该压过评价人数多的菜。
RATING_PRIOR_COUNT = 120
RATING_PRIOR_SCORE = 4.0
RATING_FLOOR = 3.0  # 低于此分视作 0，把实际集中在 4.0-4.8 的分布拉开

POPULARITY_FULL = 800  # 近七日销量达到这个值即视为满分热度
WAIT_TOLERABLE = 20  # 排队 20 分钟以上便利度归零
CROWD_MAX = 5

# 营养相关的经验阈值，均为「供能占比」
PROTEIN_RATIO_FULL = 0.35  # 蛋白质供能占 35% 视为高蛋白满分
FAT_RATIO_BAD = 0.45  # 脂肪供能占 45% 视为低脂诉求下的最差情形
CARB_RATIO_BAD = 0.60  # 碳水供能占 60% 视为低碳水诉求下的最差情形

# 热量留有余量才算好：用到上限的 60% 以内给满分，之后线性衰减
CALORIE_SWEET_SPOT = 0.6
CALORIE_DECAY_SPAN = 0.9

HIGHLIGHT_RATIO = 0.7  # 某维得分达到满分的七成才算「亮点」，才拿出来当理由
MAX_REASONS = 4

@dataclass(frozen=True)
class ScoreWeights:
    """六维权重，之和应为 100。

    默认权重的取舍：口味是选餐的第一诉求，给最高权重；口碑热度虽然不是
    用户明确提出的，但能有效兜住「用户没说清楚时给个靠谱结果」，所以给到 18；
    便利度权重最低——排队久点也就是等一会儿，不影响好不好吃。
    """

    flavor: float = 30.0
    cuisine: float = 14.0
    price: float = 16.0
    nutrition: float = 12.0
    popularity: float = 18.0
    convenience: float = 10.0

    def total(self) -> float:
        return (
            self.flavor
            + self.cuisine
            + self.price
            + self.nutrition
            + self.popularity
            + self.convenience
        )


# 减脂/增肌等带明确营养目标的请求，把营养权重顶上来，口味相应让位。
NUTRITION_FIRST_WEIGHTS = ScoreWeights(
    flavor=22.0,
    cuisine=10.0,
    price=14.0,
    nutrition=28.0,
    popularity=16.0,
    convenience=10.0,
)

# 用户完全没提偏好时走热门兜底：口味/菜系/营养都无从判断，全押口碑和便利度。
POPULAR_FALLBACK_WEIGHTS = ScoreWeights(
    flavor=0.0,
    cuisine=0.0,
    price=10.0,
    nutrition=0.0,
    popularity=62.0,
    convenience=28.0,
)

NUTRITION_TAGS = frozenset(
    {
        DietaryTag.LOW_FAT,
        DietaryTag.LOW_CARB,
        DietaryTag.HIGH_PROTEIN,
        DietaryTag.LOW_SUGAR,
    }
)


def pick_weights(pref: UserPreference) -> ScoreWeights:
    """根据偏好侧重挑一套权重。

    判断顺序有讲究：先看「有没有偏好」，再看「偏好是不是营养导向」。
    """
    if pref.is_empty():
        return POPULAR_FALLBACK_WEIGHTS
    if (
        pref.calorie_limit is not None
        or pref.min_protein is not None
        or any(tag in NUTRITION_TAGS for tag in pref.dietary_tags)
    ):
        return NUTRITION_FIRST_WEIGHTS
    return ScoreWeights()


# ---------- 六个维度的原始分，一律返回 0-1 ----------


def score_flavor(dish: Dish, pref: UserPreference) -> float:
    """口味匹配。

    三个部分：命中喜欢的口味加分、命中讨厌的口味扣分、辣度贴合度。
    辣度单独算是因为它是连续量：能吃 4 级辣的人拿到 4 级辣的菜最满意，
    拿到 0 级的菜虽然「能吃」但并不是他想要的，只给部分分。
    """
    parts: list[tuple[float, float]] = []  # (得分, 该部分的相对比重)

    if pref.liked_flavors:
        hit = sum(1 for f in pref.liked_flavors if f in dish.flavors)
        parts.append((hit / len(pref.liked_flavors), 2.0))

    if pref.disliked_flavors:
        hit = sum(1 for f in pref.disliked_flavors if f in dish.flavors)
        # 命中一个讨厌口味就砍掉大半分，命中两个及以上直接归零
        parts.append((max(0.0, 1.0 - hit * 0.7), 1.5))

    if pref.spicy_tolerance is not None:
        parts.append((_spicy_fit(dish.spicy_level, pref.spicy_tolerance), 1.0))

    if not parts:
        return NEUTRAL

    weighted = sum(value * weight for value, weight in parts)
    return _clamp(weighted / sum(weight for _, weight in parts))


def _spicy_fit(level: int, tolerance: int) -> float:
    """辣度贴合度。

    容忍度为 0（完全不吃辣）时，只有 0 级辣满分，其余交由过滤层拦掉，
    这里保守给 0。容忍度大于 0 时，越接近上限越满意，但不低于 0.4——
    毕竟「能吃辣」不等于「非辣不吃」。
    """
    if tolerance <= 0:
        return 1.0 if level == 0 else 0.0
    if level > tolerance:
        # 放宽过滤后可能漏进来一级，给个明显偏低的分让它排后面
        return 0.2
    return 0.4 + 0.6 * (level / tolerance)


def score_cuisine(dish: Dish, pref: UserPreference) -> float:
    """菜系匹配。命中给满分，没命中不至于归零——菜系是偏好而非硬条件。"""
    if not pref.liked_cuisines:
        return NEUTRAL
    return 1.0 if dish.cuisine in pref.liked_cuisines else 0.25


def score_price(dish: Dish, pref: UserPreference) -> float:
    """预算契合度。

    - 只给了上限：越省钱越高分，但不鼓励一味吃最便宜的，
      落在上限 50%-90% 区间视为「花得值」，给满分；
    - 只给了下限：低于下限的菜降权（对应过滤层故意不拦 budget_min 的设计），
      不是排除，只是往后排；
    - 都没给：中性分。
    """
    low, high = pref.budget_min, pref.budget_max

    if high is None and low is None:
        return NEUTRAL

    if high is not None and low is not None:
        if low <= dish.price <= high:
            return 1.0
        # 落在区间外，按超出幅度相对区间宽度衰减
        span = max(high - low, 1.0)
        gap = low - dish.price if dish.price < low else dish.price - high
        return _clamp(1.0 - gap / span)

    if high is not None:
        if dish.price > high:  # 放宽过滤后漏进来的
            return _clamp(1.0 - (dish.price - high) / high) * 0.5
        ratio = dish.price / high
        if ratio >= 0.5:
            return 1.0
        # 便宜也是好事，但过于便宜可能份量不足，给 0.75 起步
        return 0.75 + 0.5 * ratio

    # 只有下限
    if dish.price >= low:
        return 1.0
    return _clamp(0.35 + 0.65 * (dish.price / low)) if low > 0 else 1.0


def score_nutrition(dish: Dish, pref: UserPreference) -> float:
    """营养目标匹配。

    过滤层已经保证 dietary_tags 全部满足、热量不超上限，
    这里做的是在「都合格」的菜之间分出高下：热量留的余量、
    蛋白质密度、脂肪与碳水的供能占比。
    """
    parts: list[tuple[float, float]] = []
    nut = dish.nutrition

    if pref.calorie_limit is not None and pref.calorie_limit > 0:
        parts.append((_calorie_fit(nut.calories, pref.calorie_limit), 1.5))

    if pref.min_protein is not None and pref.min_protein > 0:
        parts.append((_clamp(nut.protein / pref.min_protein), 1.5))

    tags = set(pref.dietary_tags)

    if DietaryTag.HIGH_PROTEIN in tags:
        parts.append((_clamp(dish.protein_ratio() / PROTEIN_RATIO_FULL), 1.5))

    if DietaryTag.LOW_FAT in tags:
        fat_ratio = _energy_ratio(nut.fat * 9, nut.calories)
        parts.append((_clamp(1.0 - fat_ratio / FAT_RATIO_BAD), 1.0))

    if DietaryTag.LOW_CARB in tags or DietaryTag.LOW_SUGAR in tags:
        carb_ratio = _energy_ratio(nut.carbs * 4, nut.calories)
        parts.append((_clamp(1.0 - carb_ratio / CARB_RATIO_BAD), 1.0))

    if not parts:
        return NEUTRAL

    weighted = sum(value * weight for value, weight in parts)
    return _clamp(weighted / sum(weight for _, weight in parts))


def _calorie_fit(calories: float, limit: float) -> float:
    """热量余量评分。用到上限 60% 以内满分，之后线性衰减到 0。"""
    ratio = calories / limit
    if ratio <= CALORIE_SWEET_SPOT:
        return 1.0
    return _clamp(1.0 - (ratio - CALORIE_SWEET_SPOT) / CALORIE_DECAY_SPAN)


def _energy_ratio(kcal_from_macro: float, total_calories: float) -> float:
    if total_calories <= 0:
        return 0.0
    return kcal_from_macro / total_calories


def score_popularity(dish: Dish) -> float:
    """口碑热度。评分与销量各占一半，再给招牌菜一点加成。

    评分用贝叶斯收缩：只有 20 人评的 4.9 分不该压过 800 人评的 4.6 分。
    这一维与用户偏好无关，是「大家都说好吃」的客观兜底。
    """
    shrunk = (
        dish.rating * dish.rating_count + RATING_PRIOR_SCORE * RATING_PRIOR_COUNT
    ) / (dish.rating_count + RATING_PRIOR_COUNT)
    rating_part = _clamp((shrunk - RATING_FLOOR) / (5.0 - RATING_FLOOR))
    popularity_part = _clamp(dish.popularity / POPULARITY_FULL)

    base = 0.5 * rating_part + 0.5 * popularity_part
    if dish.signature:
        base += 0.08
    return _clamp(base)


def score_convenience(
    dish: Dish, pref: UserPreference, crowd_levels: dict[str, int] | None = None
) -> float:
    """便利度：排队时长为主，食堂拥挤度和是否为偏好食堂为辅。"""
    wait_part = _clamp(1.0 - dish.wait_minutes / WAIT_TOLERABLE)

    parts: list[tuple[float, float]] = [(wait_part, 2.0)]

    if crowd_levels:
        crowd = crowd_levels.get(dish.canteen)
        if crowd is not None:
            parts.append((_clamp(1.0 - crowd / CROWD_MAX), 1.0))

    if pref.preferred_canteens:
        parts.append((1.0 if dish.canteen in pref.preferred_canteens else 0.3, 1.0))

    if pref.max_wait_minutes is not None and dish.wait_minutes > pref.max_wait_minutes:
        # 放宽过滤后漏进来的，明确降权
        parts.append((0.0, 1.5))

    weighted = sum(value * weight for value, weight in parts)
    return _clamp(weighted / sum(weight for _, weight in parts))


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    """把原始分夹到 [0, 1]，各维度公式里出现负数或超 1 都由它兜住。"""
    return max(low, min(high, value))


# ---------- 规则版推荐理由 ----------


def build_reasons(dish: Dish, pref: UserPreference, raw: dict[str, float]) -> list[str]:
    """按维度亮点拼出结构化理由。

    这是规则兜底版：大模型不可用时直接展示给用户；模型可用时作为
    生成推荐语的事实依据传进 prompt——让模型有据可依，少编造。
    只挑得分突出的维度，避免堆一串「还行」的废话。
    """
    reasons: list[str] = []

    if raw["flavor"] >= HIGHLIGHT_RATIO:
        hit = [f.value for f in pref.liked_flavors if f in dish.flavors]
        if hit:
            reasons.append(f"命中你想要的{'、'.join(hit)}口味")
        elif pref.spicy_tolerance is not None and dish.spicy_level > 0:
            reasons.append(f"辣度 {dish.spicy_level} 级，在你能接受的范围内")

    if raw["cuisine"] >= HIGHLIGHT_RATIO and pref.liked_cuisines:
        reasons.append(f"属于你偏好的{dish.cuisine.value}")

    if raw["price"] >= HIGHLIGHT_RATIO:
        if pref.budget_max is not None:
            reasons.append(f"{dish.price:g} 元，在 {pref.budget_max:g} 元预算内")
        elif pref.budget_min is not None:
            reasons.append(f"{dish.price:g} 元，够得上你想吃好点的预期")

    if raw["nutrition"] >= HIGHLIGHT_RATIO:
        reasons.append(_nutrition_reason(dish, pref))

    if raw["popularity"] >= HIGHLIGHT_RATIO:
        if dish.signature:
            reasons.append(
                f"{dish.window}招牌菜，{dish.rating_count} 人评 {dish.rating:g} 分"
            )
        else:
            reasons.append(f"近七日卖出 {dish.popularity} 份，{dish.rating:g} 分口碑")

    if raw["convenience"] >= HIGHLIGHT_RATIO and dish.wait_minutes <= 5:
        reasons.append(f"预计只排 {dish.wait_minutes} 分钟")

    return [r for r in reasons if r][:MAX_REASONS]


def _nutrition_reason(dish: Dish, pref: UserPreference) -> str:
    """营养理由挑最贴合用户诉求的那一句说，不要一次报全部数字。"""
    nut = dish.nutrition
    tags = set(pref.dietary_tags)

    if DietaryTag.HIGH_PROTEIN in tags or pref.min_protein is not None:
        return f"蛋白质 {nut.protein:g} 克，扛饿又顶练"
    if DietaryTag.LOW_FAT in tags:
        return f"脂肪只有 {nut.fat:g} 克，热量 {nut.calories:g} 千卡"
    if DietaryTag.LOW_CARB in tags or DietaryTag.LOW_SUGAR in tags:
        return f"碳水 {nut.carbs:g} 克，控糖友好"
    if pref.calorie_limit is not None:
        return f"{nut.calories:g} 千卡，离你 {pref.calorie_limit:g} 千卡上限还有余量"
    return f"{nut.calories:g} 千卡 / 蛋白质 {nut.protein:g} 克"


# ---------- 打分器 ----------


class DishScorer:
    """把候选菜排成一个推荐列表。

    与 :class:`DishRepository` 分工明确：仓库决定「哪些能吃」，
    打分器决定「先吃哪个」。两者都不依赖大模型，是系统的确定性内核。
    """

    def __init__(
        self,
        weights: ScoreWeights | None = None,
        crowd_levels: dict[str, int] | None = None,
    ):
        # weights 为 None 表示每次按偏好动态挑一套，见 pick_weights
        self._weights = weights
        self._crowd_levels = crowd_levels or {}

    @classmethod
    def from_repository(
        cls, repo: DishRepository, weights: ScoreWeights | None = None
    ) -> DishScorer:
        """从仓库读出各食堂拥挤度，便利度评分会用到。"""
        crowd = {c.name: c.crowd_level for c in repo.canteens()}
        return cls(weights=weights, crowd_levels=crowd)

    def score_dish(self, dish: Dish, pref: UserPreference) -> Recommendation:
        """给单道菜打分，返回带明细和规则理由的推荐项。"""
        weights = self._weights or pick_weights(pref)

        raw = {
            "flavor": score_flavor(dish, pref),
            "cuisine": score_cuisine(dish, pref),
            "price": score_price(dish, pref),
            "nutrition": score_nutrition(dish, pref),
            "popularity": score_popularity(dish),
            "convenience": score_convenience(dish, pref, self._crowd_levels),
        }

        breakdown = ScoreBreakdown(
            flavor=round(raw["flavor"] * weights.flavor, 2),
            cuisine=round(raw["cuisine"] * weights.cuisine, 2),
            price=round(raw["price"] * weights.price, 2),
            nutrition=round(raw["nutrition"] * weights.nutrition, 2),
            popularity=round(raw["popularity"] * weights.popularity, 2),
            convenience=round(raw["convenience"] * weights.convenience, 2),
        )

        return Recommendation(
            dish=dish,
            score=round(breakdown.total(), 2),
            breakdown=breakdown,
            reasons=build_reasons(dish, pref, raw),
        )

    def rank(
        self, dishes: list[Dish], pref: UserPreference, limit: int | None = None
    ) -> list[Recommendation]:
        """给候选菜排序。

        并列时依次按销量、评分、菜品 id 兜底，保证同样输入永远得到同样顺序：
        顺序不稳定的推荐既没法调参也没法写断言。
        """
        results = [self.score_dish(d, pref) for d in dishes]
        results.sort(
            key=lambda r: (-r.score, -r.dish.popularity, -r.dish.rating, r.dish.id)
        )
        return results[:limit] if limit else results

    def rank_diverse(
        self,
        dishes: list[Dish],
        pref: UserPreference,
        limit: int = 5,
        max_per_window: int = 2,
    ) -> list[Recommendation]:
        """带多样性约束的排序。

        同一窗口最多出 ``max_per_window`` 道：一次推荐全来自同一个窗口，
        分数再高体验也差。名额没填满时，再从被压下的菜里按分数补齐。
        """
        ranked = self.rank(dishes, pref)

        picked: list[Recommendation] = []
        deferred: list[Recommendation] = []
        seen: dict[tuple[str, str], int] = {}

        for rec in ranked:
            key = (rec.dish.canteen, rec.dish.window)
            if seen.get(key, 0) >= max_per_window:
                deferred.append(rec)
                continue
            seen[key] = seen.get(key, 0) + 1
            picked.append(rec)
            if len(picked) >= limit:
                return picked

        picked.extend(deferred[: max(0, limit - len(picked))])
        return picked


@lru_cache(maxsize=1)
def get_scorer() -> DishScorer:
    """全局单例，FastAPI 依赖注入用。"""
    return DishScorer.from_repository(get_repository())
