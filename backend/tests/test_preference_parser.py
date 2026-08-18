"""偏好解析的测试。

关键词规则部分是纯函数，直接断言。
模型部分用假客户端注入，重点验证降级路径和「脏输出也能救回来」。
"""

from __future__ import annotations

import httpx
import pytest

from app.agent.preference_parser import (
    PreferenceParser,
    build_preference,
    extract_json,
    parse_by_keywords,
)
from app.config import Settings
from app.models.enums import Allergen, Cuisine, DietaryTag, Flavor, MealPeriod
from app.services.qianfan_client import QianfanClient

TOKEN_BODY = {"access_token": "tk-test", "expires_in": 2592000}


def make_client(model_output: str | None = None, fail: bool = False) -> QianfanClient:
    """造一个假千帆客户端：要么固定返回一段文本，要么固定失败。"""
    settings = Settings(
        qianfan_ak="ak",
        qianfan_sk="sk",
        qianfan_retry_backoff=0.0,
        qianfan_max_retries=0,
        _env_file=None,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if "oauth" in request.url.path:
            return httpx.Response(200, json=TOKEN_BODY)
        if fail:
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json={"result": model_output})

    http = httpx.AsyncClient(
        base_url=settings.qianfan_base_url, transport=httpx.MockTransport(handler)
    )
    return QianfanClient(settings=settings, client=http)


class TestKeywordFlavors:
    def test_liked_and_disliked_split(self):
        pref = parse_by_keywords("想吃点辣的，不要太甜")
        assert Flavor.SPICY in pref.liked_flavors
        assert Flavor.SWEET in pref.disliked_flavors

    def test_negation_before_keyword(self):
        pref = parse_by_keywords("不吃辣，清淡点")
        assert Flavor.SPICY in pref.disliked_flavors
        assert Flavor.LIGHT in pref.liked_flavors

    def test_seafood_does_not_become_umami_flavor(self):
        """「海鲜」里的「鲜」是食材名不是口味诉求。"""
        pref = parse_by_keywords("海鲜过敏，想吃鸡蛋")
        assert Flavor.UMAMI not in pref.liked_flavors

    def test_umami_still_detected_when_genuine(self):
        assert Flavor.UMAMI in parse_by_keywords("想吃点鲜香的").liked_flavors

    def test_low_sugar_does_not_become_sweet_preference(self):
        """「控糖」不代表想吃甜的，正好相反。"""
        pref = parse_by_keywords("控糖，想吃点清淡的")
        assert Flavor.SWEET not in pref.liked_flavors
        assert DietaryTag.LOW_SUGAR in pref.dietary_tags

    def test_sesame_does_not_become_numbing(self):
        assert Flavor.NUMBING not in parse_by_keywords("多加点芝麻").liked_flavors

    def test_numbing_detected_in_mala(self):
        assert Flavor.NUMBING in parse_by_keywords("想吃麻辣香锅").liked_flavors


class TestKeywordSpicy:
    def test_mild_maps_to_two(self):
        assert parse_by_keywords("微辣就行").spicy_tolerance == 2

    def test_no_spicy_maps_to_zero(self):
        assert parse_by_keywords("不吃辣").spicy_tolerance == 0
        assert parse_by_keywords("怕辣").spicy_tolerance == 0

    def test_extra_spicy_maps_to_five(self):
        assert parse_by_keywords("越辣越好").spicy_tolerance == 5

    def test_plain_spicy_sets_no_cap(self):
        """只说「想吃辣的」没说程度，不该设上限把特辣菜挡掉。"""
        pref = parse_by_keywords("想吃辣的")
        assert pref.spicy_tolerance is None
        assert Flavor.SPICY in pref.liked_flavors


