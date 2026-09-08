"""Telethon 客户端管理：多账号连接池 + 交互式登录 + 实体解析缓存。"""
from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from telethon import TelegramClient
from telethon.errors import (
    AuthKeyDuplicatedError,
    AuthKeyError,
    AuthKeyUnregisteredError,
    ChannelPrivateError,
    FloodWaitError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
    SessionRevokedError,
    UserDeactivatedBanError,
    UsernameNotOccupiedError,
)
from telethon.sessions import SQLiteSession, StringSession

from ..config import Settings
from ..db.models import Account

log = logging.getLogger(__name__)

_PEER_URL = re.compile(r"(?:https?://)?(?:t\.me|telegram\.me)/(?:s/)?(?P<name>[A-Za-z0-9_]+)/?")


def normalize_peer(peer: str | int) -> str | int:
    """把 t.me 链接 / @name / -100xxx / 数字 统一成 Telethon 能吃的形式。"""
    if isinstance(peer, int):
        return peer
    p = str(peer).strip()
    if not p:
        raise ValueError("空的 peer")
    m = _PEER_URL.match(p)
    if m:
        p = "@" + m.group("name")
    if p.startswith("@"):
        return p
    if re.fullmatch(r"-?\d+", p):
        return int(p)
    return "@" + p


def parse_proxy(spec: str) -> dict[str, Any] | None:
    """socks5://user:pass@host:port -> Telethon proxy dict（python-socks 风格）。"""
    if not spec:
        return None
    m = re.match(
        r"^(?P<scheme>socks5|socks4|http)://(?:(?P<user>[^:@]+)(?::(?P<pwd>[^@]*))?@)?"
        r"(?P<host>[^:]+):(?P<port>\d+)$",
        spec.strip(),
    )
    if not m:
        log.warning("代理格式无法识别，已忽略：%s", spec)
        return None
    d: dict[str, Any] = {
        "proxy_type": m.group("scheme"),
        "addr": m.group("host"),
        "port": int(m.group("port")),
        "rdns": True,
    }
    if m.group("user"):
        d["username"] = m.group("user")
        d["password"] = m.group("pwd") or ""
    return d


@dataclass
class LoginSession:
    """一次登录会话的中间状态（等待验证码 / 两步验证密码）。"""

    account_id: int
    client: TelegramClient
    phone: str
    phone_code_hash: str = ""
    stage: str = "code"  # code / password / done
    created_at: float = field(default_factory=lambda: asyncio.get_event_loop().time())


