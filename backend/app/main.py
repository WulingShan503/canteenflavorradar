"""FastAPI 应用入口。

启动：``uvicorn app.main:app --reload``

不配千帆密钥也能起来，此时 `/api/health` 会报 ``mode: rule-only``，
推荐接口照常工作，只是少了自然语言解析和大模型推荐语。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import __version__
from app.api.routes import router
from app.config import get_settings
from app.services.dish_repository import get_repository
from app.services.qianfan_client import close_client

logger = logging.getLogger(__name__)

DESCRIPTION = """
基于大模型 Agent 的高校食堂选餐系统。

设计上把「能不能吃」和「有多想吃」拆开：过敏原、忌口这类安全约束由确定性
规则代码兜住，绝不交给可能产生幻觉的模型；「为什么推荐这道菜」这类需要
语感的表达交给大模型。千帆不可用时系统降级但不瘫痪，
响应里的 `fallback_used` 会标记这一点。
"""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """启动时预热数据，关闭时释放连接池。

    预热是为了让菜品数据的解析错误在启动阶段就暴露出来，
    而不是等第一个用户请求进来才报 500。
    """
    settings = get_settings()
    repo = get_repository()
    logger.info(
        "%s v%s 启动完成，已加载 %d 道在售菜品，千帆%s",
        settings.app_name,
        __version__,
        len(repo.all_dishes()),
        "已配置" if settings.qianfan_configured() else "未配置（纯规则模式）",
    )

    yield

    await close_client()
    logger.info("已释放千帆连接池")


def create_app() -> FastAPI:
    """构造应用。做成工厂函数便于测试里造独立实例。"""
    settings = get_settings()

    app = FastAPI(
        title=settings.app_name,
        description=DESCRIPTION,
        version=__version__,
        lifespan=lifespan,
    )

    # 校园内部使用的演示系统，默认放开跨域方便前端本地联调。
    # 注意：真要对外部署得把 cors_origins 收窄到具体域名，
    # 配合 allow_credentials 时通配符是不生效的（浏览器会拒绝）。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    app.include_router(router)

    @app.get("/", include_in_schema=False)
    async def root() -> dict:
        """根路径给个指引，别让访问者看到 404 就以为挂了。"""
        return {
            "name": settings.app_name,
            "version": __version__,
            "docs": "/docs",
            "health": "/api/health",
        }

    @app.exception_handler(RequestValidationError)
    async def validation_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """把 Pydantic 的校验报错转成中文提示。

        默认的报错结构对前端不友好，而且全是英文字段路径，
        用户看到「Input should be less than or equal to 20」不知道该改哪。
        """
        first = exc.errors()[0] if exc.errors() else {}
        field = ".".join(str(p) for p in first.get("loc", ()) if p != "body")
        return JSONResponse(
            status_code=422,
            content={
                "detail": f"请求参数有误：{field or '未知字段'} {first.get('msg', '')}",
                "code": "invalid_request",
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        """兜底：不把异常堆栈暴露给调用方，但要完整记进日志。"""
        logger.exception("未处理的异常：%s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={"detail": "服务内部错误，请稍后重试", "code": "internal_error"},
        )

    return app


app = create_app()
