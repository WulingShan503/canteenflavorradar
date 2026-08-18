"""打分排序器的测试。

覆盖点：权重体系自洽、各维度单调性与中性处理、排序稳定性、
多样性约束、理由生成，以及「打分层绝不改变过滤层结论」这条分层约定。
"""

import pytest

from app.models.enums import Category, Cuisine, DietaryTag, Flavor
from app.models.preference import UserPreference
from app.services.dish_repository import DishRepository
from app.services.scorer import (
    NEUTRAL,
    NUTRITION_FIRST_WEIGHTS,
    POPULAR_FALLBACK_WEIGHTS,
    DishScorer,
    ScoreWeights,
    pick_weights,
    score_convenience,
    score_cuisine,
    score_flavor,
    score_popularity,
    score_price,
)


@pytest.fixture(scope="session")
def scorer(repo: DishRepository) -> DishScorer:
    return DishScorer.from_repository(repo)


class TestWeights:
    def test_all_weight_sets_sum_to_100(self):
        """总分要落在 0-100，三套权重必须都是满分 100。"""
        for weights in (
            ScoreWeights(),
            NUTRITION_FIRST_WEIGHTS,
            POPULAR_FALLBACK_WEIGHTS,
        ):
            assert weights.total() == pytest.approx(100.0)

    def test_empty_preference_uses_popular_fallback(self):
        assert pick_weights(UserPreference()) is POPULAR_FALLBACK_WEIGHTS

    def test_nutrition_goal_switches_weights(self):
        assert pick_weights(UserPreference(calorie_limit=500)) is NUTRITION_FIRST_WEIGHTS
        assert pick_weights(UserPreference(min_protein=25)) is NUTRITION_FIRST_WEIGHTS
        assert (
            pick_weights(UserPreference(dietary_tags=[DietaryTag.HIGH_PROTEIN]))
            is NUTRITION_FIRST_WEIGHTS
        )

    def test_plain_taste_preference_uses_default_weights(self):
        """只提口味不该切到营养优先那套权重。"""
        weights = pick_weights(UserPreference(liked_flavors=[Flavor.SPICY]))
        assert weights.flavor == ScoreWeights().flavor

    def test_vegetarian_alone_is_not_a_nutrition_goal(self):
        """素食是饮食限制而非营养目标，不该顶高营养权重。"""
        weights = pick_weights(UserPreference(dietary_tags=[DietaryTag.VEGETARIAN]))
        assert weights.nutrition == ScoreWeights().nutrition