class ClientManager:
    """按 account_id 持有并复用 TelegramClient。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._clients: dict[int, TelegramClient] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._logins: dict[int, LoginSession] = {}
        self._entity_cache: dict[tuple[int, str], Any] = {}
        #: 全局 Telegram API 凭据，由 Engine 在启动时和保存设置时同步进来
        self.api_id: int = 0
        self.api_hash: str = ""

    def set_global_api(self, api_id: int, api_hash: str) -> None:
        self.api_id = int(api_id or 0)
        self.api_hash = str(api_hash or "")

    def credentials(self, account: Account) -> tuple[int, str]:
        """取该账号实际要用的 api 凭据。

        统一用全局值；全局还没配时回退到账号自带的（兼容老数据）。
        """
        if self.api_id and self.api_hash:
            return self.api_id, self.api_hash
        if account.api_id and account.api_hash:
            return account.api_id, account.api_hash
        raise RuntimeError("还没有配置 Telegram API，请到「设置」里填写 api_id / api_hash")

    # ------------------------------------------------------------- 基础设施
    def _lock(self, account_id: int) -> asyncio.Lock:
        return self._locks.setdefault(account_id, asyncio.Lock())

    def _build(self, account: Account) -> TelegramClient:
        self.settings.sessions_dir.mkdir(parents=True, exist_ok=True)
        session_path = self.settings.sessions_dir / account.session_name
        api_id, api_hash = self.credentials(account)
        return TelegramClient(
            SQLiteSession(str(session_path)),
            api_id,
            api_hash,
            proxy=parse_proxy(account.proxy),
            connection_retries=3,
            retry_delay=2,
            auto_reconnect=True,
            device_model="TeleOps",
            app_version="0.1.0",
        )

    async def get(self, account: Account) -> TelegramClient:
        """拿到一个已连接（且已登录）的客户端。"""
        async with self._lock(account.id):
            client = self._clients.get(account.id)
            if client is None:
                client = self._build(account)
                self._clients[account.id] = client
            if not client.is_connected():
                await client.connect()
            if not await client.is_user_authorized():
                if account.is_bot and account.bot_token:
                    await client.start(bot_token=account.bot_token)  # type: ignore[arg-type]
                else:
                    raise RuntimeError(f"账号 {account.name} 未登录，请先在后台完成登录")
            return client

    async def peek(self, account: Account) -> TelegramClient:
        """只连接不要求已登录（登录流程内部使用）。"""
        async with self._lock(account.id):
            client = self._clients.get(account.id)
            if client is None:
                client = self._build(account)
                self._clients[account.id] = client
            if not client.is_connected():
                await client.connect()
            return client

    async def close(self, account_id: int) -> None:
        client = self._clients.pop(account_id, None)
        if client is not None and client.is_connected():
            await client.disconnect()  # type: ignore[misc]
        self._logins.pop(account_id, None)
        for k in [k for k in self._entity_cache if k[0] == account_id]:
            self._entity_cache.pop(k, None)

    async def close_all(self) -> None:
        for aid in list(self._clients):
            try:
                await self.close(aid)
            except Exception as e:
                log.warning("关闭客户端 %s 失败：%s", aid, e)

    # ------------------------------------------------------- 已有 session 导入
    async def probe_session(
        self, session: Any, api_id: int, api_hash: str, proxy: str = ""
    ) -> dict[str, Any]:
        """用一份现成的 session 连一次 Telegram，确认它确实是已登录状态。

        成功返回账号信息；失败抛出带人话说明的 RuntimeError。
        调用方负责在拿到结果后处理 session 文件的落位。
        """
        client = TelegramClient(
            session,
            api_id,
            api_hash,
            proxy=parse_proxy(proxy),
            connection_retries=2,
            retry_delay=2,
            device_model="TeleOps",
            app_version="0.1.0",
        )
        try:
            try:
                await client.connect()
            except Exception as e:
                raise RuntimeError(f"连不上 Telegram：{e}（如果需要代理，请填写代理地址）") from e

            try:
                authorized = await client.is_user_authorized()
            except AuthKeyDuplicatedError as e:
                raise RuntimeError(
                    "这份 session 正在别处使用，已被 Telegram 判定为重复登录。"
                    "请先在原程序里停用它，或换一份 session。"
                ) from e
            except SessionRevokedError as e:
                raise RuntimeError("这份 session 已被账号主动登出，无法再用。") from e
            except UserDeactivatedBanError as e:
                raise RuntimeError("该账号已被 Telegram 封禁或注销。") from e
            except (AuthKeyUnregisteredError, AuthKeyError) as e:
                raise RuntimeError(
                    "session 的密钥已失效（可能已被登出）。请重新获取一份。"
                ) from e

            if not authorized:
                raise RuntimeError(
                    "这份 session 不是已登录状态。常见原因："
                    "① api_id / api_hash 和当初生成 session 时用的不是同一套；"
                    "② 该账号已在 Telegram 端被登出。"
                )

            me = await client.get_me()
            if me is None:
                raise RuntimeError("已连接但读不到账号信息，session 可能已损坏")
            return _me_dict(me)
        finally:
            try:
                if client.is_connected():
                    await client.disconnect()  # type: ignore[misc]
            except Exception:
                pass
            # 必须显式关掉，否则 Windows 上文件被占用、后面挪不动
            try:
                session.close()
            except Exception:
                pass

    # ----------------------------------------------------------------- 登录
    async def start_login(self, account: Account) -> dict[str, Any]:
        client = await self.peek(account)
        if await client.is_user_authorized():
            me = await client.get_me()
            return {"stage": "done", "me": _me_dict(me)}

        if account.is_bot:
            if not account.bot_token:
                raise RuntimeError("Bot 账号必须填写 bot_token")
            await client.start(bot_token=account.bot_token)  # type: ignore[arg-type]
            me = await client.get_me()
            return {"stage": "done", "me": _me_dict(me)}

        if not account.phone:
            raise RuntimeError("用户账号必须填写手机号")
        sent = await client.send_code_request(account.phone)
        self._logins[account.id] = LoginSession(
            account_id=account.id, client=client, phone=account.phone,
            phone_code_hash=sent.phone_code_hash, stage="code",
        )
        return {"stage": "code", "hint": "验证码已发送到 Telegram 客户端"}

    async def submit_code(self, account: Account, code: str) -> dict[str, Any]:
        ls = self._logins.get(account.id)
        if ls is None:
            raise RuntimeError("登录会话已失效，请重新发起登录")
        try:
            await ls.client.sign_in(phone=ls.phone, code=code.strip(), phone_code_hash=ls.phone_code_hash)
        except SessionPasswordNeededError:
            ls.stage = "password"
            return {"stage": "password", "hint": "该账号开启了两步验证，请输入密码"}
        except PhoneCodeInvalidError:
            raise RuntimeError("验证码错误")
        me = await ls.client.get_me()
        ls.stage = "done"
        self._logins.pop(account.id, None)
        return {"stage": "done", "me": _me_dict(me)}

    async def submit_password(self, account: Account, password: str) -> dict[str, Any]:
        ls = self._logins.get(account.id)
        if ls is None:
            raise RuntimeError("登录会话已失效，请重新发起登录")
        await ls.client.sign_in(password=password)
        me = await ls.client.get_me()
        ls.stage = "done"
        self._logins.pop(account.id, None)
        return {"stage": "done", "me": _me_dict(me)}

    async def logout(self, account: Account) -> None:
        client = self._clients.get(account.id)
        if client is None:
            client = await self.peek(account)
        try:
            if await client.is_user_authorized():
                await client.log_out()
        finally:
            await self.close(account.id)
            for suffix in ("", "-journal"):
                f = self.settings.sessions_dir / (account.session_name + ".session" + suffix)
                if f.exists():
                    try:
                        f.unlink()
                    except OSError:
                        pass

    # ------------------------------------------------------------- 实体解析
    async def resolve(self, account: Account, peer: str | int) -> Any:
        key = (account.id, str(peer))
        if key in self._entity_cache:
            return self._entity_cache[key]
        client = await self.get(account)
        try:
            entity = await client.get_entity(normalize_peer(peer))
        except (UsernameNotOccupiedError, ValueError) as e:
            raise RuntimeError(f"无法解析 {peer}：{e}") from e
        except ChannelPrivateError as e:
            raise RuntimeError(f"无权访问 {peer}（私有频道且账号不在其中）") from e
        self._entity_cache[key] = entity
        return entity

    async def describe(self, account: Account, peer: str | int) -> dict[str, Any]:
        entity = await self.resolve(account, peer)
        return {
            "id": getattr(entity, "id", None),
            "title": getattr(entity, "title", None) or getattr(entity, "first_name", "") or "",
            "username": getattr(entity, "username", None),
            "kind": type(entity).__name__,
            "participants": getattr(entity, "participants_count", None),
        }

    # ----------------------------------------------------------------- 状态
    def is_connected(self, account_id: int) -> bool:
        c = self._clients.get(account_id)
        return bool(c and c.is_connected())

    def login_stage(self, account_id: int) -> str | None:
        ls = self._logins.get(account_id)
        return ls.stage if ls else None


async def with_flood_retry(coro_factory, *, max_wait: int = 600, attempts: int = 3, logger=log):
    """遇到 FloodWait 自动等待重试；等待超过 max_wait 则放弃。"""
    last: Exception | None = None
    for i in range(attempts):
        try:
            return await coro_factory()
        except FloodWaitError as e:
            last = e
            if e.seconds > max_wait:
                logger.error("FloodWait %ss 超过上限 %ss，放弃", e.seconds, max_wait)
                raise
            if i == attempts - 1:
                break  # 已经是最后一次，等完也不会再试了，别白白占住这轮
            logger.warning("触发 FloodWait，等待 %ss 后重试（%d/%d）", e.seconds, i + 1, attempts)
            await asyncio.sleep(e.seconds + 1)
    if last:
        raise last
    raise RuntimeError("重试失败")


def _me_dict(me: Any) -> dict[str, Any]:
    if me is None:
        return {}
    return {
        "id": getattr(me, "id", None),
        "username": getattr(me, "username", None),
        "first_name": getattr(me, "first_name", "") or "",
        "last_name": getattr(me, "last_name", "") or "",
        "phone": getattr(me, "phone", None),
        "bot": bool(getattr(me, "bot", False)),
    }


# --------------------------------------------------------------- session 识别
#: Telethon 的 sessions 表长这样：dc_id / server_address / port / auth_key
_TELETHON_COLS = {"dc_id", "server_address", "port", "auth_key"}
#: Pyrogram 的 sessions 表长这样：dc_id / api_id / test_mode / auth_key / user_id
_PYROGRAM_COLS = {"dc_id", "auth_key", "user_id", "is_bot"}


def inspect_session_file(path: str | Path) -> tuple[str, str]:
    """看一眼 .session 文件是什么来路。

    返回 (kind, detail)，kind 取值 telethon / pyrogram / unknown。
    只读 SQLite 元信息，不碰 auth_key 内容。
    """
    p = Path(path)
    if not p.exists() or p.stat().st_size == 0:
        return "unknown", "文件为空"

    head = p.open("rb").read(16)
    if not head.startswith(b"SQLite format 3"):
        return "unknown", "不是 SQLite 文件（Telethon 的 .session 是 SQLite 数据库）"

    try:
        conn = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error as e:
        return "unknown", f"打不开：{e}"
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "sessions" not in tables:
            return "unknown", f"缺少 sessions 表（现有表：{', '.join(sorted(tables)) or '无'}）"
        cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
        if _TELETHON_COLS <= cols:
            row = conn.execute(
                "SELECT dc_id, auth_key FROM sessions LIMIT 1").fetchone()
            if not row or not row[1]:
                return "unknown", "sessions 表里没有密钥，这份 session 是空的"
            return "telethon", f"Telethon session（DC{row[0]}）"
        if _PYROGRAM_COLS <= cols:
            return "pyrogram", "这是 Pyrogram 的 session，格式与 Telethon 不兼容"
        return "unknown", f"无法识别的 sessions 表结构：{', '.join(sorted(cols))}"
    except sqlite3.Error as e:
        return "unknown", f"读取失败：{e}"
    finally:
        conn.close()


def string_session(value: str) -> StringSession:
    """把粘贴进来的 session string 转成 Telethon 的 StringSession。"""
    v = (value or "").strip().strip('"').strip("'")
    if not v:
        raise ValueError("session string 是空的")
    if not v.startswith("1"):
        raise ValueError(
            "这不像 Telethon 的 session string（应以 1 开头）。"
            "Pyrogram / TDesktop 的字符串格式不通用。"
        )
    try:
        return StringSession(v)
    except Exception as e:
        raise ValueError(f"session string 解析失败：{e}") from e
