"""轻量管理后台：挂在 HTTP 服务（server.http_port）的 /admin 下。

统一整合到 ThinkSmart 管理后台 (:18091/admin/) 后，仅挂载 /admin/ 引导跳转页、
本机 /kiosk/* 内部控制接口，并将旧 /admin/api/* 停用（HTTP 410）。
"""

from core.admin.admin_handler import AdminHandler


def register_admin_routes(app, config, ws_server=None):
    """在 aiohttp 应用上注册管理后台路由（统一跳转与 /kiosk/* 接口）。"""
    handler = AdminHandler(config, ws_server)
    handler.register_kiosk_only(app)
