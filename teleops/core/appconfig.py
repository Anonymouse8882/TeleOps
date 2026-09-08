"""运行期可改的应用设置（存数据库，区别于启动时读的 config.yaml）。

目前只有一项：全局的 Telegram API 凭据。所有账号共用同一套 api_id / api_hash，
不再每个账号填一遍。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from ..db.models import Account, Setting
from ..db.session import session_scope

log = logging.getLogger(__name__)

TELEGRAM_KEY = "telegram_api"


@dataclass
class TelegramApi:
    """全局 Telegram API 凭据。"""

    api_id: int = 0
    api_hash: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.api_id and self.api_hash)

    def masked(self) -> dict[str, Any]:
        """给前端看的形式：api_hash 只露头尾，避免整串暴露在页面上。"""
        h = self.api_hash
        shown = f"{h[:4]}…{h[-4:]}" if len(h) > 10 else ("已设置" if h else "")
        return {
            "api_id": self.api_id,
            "api_hash_masked": shown,
            "has_hash": bool(h),
            "configured": self.configured,
        }


async def load_telegram_api() -> TelegramApi:
    async with session_scope() as s:
        row = await s.get(Setting, TELEGRAM_KEY)
        data = (row.value or {}) if row else {}
    try:
        api_id = int(data.get("api_id") or 0)
    except (TypeError, ValueError):
        api_id = 0
    return TelegramApi(api_id=api_id, api_hash=str(data.get("api_hash") or ""))


async def save_telegram_api(api_id: int, api_hash: str) -> TelegramApi:
    api = TelegramApi(api_id=int(api_id or 0), api_hash=str(api_hash or "").strip())
    async with session_scope() as s:
        row = await s.get(Setting, TELEGRAM_KEY)
        payload = {"api_id": api.api_id, "api_hash": api.api_hash}
        if row is None:
            s.add(Setting(key=TELEGRAM_KEY, value=payload))
        else:
            row.value = payload
    return api


async def seed_from_accounts() -> TelegramApi:
    """老版本把凭据存在每个账号上，首次启动时提升为全局值。

    取第一个填了凭据的账号——那份 api 正是它现有 session 的绑定对象，
    用它做全局值可以保证已登录的账号继续能用。
    """
    api = await load_telegram_api()
    if api.configured:
        return api

    async with session_scope() as s:
        acc = (
            await s.execute(
                select(Account)
                .where(Account.api_id > 0, Account.api_hash != "")
                .order_by(Account.id)
                .limit(1)
            )
        ).scalar_one_or_none()
        if acc is None:
            return api
        api_id, api_hash, name = acc.api_id, acc.api_hash, acc.name

    api = await save_telegram_api(api_id, api_hash)
    log.info("已把账号「%s」的 api_id 提升为全局设置（api_id=%s）", name, api_id)
    return api