class TestKeywordBudget:
    def test_explicit_cap(self):
        assert parse_by_keywords("20块以内").budget_max == 20
        assert parse_by_keywords("不超过15元").budget_max == 15
        assert parse_by_keywords("预算12块").budget_max == 12

    def test_cheap_words_map_to_cap(self):
        assert parse_by_keywords("便宜点就行").budget_max == 10

    def test_explicit_floor(self):
        assert parse_by_keywords("15块以上").budget_min == 15
        assert parse_by_keywords("至少20元").budget_min == 20

    def test_treat_words_map_to_floor(self):
        assert parse_by_keywords("今天想吃好点").budget_min == 15

    def test_wait_minutes_not_parsed_as_budget(self):
        """回归：「排队不超过10分钟」曾被误解析成预算 10 元。"""
        pref = parse_by_keywords("二食堂有什么好吃的，排队不超过10分钟")
        assert pref.budget_max is None
        assert pref.max_wait_minutes == 10

    def test_protein_grams_not_parsed_as_budget(self):
        """回归：「蛋白质至少30克」曾被误解析成最低预算 30 元。"""
        pref = parse_by_keywords("健身完，蛋白质至少30克")
        assert pref.budget_min is None
        assert pref.min_protein == 30

    def test_calories_not_parsed_as_budget(self):
        pref = parse_by_keywords("600大卡以内")
        assert pref.budget_max is None
        assert pref.calorie_limit == 600

    def test_inverted_range_drops_floor(self):
        """下限高于上限说明解析拧了，以上限为准。"""
        pref = parse_by_keywords("10块以内，至少20元")
        assert pref.budget_max == 10
        assert pref.budget_min is None


class TestKeywordAllergens:
    def test_allergen_requires_explicit_marker(self):
        """光提到食材名不算过敏，否则「想吃虾」会把海鲜全滤掉。"""
        assert parse_by_keywords("想吃虾").avoid_allergens == []
        assert Allergen.SEAFOOD in parse_by_keywords("海鲜过敏").avoid_allergens

    def test_marker_scoped_to_same_clause(self):
        """「海鲜过敏，想吃鸡蛋」不该把蛋类也当过敏原。"""
        pref = parse_by_keywords("海鲜过敏，想吃鸡蛋")
        assert Allergen.SEAFOOD in pref.avoid_allergens
        assert Allergen.EGG not in pref.avoid_allergens

    def test_intolerance_recognised(self):
        assert Allergen.MILK in parse_by_keywords("牛奶不耐受").avoid_allergens

    def test_cannot_eat_phrasing(self):
        assert Allergen.PEANUT in parse_by_keywords("不能吃花生").avoid_allergens


class TestKeywordOthers:
    def test_dietary_tags(self):
        assert DietaryTag.LOW_FAT in parse_by_keywords("最近在减脂").dietary_tags
        assert DietaryTag.HIGH_PROTEIN in parse_by_keywords("健身增肌").dietary_tags
        assert DietaryTag.VEGETARIAN in parse_by_keywords("我吃素").dietary_tags
        assert DietaryTag.HALAL in parse_by_keywords("要清真的").dietary_tags

    def test_disliked_ingredients(self):
        pref = parse_by_keywords("别放葱和香菜")
        assert "香菜" in pref.disliked_ingredients
        assert "葱" in pref.disliked_ingredients

    def test_ingredient_without_negation_ignored(self):
        """「想吃羊肉」不该被当成忌口。"""
        assert parse_by_keywords("想吃羊肉") .disliked_ingredients == []

    def test_cuisine_and_meal_period(self):
        pref = parse_by_keywords("中午想吃川菜")
        assert Cuisine.SICHUAN in pref.liked_cuisines
        assert pref.meal_period == MealPeriod.LUNCH

    def test_canteen_extraction(self):
        pref = parse_by_keywords("二食堂和三食堂都行")
        assert pref.preferred_canteens == ["二食堂", "三食堂"]

    def test_empty_text_gives_empty_preference(self):
        assert parse_by_keywords("").is_empty()
        assert parse_by_keywords("   ").is_empty()

    def test_raw_text_preserved(self):
        text = "想吃辣的"
        assert parse_by_keywords(text).raw_text == text

    def test_composite_sentence(self):
        """一句话里塞满条件，各维度都要解析到。"""
        pref = parse_by_keywords("想吃点辣的，20块以内，最近减脂，不要香菜，二食堂")
        assert Flavor.SPICY in pref.liked_flavors
        assert pref.budget_max == 20
        assert DietaryTag.LOW_FAT in pref.dietary_tags
        assert "香菜" in pref.disliked_ingredients
        assert pref.preferred_canteens == ["二食堂"]


