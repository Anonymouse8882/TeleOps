"""设置：全局 Telegram API 凭据 + 只读的系统信息。"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from sqlalchemy import func, select

from ...core.appconfig import save_telegram_api
from ...db.models import Account
from ..deps import DbDep, EngineDep, SettingsDep, ok
from ..schemas import TelegramApiIn

router = APIRouter(prefix="/api/settings", tags=["settings"])
log = logging.getLogger(__name__)


@router.get("")
async def read_settings(db: DbDep, engine: EngineDep, settings: SettingsDep):
    api = engine.tg_api
    total = (await db.execute(select(func.count(Account.id)))).scalar_one()
    online = (
        await db.execute(select(func.count(Account.id)).where(Account.status == "online"))
    ).scalar_one()
    return ok(
        {
            "telegram": api.masked(),
            "accounts": {"total": int(total or 0), "online": int(online or 0)},
            "system": {
                "version": "0.1.0",
                "uptime": engine.status()["uptime"],
                "data_dir": str(settings.storage.data_dir),
                "sessions_dir": str(settings.sessions_dir),
                "media_dir": str(settings.storage.media_dir),
                "plugin_dirs": [str(d) for d in settings.plugins.dirs],
                "auth_enabled": bool(settings.server.auth_token),
                "send_interval": settings.runtime.send_interval,
                "max_items_per_run": settings.runtime.max_items_per_run,
                "max_flood_wait": settings.runtime.max_flood_wait,
                "media_retention_days": settings.storage.media_retention_days,
                "auto_reload": settings.plugins.auto_reload,
            },
        }
    )


@router.put("/telegram")
async def update_telegram(payload: TelegramApiIn, db: DbDep, engine: EngineDep):
    """保存全局 api_id / api_hash。

    api_hash 留空表示沿用原值（前端只展示掩码，不回传明文）。
    """
    api_id = int(payload.api_id or 0)
    api_hash = (payload.api_hash or "").strip()

    if api_id <= 0:
        raise HTTPException(400, "api_id 必须是正整数")
    if not api_hash:
        if not engine.tg_api.api_hash:
            raise HTTPException(400, "请填写 api_hash")
        api_hash = engine.tg_api.api_hash          # 只改 id、不动 hash
    if len(api_hash) < 16:
        raise HTTPException(400, "api_hash 看起来不对，应该是 32 位十六进制字符串")

    changed = (api_id, api_hash) != (engine.tg_api.api_id, engine.tg_api.api_hash)
    await save_telegram_api(api_id, api_hash)
    await engine.reload_api_credentials()

    affected = 0
    if changed:
        # 凭据变了，已建立的连接必须重连；用旧 api 登录的 session 可能失效
        rows = (await db.execute(select(Account))).scalars().all()
        for a in rows:
            await engine.clients.close(a.id)
        affected = sum(1 for a in rows if a.status == "online")
        log.info("全局 Telegram API 已更新（api_id=%s），已断开 %d 个账号连接", api_id, len(rows))

    return ok(
        {
            **engine.tg_api.masked(),
            "changed": changed,
            "affected_accounts": affected,
            "warning": (
                "凭据已变更。用旧 api_id 登录的账号 session 会失效，需要重新登录或重新导入。"
                if changed and affected else ""
            ),
        }
    )