class TestDimensionScores:
    def test_unstated_preference_is_neutral(self, repo: DishRepository):
        """用户没提的维度按中性分处理，不能变成惩罚。"""
        dish = repo.get("D1001")
        blank = UserPreference()
        assert score_flavor(dish, blank) == NEUTRAL
        assert score_cuisine(dish, blank) == NEUTRAL
        assert score_price(dish, blank) == NEUTRAL

    def test_liked_flavor_beats_unrelated_dish(self, repo: DishRepository):
        pref = UserPreference(liked_flavors=[Flavor.SPICY, Flavor.NUMBING])
        spicy = repo.get("D1001")  # 麻婆豆腐，辣+麻，两个都命中
        light = repo.get("D1021")  # 香煎鸡胸沙拉，清淡+酸，一个都不命中
        assert score_flavor(spicy, pref) == 1.0
        assert score_flavor(light, pref) < score_flavor(spicy, pref)

    def test_disliked_flavor_penalised(self, repo: DishRepository):
        spicy = repo.get("D1001")
        disliked = UserPreference(disliked_flavors=[Flavor.SPICY])
        assert score_flavor(spicy, disliked) < NEUTRAL

    def test_spicy_fit_prefers_closer_to_tolerance(self, repo: DishRepository):
        """能吃辣的人应该拿到辣菜，而不是被推一堆不辣的。"""
        pref = UserPreference(spicy_tolerance=4)
        mild = next(d for d in repo.all_dishes() if d.spicy_level == 0)
        hot = repo.get("D1001")  # 辣度 4
        assert score_flavor(hot, pref) > score_flavor(mild, pref)

    def test_cuisine_hit_is_full_score(self, repo: DishRepository):
        pref = UserPreference(liked_cuisines=[Cuisine.SICHUAN])
        sichuan = repo.get("D1001")
        assert score_cuisine(sichuan, pref) == 1.0
        other = next(d for d in repo.all_dishes() if d.cuisine != Cuisine.SICHUAN)
        assert score_cuisine(other, pref) < 1.0

    def test_price_within_range_is_full_score(self, repo: DishRepository):
        pref = UserPreference(budget_min=6, budget_max=12)
        inside = next(d for d in repo.all_dishes() if 6 <= d.price <= 12)
        assert score_price(inside, pref) == 1.0

    def test_budget_min_downweights_cheap_dish_but_keeps_it(self, repo: DishRepository):
        """过滤层故意不拦 budget_min，这层只降权，不能把便宜菜打成 0。"""
        pref = UserPreference(budget_min=15)
        cheap = min(repo.all_dishes(), key=lambda d: d.price)
        cheap_score = score_price(cheap, pref)
        assert 0 < cheap_score < 1.0

    def test_popularity_shrinks_low_count_ratings(self, repo: DishRepository):
        """评价人数少的高分不该压过人数多的同等分菜。"""
        dishes = repo.all_dishes()
        top = max(dishes, key=lambda d: d.popularity)
        assert 0.0 <= score_popularity(top) <= 1.0
        assert score_popularity(top) > 0.5

    def test_convenience_prefers_shorter_queue(self, repo: DishRepository):
        pref = UserPreference()
        dishes = repo.all_dishes()
        quick = min(dishes, key=lambda d: d.wait_minutes)
        slow = max(dishes, key=lambda d: d.wait_minutes)
        assert score_convenience(quick, pref) > score_convenience(slow, pref)

    def test_preferred_canteen_boosts_convenience(self, repo: DishRepository):
        dish = repo.get("D1001")
        liked = UserPreference(preferred_canteens=[dish.canteen])
        disliked = UserPreference(preferred_canteens=["三食堂"])
        assert score_convenience(dish, liked) > score_convenience(dish, disliked)


class TestScoreDish:
    def test_score_in_range_and_matches_breakdown(
        self, repo: DishRepository, scorer: DishScorer
    ):
        pref = UserPreference(liked_flavors=[Flavor.SPICY], budget_max=12)
        for dish in repo.all_dishes():
            rec = scorer.score_dish(dish, pref)
            assert 0.0 <= rec.score <= 100.0
            assert rec.score == pytest.approx(rec.breakdown.total(), abs=0.01)

    def test_breakdown_dimensions_never_exceed_weight(
        self, repo: DishRepository, scorer: DishScorer
    ):
        """每一维得分不得超过它的权重上限，否则说明原始分越界了。"""
        pref = UserPreference(
            liked_flavors=[Flavor.SPICY], spicy_tolerance=5, budget_max=20
        )
        weights = pick_weights(pref)
        for dish in repo.all_dishes():
            b = scorer.score_dish(dish, pref).breakdown
            assert b.flavor <= weights.flavor + 0.01
            assert b.cuisine <= weights.cuisine + 0.01
            assert b.price <= weights.price + 0.01
            assert b.nutrition <= weights.nutrition + 0.01
            assert b.popularity <= weights.popularity + 0.01
            assert b.convenience <= weights.convenience + 0.01

    def test_reasons_generated_for_good_match(
        self, repo: DishRepository, scorer: DishScorer
    ):
        pref = UserPreference(
            liked_flavors=[Flavor.SPICY, Flavor.NUMBING],
            liked_cuisines=[Cuisine.SICHUAN],
            budget_max=12,
        )
        rec = scorer.score_dish(repo.get("D1001"), pref)
        assert rec.reasons, "高度匹配的菜必须给出理由"
        assert len(rec.reasons) <= 4

    def test_comment_left_empty_for_rule_layer(
        self, repo: DishRepository, scorer: DishScorer
    ):
        """推荐语是大模型的活，规则层不许往里写东西。"""
        rec = scorer.score_dish(repo.get("D1001"), UserPreference())
        assert rec.comment == ""


