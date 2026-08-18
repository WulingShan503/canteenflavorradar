"""千帆客户端的测试。

全部用 ``httpx.MockTransport`` 拦掉请求，不联网、不需要真密钥。
重点验证：token 缓存与并发去重、哪些错误重试哪些不重试、
熔断的开合、以及「客户端只抛异常不做业务兜底」这条职责边界。
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.config import Settings
from app.services.qianfan_client import (
    QianfanAPIError,
    QianfanAuthError,
    QianfanCircuitOpenError,
    QianfanClient,
    QianfanError,
    QianfanNotConfiguredError,
    QianfanTimeoutError,
    _CircuitBreaker,
)

TOKEN_BODY = {"access_token": "tk-test", "expires_in": 2592000}


def make_settings(**overrides) -> Settings:
    """造一份带假密钥的配置，退避设成 0 免得测试白等。"""
    base = {
        "qianfan_ak": "ak-test",
        "qianfan_sk": "sk-test",
        "qianfan_retry_backoff": 0.0,
        "qianfan_timeout": 1.0,
        # 不读本地 .env，避免开发机上真配了密钥导致测试行为不一致
        "_env_file": None,
    }
    base.update(overrides)
    return Settings(**base)


def make_client(handler, **setting_overrides) -> QianfanClient:
    settings = make_settings(**setting_overrides)
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(
        base_url=settings.qianfan_base_url, transport=transport
    )
    return QianfanClient(settings=settings, client=http)


def chat_ok(text: str = "这道菜很适合你") -> dict:
    return {"result": text, "id": "as-1", "object": "chat.completion"}


class TestConfiguration:
    def test_missing_keys_detected(self):
        assert not Settings(qianfan_ak="", qianfan_sk="", _env_file=None).qianfan_configured()
        assert not Settings(qianfan_ak="a", qianfan_sk="", _env_file=None).qianfan_configured()
        assert Settings(qianfan_ak="a", qianfan_sk="b", _env_file=None).qianfan_configured()

    def test_blank_keys_are_not_configured(self):
        """只填空格等于没填，不能让它跑去发一次注定失败的请求。"""
        assert not Settings(qianfan_ak="  ", qianfan_sk="  ", _env_file=None).qianfan_configured()

    @pytest.mark.asyncio
    async def test_unconfigured_raises_immediately(self):
        """没配密钥要立刻抛，不能白等一次超时。"""
        called = False

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal called
            called = True
            return httpx.Response(200, json=chat_ok())

        client = make_client(handler, qianfan_ak="", qianfan_sk="")
        with pytest.raises(QianfanNotConfiguredError):
            await client.chat("你好")
        assert not called, "未配置时不应该发出任何请求"
        assert not client.available


class TestToken:
    @pytest.mark.asyncio
    async def test_token_fetched_once_and_cached(self):
        token_calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal token_calls
            if "oauth" in request.url.path:
                token_calls += 1
                return httpx.Response(200, json=TOKEN_BODY)
            return httpx.Response(200, json=chat_ok())

        client = make_client(handler)
        await client.chat("第一次")
        await client.chat("第二次")
        assert token_calls == 1, "token 应该被缓存，不该每次请求都换"

    @pytest.mark.asyncio
    async def test_concurrent_calls_share_one_token_request(self):
        """并发首次调用不该各换一次 token。"""
        token_calls = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal token_calls
            if "oauth" in request.url.path:
                token_calls += 1
                await asyncio.sleep(0.01)  # 放大竞争窗口
                return httpx.Response(200, json=TOKEN_BODY)
            return httpx.Response(200, json=chat_ok())

        client = make_client(handler)
        await asyncio.gather(*(client.chat(f"并发{i}") for i in range(5)))
        assert token_calls == 1

    @pytest.mark.asyncio
    async def test_bad_credentials_not_retried(self):
        """AK/SK 错了重试没意义，应该只请求一次就抛。"""
        token_calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal token_calls
            token_calls += 1
            return httpx.Response(
                200,
                json={"error": "invalid_client", "error_description": "unknown client id"},
            )

        client = make_client(handler)
        with pytest.raises(QianfanAuthError):
            await client.chat("你好")
        assert token_calls == 1

    @pytest.mark.asyncio
    async def test_expired_token_refreshed_and_retried(self):
        """错误码 110 表示 token 失效，应清缓存换新 token 并重试成功。"""
        token_calls = 0
        chat_calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal token_calls, chat_calls
            if "oauth" in request.url.path:
                token_calls += 1
                return httpx.Response(200, json=TOKEN_BODY)
            chat_calls += 1
            if chat_calls == 1:
                return httpx.Response(
                    200, json={"error_code": 110, "error_msg": "Access token invalid"}
                )
            return httpx.Response(200, json=chat_ok("换完 token 就好了"))

        client = make_client(handler)
        assert await client.chat("你好") == "换完 token 就好了"
        assert token_calls == 2, "失效后应该重新换 token"

    @pytest.mark.asyncio
    async def test_invalidate_token_forces_refetch(self):
        token_calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal token_calls
            if "oauth" in request.url.path:
                token_calls += 1
                return httpx.Response(200, json=TOKEN_BODY)
            return httpx.Response(200, json=chat_ok())

        client = make_client(handler)
        await client.chat("一")
        client.invalidate_token()
        await client.chat("二")
        assert token_calls == 2


class TestRetry:
    @pytest.mark.asyncio
    async def test_timeout_retried_then_succeeds(self):
        chat_calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal chat_calls
            if "oauth" in request.url.path:
                return httpx.Response(200, json=TOKEN_BODY)
            chat_calls += 1
            if chat_calls == 1:
                raise httpx.ReadTimeout("timeout", request=request)
            return httpx.Response(200, json=chat_ok("重试成功"))

        client = make_client(handler)
        assert await client.chat("你好") == "重试成功"
        assert chat_calls == 2

    @pytest.mark.asyncio
    async def test_timeout_exhausts_retries_and_raises(self):
        chat_calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal chat_calls
            if "oauth" in request.url.path:
                return httpx.Response(200, json=TOKEN_BODY)
            chat_calls += 1
            raise httpx.ReadTimeout("timeout", request=request)

        client = make_client(handler, qianfan_max_retries=2)
        with pytest.raises(QianfanTimeoutError):
            await client.chat("你好")
        assert chat_calls == 3, "1 次原始请求 + 2 次重试"

    @pytest.mark.asyncio
    async def test_server_error_retried(self):
        chat_calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal chat_calls
            if "oauth" in request.url.path:
                return httpx.Response(200, json=TOKEN_BODY)
            chat_calls += 1
            if chat_calls < 3:
                return httpx.Response(502, text="bad gateway")
            return httpx.Response(200, json=chat_ok("终于通了"))

        client = make_client(handler)
        assert await client.chat("你好") == "终于通了"
        assert chat_calls == 3

    @pytest.mark.asyncio
    async def test_rate_limit_retried(self):
        """QPS 超限是临时问题，应该退避后重试。"""
        chat_calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal chat_calls
            if "oauth" in request.url.path:
                return httpx.Response(200, json=TOKEN_BODY)
            chat_calls += 1
            if chat_calls == 1:
                return httpx.Response(
                    200, json={"error_code": 18, "error_msg": "Open api qps limit"}
                )
            return httpx.Response(200, json=chat_ok())

        client = make_client(handler)
        await client.chat("你好")
        assert chat_calls == 2

    @pytest.mark.asyncio
    async def test_fatal_error_code_not_retried(self):
        """权限类错误码重试多少次都一样，不该浪费时间。"""
        chat_calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal chat_calls
            if "oauth" in request.url.path:
                return httpx.Response(200, json=TOKEN_BODY)
            chat_calls += 1
            return httpx.Response(
                200, json={"error_code": 17, "error_msg": "Open api daily request limit reached"}
            )

        client = make_client(handler)
        with pytest.raises(QianfanAPIError) as exc:
            await client.chat("你好")
        assert exc.value.code == 17
        assert chat_calls == 1

    @pytest.mark.asyncio
    async def test_client_error_not_retried(self):
        chat_calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal chat_calls
            if "oauth" in request.url.path:
                return httpx.Response(200, json=TOKEN_BODY)
            chat_calls += 1
            return httpx.Response(400, text="bad request")

        client = make_client(handler)
        with pytest.raises(QianfanError):
            await client.chat("你好")
        assert chat_calls == 1

    @pytest.mark.asyncio
    async def test_zero_retries_setting_respected(self):
        chat_calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal chat_calls
            if "oauth" in request.url.path:
                return httpx.Response(200, json=TOKEN_BODY)
            chat_calls += 1
            raise httpx.ReadTimeout("timeout", request=request)

        client = make_client(handler, qianfan_max_retries=0)
        with pytest.raises(QianfanTimeoutError):
            await client.chat("你好")
        assert chat_calls == 1


class TestResponseHandling:
    @pytest.mark.asyncio
    async def test_empty_result_treated_as_failure(self):
        """内容安全拦截时 HTTP 200 但 result 为空，不能当成正常推荐语。"""

        def handler(request: httpx.Request) -> httpx.Response:
            if "oauth" in request.url.path:
                return httpx.Response(200, json=TOKEN_BODY)
            return httpx.Response(200, json={"result": "", "flag": 1})

        client = make_client(handler)
        with pytest.raises(QianfanAPIError):
            await client.chat("你好")

    @pytest.mark.asyncio
    async def test_non_json_response_wrapped(self):
        """网关返回 HTML 错误页时也要抛 QianfanError，不能漏出 ValueError。"""

        def handler(request: httpx.Request) -> httpx.Response:
            if "oauth" in request.url.path:
                return httpx.Response(200, json=TOKEN_BODY)
            return httpx.Response(200, text="<html>error</html>")

        client = make_client(handler)
        with pytest.raises(QianfanError):
            await client.chat("你好")

    @pytest.mark.asyncio
    async def test_result_is_stripped(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if "oauth" in request.url.path:
                return httpx.Response(200, json=TOKEN_BODY)
            return httpx.Response(200, json=chat_ok("  带空白的推荐语  \n"))

        client = make_client(handler)
        assert await client.chat("你好") == "带空白的推荐语"

    @pytest.mark.asyncio
    async def test_system_prompt_sent_separately(self):
        """千帆要求 system 单独放 body，不能混进 messages。"""
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if "oauth" in request.url.path:
                return httpx.Response(200, json=TOKEN_BODY)
            import json as _json

            captured.update(_json.loads(request.content))
            return httpx.Response(200, json=chat_ok())

        client = make_client(handler)
        await client.chat("推荐一道菜", system="你是食堂推荐助手")
        assert captured["system"] == "你是食堂推荐助手"
        assert captured["messages"] == [{"role": "user", "content": "推荐一道菜"}]
        assert all(m["role"] != "system" for m in captured["messages"])

    @pytest.mark.asyncio
    async def test_model_name_in_url(self):
        captured_paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured_paths.append(request.url.path)
            if "oauth" in request.url.path:
                return httpx.Response(200, json=TOKEN_BODY)
            return httpx.Response(200, json=chat_ok())

        client = make_client(handler, qianfan_model="ernie-4.0-8k")
        await client.chat("你好")
        assert any(p.endswith("/ernie-4.0-8k") for p in captured_paths)


class TestCircuitBreaker:
    def test_opens_after_threshold(self):
        cb = _CircuitBreaker(threshold=3, cooldown=60.0)
        assert cb.allow()
        cb.record_failure()
        cb.record_failure()
        assert cb.allow(), "还没到阈值不该熔断"
        cb.record_failure()
        assert cb.is_open
        assert not cb.allow()

    def test_success_resets_counter(self):
        cb = _CircuitBreaker(threshold=3, cooldown=60.0)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        cb.record_failure()
        assert cb.allow(), "成功一次后计数应清零"

    def test_half_open_probe_after_cooldown(self):
        """冷却结束应放一个探测请求，成功则完全恢复。"""
        cb = _CircuitBreaker(threshold=1, cooldown=0.0)
        cb.record_failure()
        assert cb.allow(), "冷却为 0，应立即允许探测"
        cb.record_success()
        assert not cb.is_open
        assert cb.failures == 0

    def test_failed_probe_reopens(self):
        cb = _CircuitBreaker(threshold=1, cooldown=0.0)
        cb.record_failure()
        assert cb.allow()
        cb.record_failure()
        assert cb.opened_at is not None, "探测失败应重新进入熔断"

    def test_is_open_false_once_cooldown_elapsed(self):
        """冷却已过就该对外报可用，否则上层会永远绕开千帆。"""
        cb = _CircuitBreaker(threshold=1, cooldown=0.0)
        cb.record_failure()
        assert not cb.is_open

    @pytest.mark.asyncio
    async def test_client_short_circuits_after_repeated_failures(self):
        """连续失败到阈值后，后续调用不该再真的发请求。"""
        chat_calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal chat_calls
            if "oauth" in request.url.path:
                return httpx.Response(200, json=TOKEN_BODY)
            chat_calls += 1
            return httpx.Response(500, text="boom")

        client = make_client(
            handler,
            qianfan_max_retries=0,
            qianfan_failure_threshold=2,
            qianfan_circuit_cooldown=60.0,
        )

        for _ in range(2):
            with pytest.raises(QianfanError):
                await client.chat("你好")

        calls_before = chat_calls
        with pytest.raises(QianfanCircuitOpenError):
            await client.chat("你好")
        assert chat_calls == calls_before, "熔断后不应再发出请求"
        assert not client.available

    @pytest.mark.asyncio
    async def test_available_reflects_state(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if "oauth" in request.url.path:
                return httpx.Response(200, json=TOKEN_BODY)
            return httpx.Response(200, json=chat_ok())

        client = make_client(handler)
        assert client.available
        await client.chat("你好")
        assert client.available


class TestLayerBoundary:
    @pytest.mark.asyncio
    async def test_client_never_returns_fallback_text(self):
        """职责边界：客户端只抛异常，业务兜底是上层的事。

        如果哪天有人在这里 return 一句「暂时无法生成推荐语」，
        上层就没法通过 fallback_used 告知用户降级了，这条断言会挡住。
        """

        def handler(request: httpx.Request) -> httpx.Response:
            if "oauth" in request.url.path:
                return httpx.Response(200, json=TOKEN_BODY)
            return httpx.Response(500, text="boom")

        client = make_client(handler, qianfan_max_retries=0)
        with pytest.raises(QianfanError):
            await client.chat("你好")

    @pytest.mark.asyncio
    async def test_all_errors_share_one_base_class(self):
        """上层 except QianfanError 必须能兜住所有失败情形。"""
        for exc_type in (
            QianfanAuthError,
            QianfanTimeoutError,
            QianfanCircuitOpenError,
            QianfanNotConfiguredError,
        ):
            assert issubclass(exc_type, QianfanError)
        assert issubclass(QianfanAPIError, QianfanError)
