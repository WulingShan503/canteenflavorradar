"""HTTP 接口层。

路由写得薄：只做请求校验、依赖注入、把结果转成响应体。
业务逻辑全在 `app/agent/` 和 `app/services/` 里，
这样换个 Web 框架或加个命令行入口都不用动业务代码。
"""

from app.api.routes import router

__all__ = ["router"]
