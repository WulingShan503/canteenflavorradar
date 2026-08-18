"""推荐语生成的测试。

重点：失败必须兜底不能留空、模型漏写的条目单独补、
prompt 里要带上规则理由作为事实依据（这是抑制编造的关键）。
"""

from __future__ import annotations

import httpx
import pytest

from app.agent.comment_writer import (
    MAX_COMMENT_CHARS,
    CommentWriter,
    build_prompt,
    fallback_comment,
    parse_comments,
)
from app.config import Settings
from app.models.enums import Flavor
from app.models.preference import UserPreference
from app.services.dish_repository import DishRepository
from app.services.qianfan_client import QianfanClient
from app.services.scorer import DishScorer

TOKEN_BODY = {"access_token": "tk-test", "expires_in": 2592000}


@pytest.fixture
def recs(repo: DishRepository):
    """造三条真实推荐项。"""
    scorer = DishScorer.from_repository(repo)
    pref = UserPreference(liked_flavors=[Flavor.SPICY], budget_max=15, raw_text="想吃辣的")
    candidates, _ = repo.find_candidates(pref)
    return scorer.rank(candidates, pref, limit=3)


def make_client(model_output: str | None = None, fail: bool = False) -> QianfanClient:
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


class TestPromptBuilding:
    def test_prompt_includes_rule_reasons(self, recs):
        """规则理由必须进 prompt——这是让模型有据可依、少编造的关键。"""
        prompt = build_prompt(recs, "想吃辣的")
        assert "推荐依据" in prompt
        for reason in recs[0].reasons:
            assert reason in prompt

    def test_prompt_includes_dish_facts(self, recs):
        prompt = build_prompt(recs, "")
        dish = recs[0].dish
        assert dish.id in prompt
        assert dish.name in prompt
        assert dish.canteen in prompt

    def test_prompt_omits_scores(self, recs):
        """得分和权重是调参用的，写进 prompt 只会让模型去解释数字。"""
        prompt = build_prompt(recs, "")
        assert "得分" not in prompt
        assert "score" not in prompt.lower()

    def test_prompt_includes_user_words(self, recs):
        assert "想吃辣的" in build_prompt(recs, "想吃辣的")

    def test_prompt_handles_blank_user_text(self, recs):
        prompt = build_prompt(recs, "   ")
        assert "用户原话" not in prompt


class TestParseComments:
    def test_plain_array(self):
        raw = '[{"id":"D1001","comment":"麻辣够味"}]'
        assert parse_comments(raw) == {"D1001": "麻辣够味"}

    def test_code_block_wrapped(self):
        raw = '```json\n[{"id":"D1001","comment":"够味"}]\n```'
        assert parse_comments(raw) == {"D1001": "够味"}

    def test_malformed_entries_skipped(self):
        raw = '[{"id":"D1001","comment":"好"},"垃圾",{"comment":"缺id"}]'
        assert parse_comments(raw) == {"D1001": "好"}

    def test_no_array_raises(self):
        with pytest.raises(ValueError):
            parse_comments("我写不出来")

    def test_object_instead_of_array_raises(self):
        with pytest.raises(ValueError):
            parse_comments('{"id":"D1001"}')


class TestFallbackComment:
    def test_uses_rule_reasons(self, recs):
        rec = recs[0]
        assert rec.reasons, "这条测试需要有理由的推荐项"
        comment = fallback_comment(rec)
        assert rec.reasons[0] in comment
        assert comment.endswith("。")

    def test_works_without_reasons(self, recs):
        rec = recs[0].model_copy(deep=True)
        rec.reasons = []
        comment = fallback_comment(rec)
        assert rec.dish.canteen in comment
        assert comment, "没有理由也不能返回空字符串"


class TestWriteFlow:
    async def test_model_comments_applied(self, recs):
        payload = ",".join(
            f'{{"id":"{r.dish.id}","comment":"这道菜{i}号推荐语"}}'
            for i, r in enumerate(recs)
        )
        client = make_client(f"[{payload}]")
        fallback = await CommentWriter(client).write(recs, "想吃辣的")
        assert not fallback
        assert all(r.comment.startswith("这道菜") for r in recs)

    async def test_model_failure_falls_back_to_reasons(self, recs):
        client = make_client(fail=True)
        fallback = await CommentWriter(client).write(recs, "想吃辣的")
        assert fallback
        assert all(r.comment for r in recs), "兜底后每条都必须有推荐语"

    async def test_partial_output_filled_per_item(self, recs):
        """模型只写了第一条，其余用规则理由补，不整批丢弃。"""
        first = recs[0]
        client = make_client(f'[{{"id":"{first.dish.id}","comment":"模型写的"}}]')
        fallback = await CommentWriter(client).write(recs, "")
        assert fallback, "有条目走了兜底就该标记"
        assert recs[0].comment == "模型写的"
        assert all(r.comment for r in recs[1:])

    async def test_unknown_ids_ignored(self, recs):
        """模型返回了不存在的编号，不该错配到别的菜上。"""
        client = make_client('[{"id":"D9999","comment":"张冠李戴"}]')
        await CommentWriter(client).write(recs, "")
        assert all(r.comment != "张冠李戴" for r in recs)

    async def test_overlong_comment_truncated(self, recs):
        long_text = "很" * 300
        payload = ",".join(
            f'{{"id":"{r.dish.id}","comment":"{long_text}"}}' for r in recs
        )
        client = make_client(f"[{payload}]")
        await CommentWriter(client).write(recs, "")
        assert all(len(r.comment) <= MAX_COMMENT_CHARS for r in recs)

    async def test_no_client_uses_fallback(self, recs):
        fallback = await CommentWriter(None).write(recs, "")
        assert fallback
        assert all(r.comment for r in recs)

    async def test_empty_list_is_noop(self):
        assert not await CommentWriter(None).write([], "")

    async def test_blank_model_comment_falls_back(self, recs):
        """模型返回空字符串等于没写，要用规则理由补上。"""
        payload = ",".join(f'{{"id":"{r.dish.id}","comment":"  "}}' for r in recs)
        client = make_client(f"[{payload}]")
        fallback = await CommentWriter(client).write(recs, "")
        assert fallback
        assert all(r.comment.strip() for r in recs)