class TestJSONExtraction:
    def test_plain_json(self):
        assert extract_json('{"liked_flavors":["辣"]}') == {"liked_flavors": ["辣"]}

    def test_code_block_wrapped(self):
        raw = '```json\n{"budget_max": 15}\n```'
        assert extract_json(raw) == {"budget_max": 15}

    def test_leading_chatter_stripped(self):
        raw = '好的，解析结果如下：\n{"budget_max": 15}\n希望有帮助'
        assert extract_json(raw) == {"budget_max": 15}

    def test_no_json_raises(self):
        with pytest.raises(ValueError):
            extract_json("我不明白你的意思")

    def test_malformed_json_raises(self):
        with pytest.raises(ValueError):
            extract_json('{"budget_max": }')

    def test_array_rejected(self):
        with pytest.raises(ValueError):
            extract_json("[1, 2, 3]")


class TestBuildPreference:
    def test_unknown_fields_dropped(self):
        """模型多给的字段直接丢掉，不能让整条请求失败。"""
        pref = build_preference({"liked_flavors": ["辣"], "mood": "开心"})
        assert Flavor.SPICY in pref.liked_flavors

    def test_illegal_enum_value_salvaged(self):
        """一个词不在词表里，其他字段还得救回来。"""
        pref = build_preference({"liked_flavors": ["spicy"], "budget_max": 15})
        assert pref.budget_max == 15
        assert pref.liked_flavors == []

    def test_out_of_range_value_dropped(self):
        pref = build_preference({"spicy_tolerance": 99, "budget_max": 12})
        assert pref.spicy_tolerance is None
        assert pref.budget_max == 12

    def test_null_values_ignored(self):
        pref = build_preference({"budget_max": None, "liked_flavors": ["辣"]})
        assert pref.budget_max is None
        assert Flavor.SPICY in pref.liked_flavors

    def test_raw_text_attached(self):
        pref = build_preference({"budget_max": 10}, raw_text="便宜点")
        assert pref.raw_text == "便宜点"

    def test_inverted_budget_range_salvaged(self):
        """budget_min > budget_max 会触发模型校验器报错，得剔掉冲突字段而不是整体失败。"""
        pref = build_preference({"budget_min": 30, "budget_max": 10})
        assert pref.budget_max == 10 or pref.budget_min == 30
        # 两个都保留就说明校验器没生效
        assert not (pref.budget_min == 30 and pref.budget_max == 10)


class TestParserFallback:
    async def test_model_path_used_when_available(self):
        client = make_client('{"liked_flavors":["辣"],"budget_max":15}')
        pref, fallback = await PreferenceParser(client).parse("想吃辣的，别太贵")
        assert not fallback, "模型可用时不该标记降级"
        assert pref.budget_max == 15

    async def test_model_failure_falls_back_to_keywords(self):
        client = make_client(fail=True)
        pref, fallback = await PreferenceParser(client).parse("想吃辣的，20块以内")
        assert fallback, "模型失败必须标记降级"
        assert Flavor.SPICY in pref.liked_flavors
        assert pref.budget_max == 20

    async def test_garbage_output_falls_back(self):
        """模型答非所问时降级，不能让请求失败。"""
        client = make_client("今天天气不错")
        pref, fallback = await PreferenceParser(client).parse("不吃辣")
        assert fallback
        assert pref.spicy_tolerance == 0

    async def test_no_client_uses_keywords(self):
        pref, fallback = await PreferenceParser(None).parse("微辣")
        assert fallback
        assert pref.spicy_tolerance == 2

    async def test_unconfigured_client_uses_keywords(self):
        """没配密钥的客户端 available 为 False，应直接走规则不发请求。"""
        settings = Settings(qianfan_ak="", qianfan_sk="", _env_file=None)
        client = QianfanClient(settings=settings)
        pref, fallback = await PreferenceParser(client).parse("便宜点")
        assert fallback
        assert pref.budget_max == 10

    async def test_empty_text_not_marked_fallback(self):
        """空输入本来就没什么可解析的，不该报降级。"""
        pref, fallback = await PreferenceParser(None).parse("")
        assert not fallback
        assert pref.is_empty()

    async def test_raw_text_survives_model_path(self):
        client = make_client('{"budget_max":15}')
        pref, _ = await PreferenceParser(client).parse("别太贵")
        assert pref.raw_text == "别太贵"
