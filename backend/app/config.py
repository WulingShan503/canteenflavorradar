"""应用配置。

千帆的密钥只从环境变量或 `.env` 读取，绝不写进代码。
本地开发把 `.env.example` 复制成 `.env` 填上自己的 AK/SK，
`.env` 已在 `.gitignore` 里，不会被提交。
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """全局配置。

    没配千帆密钥时 :meth:`qianfan_configured` 返回 False，
    系统整体降级到纯规则模式——不配密钥也能跑起来，方便评审和本地开发。
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- 千帆平台 ----
    qianfan_ak: str = Field("", description="千帆应用 API Key")
    qianfan_sk: str = Field("", description="千帆应用 Secret Key")
    qianfan_model: str = Field(
        "ernie-3.5-8k", description="对话模型名，走 ERNIE 系列"
    )
    qianfan_base_url: str = Field(
        "https://aip.baidubce.com", description="千帆网关地址"
    )

    # ---- 调用策略 ----
    # 选餐是同步交互场景，用户等不了太久：单次 12 秒超时，最多重试 2 次，
    # 最差情况约 30 秒后降级到规则结果，不会一直挂着。
    qianfan_timeout: float = Field(12.0, gt=0, description="单次请求超时，秒")
    qianfan_max_retries: int = Field(2, ge=0, description="失败重试次数")
    qianfan_retry_backoff: float = Field(
        0.5, ge=0, description="重试退避基数，秒，按指数增长"
    )
    qianfan_temperature: float = Field(
        0.7, ge=0.01, le=1.0, description="生成温度，推荐语需要一点发挥空间"
    )

    # 熔断：连续失败达到阈值后直接短路，不再白等超时，冷却结束自动半开重试。
    qianfan_failure_threshold: int = Field(
        3, ge=1, description="连续失败多少次后熔断"
    )
    qianfan_circuit_cooldown: float = Field(
        60.0, gt=0, description="熔断冷却时长，秒"
    )

    # ---- 推荐行为 ----
    recommend_limit: int = Field(5, gt=0, description="默认返回几道菜")
    recommend_min_candidates: int = Field(
        5, gt=0, description="候选不足此数时触发逐级放宽"
    )
    max_per_window: int = Field(2, gt=0, description="同一窗口最多出几道菜")

    # ---- 服务 ----
    app_name: str = Field("食堂味蕾雷达", description="应用名，展示用")
    cors_origins: list[str] = Field(
        default_factory=lambda: ["*"], description="允许的跨域来源"
    )

    def qianfan_configured(self) -> bool:
        """密钥是否齐全。缺任意一个都视为未配置，走纯规则模式。"""
        return bool(self.qianfan_ak.strip() and self.qianfan_sk.strip())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """全局单例。配置在进程启动时读一次，避免每个请求都解析一遍 .env。"""
    return Settings()
