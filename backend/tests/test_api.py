"""HTTP 接口测试。

用 FastAPI 的 TestClient 直接打路由，不起真服务器。
千帆全程不可用（不注入客户端），所以测的是纯规则模式下的接口行为——
这也正是评审环境最可能的状态。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.agent.orchestrator import RecommendAgent, get_agent
from app.api.schemas import MAX_LIMIT, MAX_TEXT_LENGTH
from app.main import create_app
from app.services.dish_repository import DishRepository, get_repository
from app.services.scorer import DishScorer


@pytest.fixture(scope="module")
def client(repo: DishRepository) -> TestClient:
    """造一个不依赖千帆的测试客户端。"""
    app = create_app()

    def _repo() -> DishRepository:
        return repo

    def _agent() -> RecommendAgent:
        return RecommendAgent(
            repo=repo, scorer=DishScorer.from_repository(repo), client=None
        )

    app.dependency_overrides[get_repository] = _repo
    app.dependency_overrides[get_agent] = _agent
    with TestClient(app) as test_client:
        yield test_client


class TestHealth:
    def test_health_ok(self, client: TestClient):
        resp = client.get("/api/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["dish_count"] > 0

    def test_health_reports_mode(self, client: TestClient):
        """要能一眼看出是纯规则模式还是模型可用。"""
        body = client.get("/api/health").json()
        assert body["mode"] in ("full", "rule-only")
        assert isinstance(body["qianfan_configured"], bool)

    def test_root_gives_pointers(self, client: TestClient):
        body = client.get("/").json()
        assert body["docs"] == "/docs"
        assert body["health"] == "/api/health"

    def test_openapi_schema_generated(self, client: TestClient):
        """schema 生成不了说明有响应模型写错了，值得单独守一条。"""
        resp = client.get("/openapi.json")
        assert resp.status_code == 200
        assert "/api/recommend" in resp.json()["paths"]


class TestRecommend:
    def test_natural_language_request(self, client: TestClient):
        resp = client.post(
            "/api/recommend", json={"text": "想吃点辣的，20块以内", "limit": 3}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["recommendations"]) == 3
        assert body["fallback_used"] is True, "没有千帆，必须标记降级"

    def test_every_recommendation_is_complete(self, client: TestClient):
        """前端要用的字段一个都不能少。"""
        body = client.post("/api/recommend", json={"text": "想吃辣的"}).json()
        for rec in body["recommendations"]:
            assert rec["dish"]["name"]
            assert rec["dish"]["price"] > 0
            assert 0 <= rec["score"] <= 100
            assert rec["comment"], "推荐语不能为空，规则兜底也得有"
            assert "flavor" in rec["breakdown"]

    def test_structured_preference_request(self, client: TestClient):
        resp = client.post(
            "/api/recommend",
            json={
                "preference": {"liked_flavors": ["辣"], "budget_max": 12},
                "limit": 3,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["recommendations"]

    def test_empty_request_still_returns_dishes(self, client: TestClient):
        """什么都不传也要给结果，走热门兜底。"""
        resp = client.post("/api/recommend", json={})
        assert resp.status_code == 200
        assert resp.json()["recommendations"]

    def test_allergen_filtered_through_api(self, client: TestClient):
        """安全约束在接口层同样生效——这是端到端的红线验证。"""
        resp = client.post(
            "/api/recommend",
            json={"preference": {"avoid_allergens": ["花生"]}, "limit": 10},
        )
        for rec in resp.json()["recommendations"]:
            assert "花生" not in rec["dish"]["allergens"]

    def test_parsed_preference_echoed(self, client: TestClient):
        body = client.post("/api/recommend", json={"text": "20块以内，不吃辣"}).json()
        assert body["parsed_preference"].get("budget_max") == 20

    def test_meal_plan_on_request(self, client: TestClient):
        body = client.post(
            "/api/recommend", json={"text": "随便", "with_meal_plan": True}
        ).json()
        assert body["meal_plan"] is not None
        assert len(body["meal_plan"]["items"]) >= 2

    def test_meal_plan_absent_by_default(self, client: TestClient):
        body = client.post("/api/recommend", json={"text": "随便"}).json()
        assert body["meal_plan"] is None

    def test_limit_upper_bound_enforced(self, client: TestClient):
        """limit 传太大要被挡住，别让人一次拉爆响应。"""
        resp = client.post("/api/recommend", json={"limit": MAX_LIMIT + 1})
        assert resp.status_code == 422
        assert resp.json()["code"] == "invalid_request"

    def test_limit_zero_rejected(self, client: TestClient):
        assert client.post("/api/recommend", json={"limit": 0}).status_code == 422

    def test_overlong_text_rejected(self, client: TestClient):
        resp = client.post(
            "/api/recommend", json={"text": "辣" * (MAX_TEXT_LENGTH + 1)}
        )
        assert resp.status_code == 422

    def test_validation_error_message_is_chinese(self, client: TestClient):
        """校验报错要给中文提示，默认的英文结构对前端不友好。"""
        body = client.post("/api/recommend", json={"limit": 999}).json()
        assert "请求参数有误" in body["detail"]

    def test_illegal_enum_in_preference_rejected(self, client: TestClient):
        resp = client.post(
            "/api/recommend", json={"preference": {"liked_flavors": ["spicy"]}}
        )
        assert resp.status_code == 422

    def test_inverted_budget_rejected(self, client: TestClient):
        resp = client.post(
            "/api/recommend",
            json={"preference": {"budget_min": 30, "budget_max": 10}},
        )
        assert resp.status_code == 422


class TestDishes:
    def test_list_all_available(self, client: TestClient):
        body = client.get("/api/dishes").json()
        assert body["total"] > 0
        assert all(d["available"] for d in body["dishes"])

    def test_include_unavailable(self, client: TestClient):
        with_all = client.get("/api/dishes?include_unavailable=true").json()
        default = client.get("/api/dishes").json()
        assert with_all["total"] > default["total"]

    def test_keyword_search(self, client: TestClient):
        body = client.get("/api/dishes?keyword=豆腐").json()
        assert body["total"] > 0
        assert any("豆腐" in d["name"] for d in body["dishes"])

    def test_keyword_search_excludes_unavailable_by_default(self, client: TestClient):
        """搜索走的是全量数据，可用性过滤别漏掉。

        D1025 芝士焗饭是示例数据里唯一停供的菜，正好用来验证两个方向：
        默认搜不到，显式要求包含停供才搜得到。
        """
        default = client.get("/api/dishes?keyword=焗饭").json()
        assert all(d["available"] for d in default["dishes"])
        assert "D1025" not in [d["id"] for d in default["dishes"]]

        with_unavailable = client.get(
            "/api/dishes?keyword=焗饭&include_unavailable=true"
        ).json()
        assert "D1025" in [d["id"] for d in with_unavailable["dishes"]]

    def test_filter_by_canteen(self, client: TestClient):
        body = client.get("/api/dishes?canteen=二食堂").json()
        assert body["total"] > 0
        assert all(d["canteen"] == "二食堂" for d in body["dishes"])

    def test_filter_by_category(self, client: TestClient):
        body = client.get("/api/dishes?category=汤品").json()
        assert all(d["category"] == "汤品" for d in body["dishes"])

    def test_filter_by_cuisine(self, client: TestClient):
        body = client.get("/api/dishes?cuisine=川菜").json()
        assert all(d["cuisine"] == "川菜" for d in body["dishes"])

    def test_filters_combine_with_and(self, client: TestClient):
        body = client.get("/api/dishes?canteen=一食堂&category=荤菜").json()
        for dish in body["dishes"]:
            assert dish["canteen"] == "一食堂"
            assert dish["category"] == "荤菜"

    def test_illegal_enum_query_rejected(self, client: TestClient):
        assert client.get("/api/dishes?category=火锅").status_code == 422

    def test_get_single_dish(self, client: TestClient):
        body = client.get("/api/dishes/D1001").json()
        assert body["id"] == "D1001"
        assert body["nutrition"]["calories"] > 0

    def test_missing_dish_gives_404(self, client: TestClient):
        resp = client.get("/api/dishes/D9999")
        assert resp.status_code == 404
        assert "D9999" in resp.json()["detail"]


class TestCanteens:
    def test_list_canteens(self, client: TestClient):
        body = client.get("/api/canteens").json()
        assert body["total"] == 3
        assert all(c["name"] for c in body["canteens"])

    def test_canteen_has_crowd_level(self, client: TestClient):
        """便利度打分要用拥挤度，接口得把它带出来。"""
        for canteen in client.get("/api/canteens").json()["canteens"]:
            assert 0 <= canteen["crowd_level"] <= 5
