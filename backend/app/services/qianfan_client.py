"""百度智能云千帆平台客户端。

职责边界：只负责「把请求发出去、把文本拿回来」，包括鉴权、超时、重试、熔断。
**不做任何业务兜底**——调用失败一律抛 :class:`QianfanError`，
由上层（`app/agent/`）决定降级成规则结果还是直接报错。
把「怎么调模型」和「调不通怎么办」分开，两边都好测。

鉴权用 access_token 模式：AK/SK 换 token，token 有效期约 30 天，
进程内缓存并提前刷新，不必每次请求都换一遍。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.config import Settings, get_settings

# token 提前多久刷新：官方给 30 天有效期，留 10 分钟余量足够，
# 避免请求正好卡在过期瞬间。
TOKEN_REFRESH_MARGIN = 600.0

TOKEN_PATH = "/oauth/2.0/token"
CHAT_PATH = "/rpc/2.0/ai_custom/v1/wenxinworkshop/chat"

# 千帆的业务错误码：这几个重试也没用，直接放弃。
# 18 是 QPS 超限、336501 是服务过载，属于临时问题，值得重试。
FATAL_ERROR_CODES = frozenset({4, 6, 13, 14, 15, 17, 100, 110, 111})
RETRYABLE_ERROR_CODES = frozenset({18, 19, 336100, 336501, 336502})


class QianfanError(RuntimeError):
    """千帆调用失败的基类。上层捕获这个就能覆盖所有失败情形。"""


class QianfanAuthError(QianfanError):
    """鉴权失败：AK/SK 不对或应用被禁用。重试无意义。"""


class QianfanTimeoutError(QianfanError):
    """重试之后仍然超时。"""


class QianfanAPIError(QianfanError):
    """千帆返回了业务错误码。"""

    def __init__(self, code: int | str, message: str):
        super().__init__(f"千帆返回错误 {code}: {message}")
        self.code = code
        self.message = message


class QianfanCircuitOpenError(QianfanError):
    """熔断器打开，本次调用被短路，没有真的发出请求。"""


class QianfanNotConfiguredError(QianfanError):
    """没配 AK/SK。用于让上层明确走纯规则模式，而不是等一次无谓的超时。"""


@dataclass
class _CircuitBreaker:
    """极简熔断器。

    千帆挂掉时，每个请求都白等 12 秒再重试两次是最糟的体验。
    连续失败到阈值就直接短路，冷却期过后放一个请求试探，
    成功则恢复，失败则重新计时。

    只在单进程内计数，多实例部署时各自独立——对本项目的规模足够，
    真要跨实例共享状态得上 Redis，属于过度设计。
    """

    threshold: int
    cooldown: float

    failures: int = 0
    opened_at: float | None = None
    _probing: bool = field(default=False, repr=False)

    def allow(self) -> bool:
        """现在能不能发请求。"""
        if self.opened_at is None:
            return True
        if time.monotonic() - self.opened_at >= self.cooldown:
            # 冷却结束，半开：放一个探测请求出去
            self._probing = True
            return True
        return False

    def record_success(self) -> None:
        self.failures = 0
        self.opened_at = None
        self._probing = False

    def record_failure(self) -> None:
        self.failures += 1
        if self._probing or self.failures >= self.threshold:
            # 探测请求也失败，说明还没恢复，重新开始冷却
            self.opened_at = time.monotonic()
            self._probing = False

    @property
    def is_open(self) -> bool:
        """是否正在短路。

        冷却期已过就不算 open 了——此时下一次 :meth:`allow` 会放行探测请求，
        对外应该报「可用」，否则上层会永远绕开千帆不再尝试。
        """
        if self.opened_at is None:
            return False
        return time.monotonic() - self.opened_at < self.cooldown

    def retry_after(self) -> float:
        """还要等多久才会放行，仅用于错误信息。"""
        if self.opened_at is None:
            return 0.0
        return max(0.0, self.cooldown - (time.monotonic() - self.opened_at))


class QianfanClient:
    """千帆对话接口的异步客户端。

    用法::

        async with QianfanClient() as client:
            text = await client.chat("你好", system="你是食堂推荐助手")

    也可以复用一个长生命周期实例（FastAPI 场景），
    由 :func:`get_client` 提供单例，进程退出时调 :meth:`aclose`。
    """

    def __init__(
        self,
        settings: Settings | None = None,
        client: httpx.AsyncClient | None = None,
    ):
        self._settings = settings or get_settings()
        # 允许注入 httpx.AsyncClient，测试时塞 MockTransport 就能不联网跑通
        self._http = client
        self._owns_http = client is None

        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self._token_lock = asyncio.Lock()

        self._breaker = _CircuitBreaker(
            threshold=self._settings.qianfan_failure_threshold,
            cooldown=self._settings.qianfan_circuit_cooldown,
        )

    # ---------- 生命周期 ----------

    async def __aenter__(self) -> QianfanClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    def _ensure_http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                base_url=self._settings.qianfan_base_url,
                timeout=self._settings.qianfan_timeout,
            )
        return self._http

    async def aclose(self) -> None:
        if self._http is not None and self._owns_http:
            await self._http.aclose()
            self._http = None

    # ---------- 状态查询 ----------

    @property
    def available(self) -> bool:
        """现在值不值得尝试调用。上层可以拿它决定是否直接走规则路径。"""
        return self._settings.qianfan_configured() and not self._breaker.is_open

    # ---------- 鉴权 ----------

    async def _get_access_token(self) -> str:
        """取 access_token，进程内缓存到快过期为止。

        加锁是为了防止并发请求同时发现 token 过期、一起去换新的：
        既浪费配额，也可能让先拿到的 token 被后一次调用覆盖。
        """
        if not self._settings.qianfan_configured():
            raise QianfanNotConfiguredError(
                "未配置 QIANFAN_AK / QIANFAN_SK，无法调用千帆"
            )

        if self._token and time.monotonic() < self._token_expires_at:
            return self._token

        async with self._token_lock:
            # 双重检查：等锁期间可能已经有人刷好了
            if self._token and time.monotonic() < self._token_expires_at:
                return self._token

            http = self._ensure_http()
            try:
                resp = await http.post(
                    TOKEN_PATH,
                    params={
                        "grant_type": "client_credentials",
                        "client_id": self._settings.qianfan_ak,
                        "client_secret": self._settings.qianfan_sk,
                    },
                )
            except httpx.TimeoutException as exc:
                raise QianfanTimeoutError(f"获取 access_token 超时: {exc}") from exc
            except httpx.HTTPError as exc:
                raise QianfanError(f"获取 access_token 失败: {exc}") from exc

            if resp.status_code != 200:
                raise QianfanAuthError(
                    f"获取 access_token 返回 HTTP {resp.status_code}"
                )

            data = _parse_json(resp)
            if "error" in data:
                # AK/SK 不对会走到这里，重试没有意义
                raise QianfanAuthError(
                    f"鉴权失败: {data.get('error')} {data.get('error_description', '')}"
                )

            token = data.get("access_token")
            if not token:
                raise QianfanAuthError(f"响应里没有 access_token: {data}")

            expires_in = float(data.get("expires_in", 2592000))
            self._token = token
            self._token_expires_at = (
                time.monotonic() + max(expires_in - TOKEN_REFRESH_MARGIN, 60.0)
            )
            return token

    def invalidate_token(self) -> None:
        """丢弃缓存的 token。token 失效（错误码 110/111）时调用，下次请求会重新换。"""
        self._token = None
        self._token_expires_at = 0.0

    # ---------- 对话 ----------

    async def chat(
        self,
        prompt: str,
        system: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
    ) -> str:
        """发一轮对话，返回模型输出的纯文本。

        Raises:
            QianfanNotConfiguredError: 没配密钥。
            QianfanCircuitOpenError: 熔断中，请求没有真的发出。
            QianfanTimeoutError: 重试后仍超时。
            QianfanAPIError: 千帆返回业务错误码。
        """
        messages = [{"role": "user", "content": prompt}]
        return await self.chat_messages(
            messages,
            system=system,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
        )

    async def chat_messages(
        self,
        messages: list[dict[str, str]],
        system: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
    ) -> str:
        """多轮对话版本。

        千帆要求 messages 里 user/assistant 严格交替且以 user 结尾，
        system 提示词要单独放在 body 的 system 字段，不能混进 messages。
        """
        if not self._settings.qianfan_configured():
            raise QianfanNotConfiguredError(
                "未配置 QIANFAN_AK / QIANFAN_SK，无法调用千帆"
            )

        if not self._breaker.allow():
            raise QianfanCircuitOpenError(
                f"千帆连续失败 {self._breaker.failures} 次已熔断，"
                f"{self._breaker.retry_after():.0f} 秒后重试"
            )

        payload: dict[str, Any] = {
            "messages": messages,
            "temperature": temperature
            if temperature is not None
            else self._settings.qianfan_temperature,
        }
        if system:
            payload["system"] = system
        if max_output_tokens:
            payload["max_output_tokens"] = max_output_tokens

        try:
            data = await self._post_with_retry(payload)
        except QianfanError:
            self._breaker.record_failure()
            raise

        self._breaker.record_success()

        result = data.get("result", "")
        if not isinstance(result, str) or not result.strip():
            # 内容安全拦截时 result 为空但 HTTP 200，要当失败处理，
            # 否则上层会把空字符串当成正常推荐语展示出去。
            if data.get("flag"):
                raise QianfanAPIError("content_filtered", "输出被内容安全策略拦截")
            raise QianfanError(f"千帆返回了空结果: {data}")
        return result.strip()

    async def _post_with_retry(self, payload: dict[str, Any]) -> dict[str, Any]:
        """带指数退避的重试。

        只对超时、5xx、以及明确可重试的业务错误码重试；
        鉴权错误和参数错误重试多少次都一样，直接抛出去。
        """
        http = self._ensure_http()
        url = f"{CHAT_PATH}/{self._settings.qianfan_model}"
        attempts = self._settings.qianfan_max_retries + 1
        last_error: QianfanError | None = None

        for attempt in range(attempts):
            if attempt:
                # 0.5s, 1s, 2s ... 选餐是同步场景，退避不宜过长
                await asyncio.sleep(
                    self._settings.qianfan_retry_backoff * (2 ** (attempt - 1))
                )

            try:
                token = await self._get_access_token()
            except QianfanAuthError:
                # AK/SK 不对，再试也是一样的结果
                raise
            except QianfanError as exc:
                # 换 token 时的超时或网络抖动，值得跟着重试
                last_error = exc
                continue

            try:
                resp = await http.post(
                    url, params={"access_token": token}, json=payload
                )
            except httpx.TimeoutException as exc:
                last_error = QianfanTimeoutError(f"请求超时: {exc}")
                continue
            except httpx.HTTPError as exc:
                last_error = QianfanError(f"网络错误: {exc}")
                continue

            if resp.status_code >= 500:
                last_error = QianfanError(f"千帆服务端错误 HTTP {resp.status_code}")
                continue
            if resp.status_code == 429:
                last_error = QianfanError("千帆限流 HTTP 429")
                continue
            if resp.status_code != 200:
                # 4xx 基本是请求本身有问题，重试也一样
                raise QianfanError(
                    f"千帆返回 HTTP {resp.status_code}: {resp.text[:200]}"
                )

            data = _parse_json(resp)
            error_code = data.get("error_code")
            if not error_code:
                return data

            message = str(data.get("error_msg", ""))
            code = int(error_code) if str(error_code).isdigit() else error_code

            if code in (110, 111):
                # token 过期或非法，清掉缓存后重试一次就能拿到新 token
                self.invalidate_token()
                last_error = QianfanAuthError(f"access_token 失效: {message}")
                continue

            if code in FATAL_ERROR_CODES:
                raise QianfanAPIError(code, message)

            if code in RETRYABLE_ERROR_CODES:
                last_error = QianfanAPIError(code, message)
                continue

            # 没见过的错误码，保守当成不可重试，避免无意义地耗时
            raise QianfanAPIError(code, message)

        raise last_error or QianfanError("千帆调用失败，且没有记录到具体原因")


# ---------- 辅助 ----------


def _parse_json(resp: httpx.Response) -> dict[str, Any]:
    """解析响应体。

    千帆偶尔会在网关异常时返回 HTML 错误页，直接 .json() 会抛
    ValueError 而不是 QianfanError，得在这里统一转换。
    """
    try:
        data = resp.json()
    except ValueError as exc:
        raise QianfanError(f"响应不是合法 JSON: {resp.text[:200]}") from exc
    if not isinstance(data, dict):
        raise QianfanError(f"响应结构不是对象: {data!r}")
    return data


_client: QianfanClient | None = None


def get_client() -> QianfanClient:
    """全局单例，FastAPI 依赖注入用。

    没用 lru_cache 是因为需要在应用关闭时拿到实例调 aclose，
    顺手也方便测试里重置。
    """
    global _client
    if _client is None:
        _client = QianfanClient()
    return _client


async def close_client() -> None:
    """应用关闭时释放连接池。"""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