class TestRanking:
    def test_rank_is_descending_and_deterministic(
        self, repo: DishRepository, scorer: DishScorer
    ):
        pref = UserPreference(liked_flavors=[Flavor.SPICY], budget_max=15)
        candidates, _ = repo.find_candidates(pref)
        first = scorer.rank(candidates, pref)
        second = scorer.rank(candidates, pref)

        scores = [r.score for r in first]
        assert scores == sorted(scores, reverse=True)
        assert [r.dish.id for r in first] == [r.dish.id for r in second], "排序必须可复现"

    def test_limit_respected(self, repo: DishRepository, scorer: DishScorer):
        pref = UserPreference()
        ranked = scorer.rank(repo.all_dishes(), pref, limit=5)
        assert len(ranked) == 5

    def test_spicy_lover_gets_spicy_dish_on_top(
        self, repo: DishRepository, scorer: DishScorer
    ):
        pref = UserPreference(
            liked_flavors=[Flavor.SPICY], spicy_tolerance=5, raw_text="想吃点辣的"
        )
        candidates, _ = repo.find_candidates(pref)
        top = scorer.rank(candidates, pref, limit=3)
        assert any(Flavor.SPICY in r.dish.flavors for r in top)
        assert top[0].dish.spicy_level > 0

    def test_ranking_never_resurrects_filtered_dishes(
        self, repo: DishRepository, scorer: DishScorer
    ):
        """分层红线：打分层只能排序候选集，不能把被过滤掉的菜捞回来。"""
        pref = UserPreference(disliked_ingredients=["香菜"], budget_max=10)
        candidates, _ = repo.find_candidates(pref)
        allowed = {d.id for d in candidates}
        ranked = scorer.rank(candidates, pref)
        assert {r.dish.id for r in ranked} == allowed

    def test_unavailable_dish_never_ranked(
        self, repo: DishRepository, scorer: DishScorer
    ):
        pref = UserPreference()
        candidates, _ = repo.find_candidates(pref)
        assert all(r.dish.available for r in scorer.rank(candidates, pref))

    def test_diverse_ranking_caps_same_window(
        self, repo: DishRepository, scorer: DishScorer
    ):
        pref = UserPreference()
        picked = scorer.rank_diverse(repo.all_dishes(), pref, limit=6, max_per_window=2)
        assert len(picked) == 6
        counts: dict[tuple[str, str], int] = {}
        for rec in picked:
            key = (rec.dish.canteen, rec.dish.window)
            counts[key] = counts.get(key, 0) + 1
        assert max(counts.values()) <= 2

    def test_diverse_ranking_fills_quota_when_windows_run_out(
        self, repo: DishRepository, scorer: DishScorer
    ):
        """窗口数不够时也要凑满名额，不能因为多样性约束少给结果。"""
        pref = UserPreference(categories=[Category.SOUP])
        candidates, _ = repo.find_candidates(pref, min_results=1)
        picked = scorer.rank_diverse(candidates, pref, limit=3, max_per_window=1)
        assert len(picked) == min(3, len(candidates))


class TestNutritionScenario:
    def test_cutting_scenario_prefers_low_calorie_high_protein(
        self, repo: DishRepository, scorer: DishScorer
    ):
        """减脂场景：热量上限 + 高蛋白，排第一的应该确实又轻又顶。"""
        pref = UserPreference(
            dietary_tags=[DietaryTag.HIGH_PROTEIN],
            calorie_limit=400,
            min_protein=20,
            raw_text="最近在减脂，想吃高蛋白低热量的",
        )
        candidates, _ = repo.find_candidates(pref)
        assert candidates
        top = scorer.rank(candidates, pref, limit=3)
        assert top[0].dish.nutrition.calories <= 400
        assert top[0].breakdown.nutrition > 0
        assert any("蛋白质" in r for r in top[0].reasons)

    def test_empty_preference_ranks_by_popularity(
        self, repo: DishRepository, scorer: DishScorer
    ):
        """没有任何偏好时走热门兜底，口味/菜系/营养维度都应为 0 分。"""
        pref = UserPreference()
        top = scorer.rank(repo.all_dishes(), pref, limit=3)
        assert top[0].breakdown.flavor == 0.0
        assert top[0].breakdown.cuisine == 0.0
        assert top[0].breakdown.nutrition == 0.0
        # 不逐名比销量：便利度也占 28 分权重，销量最高的菜可能因为排队久被压下去
        ranked = scorer.rank(repo.all_dishes(), pref)
        head = sum(r.dish.popularity for r in ranked[:3]) / 3
        tail = sum(r.dish.popularity for r in ranked[-3:]) / 3
        assert head > tail, "热门兜底下头部的销量应显著高于尾部"
