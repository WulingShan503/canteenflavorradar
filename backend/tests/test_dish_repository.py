"""菜品仓库与硬性过滤的测试。"""

from app.models.enums import Allergen, Category, DietaryTag, Flavor, MealPeriod
from app.models.preference import UserPreference
from app.services.dish_repository import DishRepository


class TestLoading:
    def test_sample_data_loads(self, repo: DishRepository):
        dishes = repo.all_dishes(include_unavailable=True)
        assert len(dishes) == 30
        assert len({d.id for d in dishes}) == 30, "菜品 id 必须唯一"

    def test_unavailable_excluded_by_default(self, repo: DishRepository):
        assert all(d.available for d in repo.all_dishes())
        assert len(repo.all_dishes()) < len(repo.all_dishes(include_unavailable=True))

    def test_every_dish_belongs_to_registered_canteen(self, repo: DishRepository):
        registered = {c.name for c in repo.canteens()}
        for dish in repo.all_dishes(include_unavailable=True):
            assert dish.canteen in registered

    def test_get_and_search(self, repo: DishRepository):
        assert repo.get("D1001") is not None
        assert repo.get("D9999") is None
        assert any(d.name == "麻婆豆腐" for d in repo.search("豆腐"))
        assert repo.search("   ") == []


class TestHardFilters:
    def test_allergen_never_returned(self, repo: DishRepository):
        pref = UserPreference(avoid_allergens=[Allergen.PEANUT, Allergen.SEAFOOD])
        candidates, _ = repo.find_candidates(pref)
        assert candidates, "过敏原过滤后不应为空"
        for dish in candidates:
            assert Allergen.PEANUT not in dish.allergens
            assert Allergen.SEAFOOD not in dish.allergens

    def test_dietary_tags_all_required(self, repo: DishRepository):
        pref = UserPreference(
            dietary_tags=[DietaryTag.VEGAN, DietaryTag.LOW_FAT]
        )
        candidates, _ = repo.find_candidates(pref)
        for dish in candidates:
            assert DietaryTag.VEGAN in dish.dietary_tags
            assert DietaryTag.LOW_FAT in dish.dietary_tags

    def test_disliked_ingredient_filtered(self, repo: DishRepository):
        pref = UserPreference(disliked_ingredients=["香菜"])
        candidates, _ = repo.find_candidates(pref)
        names = {d.name for d in candidates}
        assert "酸辣粉" not in names
        assert "兰州牛肉面" not in names

    def test_spicy_tolerance_respected(self, repo: DishRepository):
        pref = UserPreference(spicy_tolerance=0)
        candidates, notes = repo.find_candidates(pref)
        assert notes == [], "不辣的菜够多，不该触发放宽"
        assert all(d.spicy_level == 0 for d in candidates)

    def test_budget_cap_respected(self, repo: DishRepository):
        pref = UserPreference(budget_max=10.0)
        candidates, notes = repo.find_candidates(pref)
        assert notes == []
        assert all(d.price <= 10.0 for d in candidates)

    def test_meal_period_respected(self, repo: DishRepository):
        pref = UserPreference(meal_period=MealPeriod.BREAKFAST)
        candidates, _ = repo.find_candidates(pref)
        assert candidates
        for dish in candidates:
            assert MealPeriod.BREAKFAST in dish.meal_periods

    def test_category_filter(self, repo: DishRepository):
        pref = UserPreference(categories=[Category.SOUP])
        candidates, _ = repo.find_candidates(pref, min_results=1)
        assert all(d.category == Category.SOUP for d in candidates)


class TestRelaxation:
    def test_over_strict_conditions_get_relaxed(self, repo: DishRepository):
        """预算 3 元 + 只要套餐，本来一条都没有，应放宽后给出结果并说明。"""
        pref = UserPreference(budget_max=3.0, categories=[Category.COMBO])
        candidates, notes = repo.find_candidates(pref, min_results=3)
        assert candidates, "放宽后应该能给出候选"
        assert notes, "放宽时必须告知用户"

    def test_relaxation_never_breaks_allergen_rule(self, repo: DishRepository):
        """条件苛刻到需要放宽预算和辣度时，过敏原依然是红线。"""
        pref = UserPreference(
            avoid_allergens=[Allergen.SEAFOOD, Allergen.GLUTEN, Allergen.MILK],
            budget_max=6.0,
            spicy_tolerance=0,
            calorie_limit=200,
        )
        candidates, notes = repo.find_candidates(pref, min_results=5)
        assert candidates, "放宽后应该能给出候选，否则这个测试断言不到东西"
        assert notes, "触发了放宽就必须有说明"
        for dish in candidates:
            assert Allergen.SEAFOOD not in dish.allergens
            assert Allergen.GLUTEN not in dish.allergens
            assert Allergen.MILK not in dish.allergens

    def test_unset_conditions_not_reported_as_relaxed(self, repo: DishRepository):
        """用户没设排队时长，就不该出现「放宽了排队时长」这种莫名提示。"""
        pref = UserPreference(categories=[Category.COMBO], budget_max=3.0)
        _, notes = repo.find_candidates(pref, min_results=3)
        assert "放宽了排队时长要求" not in notes

    def test_ineffective_relaxation_not_reported(self, repo: DishRepository):
        """放宽了但一道菜都没多筛出来时，不能谎报已放宽。

        素食里 3 元以内只有 2 道，上调到 3.6 元还是那 2 道。
        这时提示「已上调预算上限」会让用户以为系统给了更贵的选择，
        对着结果一看全是原价位，反而像出了故障。
        """
        pref = UserPreference(dietary_tags=[DietaryTag.VEGETARIAN], budget_max=3.0)
        candidates, notes = repo.find_candidates(pref, min_results=5)
        assert candidates, "至少还有白米饭和豆浆"
        assert "略微上调了预算上限" not in notes

    def test_effective_relaxation_still_reported(self, repo: DishRepository):
        """真的靠放宽多筛出菜时，必须照实告知。"""
        pref = UserPreference(categories=[Category.COMBO], budget_max=3.0)
        candidates, notes = repo.find_candidates(pref, min_results=3)
        assert candidates
        assert notes, "放宽确实起作用了就得说明"


class TestPreferenceModel:
    def test_conflicting_flavor_resolved_to_liked(self):
        pref = UserPreference(
            liked_flavors=[Flavor.SPICY], disliked_flavors=[Flavor.SPICY, Flavor.SWEET]
        )
        assert pref.disliked_flavors == [Flavor.SWEET]

    def test_empty_preference_detected(self):
        assert UserPreference().is_empty()
        assert not UserPreference(liked_flavors=[Flavor.SPICY]).is_empty()
        assert not UserPreference(spicy_tolerance=0).is_empty()

    def test_invalid_budget_range_rejected(self):
        import pytest

        with pytest.raises(ValueError):
            UserPreference(budget_min=30, budget_max=10)

    def test_protein_ratio(self, repo: DishRepository):
        salad = repo.get("D1021")
        assert salad is not None
        assert salad.protein_ratio() > 0.4, "鸡胸沙拉应是高蛋白占比"
