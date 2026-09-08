"""FastAPI 依赖：鉴权 + 引擎注入。"""
from __future__ import annotations

from typing import Annotated, Any, AsyncIterator

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings, get_settings
from ..core.engine import Engine, get_engine
from ..db.session import session_scope


async def require_auth(
    x_token: Annotated[str | None, Header(alias="X-Token")] = None,
    settings: Settings = Depends(get_settings),
) -> None:
    token = settings.server.auth_token
    if not token:
        return
    if x_token != token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "口令错误")


async def db_session() -> AsyncIterator[AsyncSession]:
    async with session_scope() as s:
        yield s


def engine() -> Engine:
    return get_engine()


DbDep = Annotated[AsyncSession, Depends(db_session)]
EngineDep = Annotated[Engine, Depends(engine)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def ok(data: Any = None, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"ok": True}
    if data is not None:
        out["data"] = data
    out.update(extra)
    return out
