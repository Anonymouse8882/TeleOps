"""FastAPI 应用装配。"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..config import Settings, get_settings, set_settings
from ..core.engine import Engine, ensure_plugin_dirs, set_engine
from ..logging_setup import setup_logging
from .deps import require_auth
from .routers import (
    accounts, channels, graph, overview, plugins, settings as settings_router, workflows,
)

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
NO_CACHE = {"Cache-Control": "no-cache"}


class NoCacheStatic(StaticFiles):
    """前端资源不做长缓存。

    app.js / style.css 的地址是固定的，没有版本号；浏览器在只看到 Last-Modified、
    没有 Cache-Control 时会按启发式规则自己决定缓存多久，改了前端刷新页面还是旧的。
    带 no-cache 只是强制回源问一句，ETag 命中照样返回 304，几乎不费流量。
    """

    def file_response(self, *args, **kwargs):  # noqa: ANN002, ANN003
        resp = super().file_response(*args, **kwargs)
        resp.headers.update(NO_CACHE)
        return resp


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    set_settings(settings)  # 让 require_auth / SettingsDep 用的是同一份配置
    setup_logging(settings)
    ensure_plugin_dirs(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = Engine(settings)
        set_engine(engine)
        app.state.engine = engine
        await engine.start()
        try:
            yield
        finally:
            await engine.stop()

    app = FastAPI(
        title="TeleOps",
        description="Telegram 频道专业运营工具",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )

    guard = [Depends(require_auth)]
    for r in (overview.router, accounts.router, channels.router, workflows.router,
              graph.router, plugins.router, settings_router.router):
        app.include_router(r, dependencies=guard)

    @app.exception_handler(Exception)
    async def on_error(request: Request, exc: Exception):  # noqa: ANN001
        log.exception("请求 %s 出错", request.url.path)
        return JSONResponse(
            status_code=500, content={"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        )

    @app.get("/api/ping")
    async def ping():
        return {"ok": True, "auth": bool(settings.server.auth_token)}

    if STATIC.exists():
        app.mount("/static", NoCacheStatic(directory=STATIC), name="static")

        @app.get("/", include_in_schema=False)
        async def index():
            return FileResponse(STATIC / "index.html", headers=NO_CACHE)

    return app
