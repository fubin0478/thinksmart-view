from aiohttp import web
from config.logger import setup_logging


class BaseHandler:
    def __init__(self, config: dict):
        self.config = config
        self.logger = setup_logging()

    def _add_cors_headers(self, response):
        """添加安全响应头（不向外部网页开放通配符 CORS）"""
        if response is None:
            return
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Cache-Control", "no-store")

    async def handle_options(self, request):
        """处理OPTIONS请求（不返回通配符跨域授权头）"""
        response = web.Response(status=204)
        self._add_cors_headers(response)
        return response
