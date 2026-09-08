"""登录验证码监听。

Telegram 把登录验证码作为消息发给账号自己（发信人是官方服务号 777000）。
只要这个账号已经在 TeleOps 里登录着，就能替你把码读出来——在别处登录该账号时
不用再去翻手机。

用法是"先开监听、再去别处触发登录"：
    start()  -> 注册事件处理器并回扫最近几分钟的历史
    status() -> 轮询拿结果
    stop()   -> 手动结束（到期也会自动结束）
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from telethon import events
from telethon.tl.types import InputPeerUser

from ..db.models import Account

log = logging.getLogger(__name__)

#: Telegram 官方服务号，登录码、登录提醒都由它下发
SERVICE_UID = 777000
#: 服务号的 access_hash 恒为 0，直接构造 peer 就能读，
#: 不用先 get_entity——有些 session 的实体缓存里没有它，解析会失败。
SERVICE_PEER = InputPeerUser(SERVICE_UID, 0)

DEFAULT_SECONDS = 180
MAX_SECONDS = 600

#: 主规则：冒号后紧跟 5~7 位数字。
#: Telegram 各语言版本的验证码文案都是这个骨架——
#:   Login code: 41573 / Código de inicio de sesión: 12280 /
#:   Код для входа: 33449 / 登录代码：51923 / ログインコード: 12345
#: 所以按标点匹配比追关键词可靠得多。半角全角冒号、法语的空格冒号都能吃下。
_COLON = re.compile(r"[:：]\s*\*{0,2}(\d{5,7})(?!\d)")

#: 少数语言的文案不带冒号，补几个关键词形式兜着
_KEYED = [
    re.compile(r"(?:login\s*code|code)\s*\*{0,2}(\d{5,7})(?!\d)", re.I),
    re.compile(r"(?:登录|登陆|登入)\s*(?:验证码|驗證碼|代码|代碼|码|碼)\s*\*{0,2}(\d{5,7})(?!\d)"),
    re.compile(r"(?:验证码|驗證碼)\s*\*{0,2}(\d{5,7})(?!\d)"),
    re.compile(r"код\D{0,12}(\d{5,7})(?!\d)", re.I),
]

#: 独立的 5~7 位数字（前后都不是数字），用于最后的兜底
_LOOSE = re.compile(r"(?<!\d)(\d{5,7})(?!\d)")

#: 登录提醒（"检测到新设备登录"）不是验证码，别把里面的数字当码。
#: 这类消息的共同特征是提到了设备/地点/时间，且没有"冒号+码"的结构。
_ALERT = re.compile(
    r"new\s+login|nuevo\s+inicio|novo\s+login|nouvelle\s+connexion|neue\s+anmeldung|"
    r"nuovo\s+accesso|新的?登录|新設備|новый\s+вход|yeni\s+giriş",
    re.I,
)


def extract_code(text: str) -> str | None:
    """从服务号消息里抠出验证码；抠不到返回 None。

    调用方已经保证这条消息来自官方服务号 777000，所以这里可以放得开一些。
    """
    t = (text or "").strip()
    if not t:
        return None

    # 1) 冒号骨架——覆盖绝大多数语言
    m = _COLON.search(t)
    if m:
        return m.group(1)

    # 2) 个别不带冒号的写法
    for rx in _KEYED:
        m = rx.search(t)
        if m:
            return m.group(1)

    # 3) 兜底：全文只有一个独立的 5~7 位数字，且不是登录提醒
    if not _ALERT.search(t):
        nums = set(_LOOSE.findall(t))
        if len(nums) == 1:
            return nums.pop()
    return None


def _from_service(message: Any) -> bool:
    """这条消息是不是 Telegram 官方服务号发来的。"""
    if message is None:
        return False
    if getattr(message, "sender_id", None) == SERVICE_UID:
        return True
    peer = getattr(message, "peer_id", None)
    return getattr(peer, "user_id", None) == SERVICE_UID


@dataclass
class CodeHit:
    code: str
    text: str
    at: dt.datetime
    source: str = "live"        # live=监听到的 / history=开监听时回扫到的

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "text": self.text[:400],
            "at": self.at.isoformat() if self.at else None,
            "source": self.source,
        }


@dataclass
class Watch:
    account_id: int
    client: Any
    handler: Any
    started_at: float
    expires_at: float
    hits: list[CodeHit] = field(default_factory=list)
    #: 服务号最近的原始消息。抽不出码时把原文摆出来，
    #: 至少让人能自己看，也方便判断到底是没收到还是没抽出来。
    seen_msgs: list[dict[str, Any]] = field(default_factory=list)
    seen_ids: set[int] = field(default_factory=set)
    task: asyncio.Task[None] | None = None
    error: str = ""

    @property
    def remaining(self) -> int:
        return max(0, int(self.expires_at - asyncio.get_event_loop().time()))

    @property
    def active(self) -> bool:
        return self.remaining > 0


class CodeWatcher:
    """按账号管理验证码监听。"""

    def __init__(self, clients: Any) -> None:
        self.clients = clients
        self._watches: dict[int, Watch] = {}

    # ------------------------------------------------------------------ 开始
    async def start(self, account: Account, seconds: int = DEFAULT_SECONDS) -> dict[str, Any]:
        if account.is_bot:
            raise RuntimeError("Bot 账号收不到登录验证码")

        seconds = max(30, min(int(seconds or DEFAULT_SECONDS), MAX_SECONDS))
        await self.stop(account.id)          # 重复点就重开，别叠加处理器

        client = await self.clients.get(account)
        loop = asyncio.get_event_loop()
        watch = Watch(
            account_id=account.id,
            client=client,
            handler=None,
            started_at=loop.time(),
            expires_at=loop.time() + seconds,
        )

        async def on_message(event: Any) -> None:
            try:
                if _from_service(event.message):
                    self._absorb(watch, event.message, "live")
            except Exception as e:            # 事件回调里绝不能抛
                log.warning("处理验证码消息出错：%s", e)

        watch.handler = on_message
        # 不用 events.NewMessage(chats=...)：那个过滤器要先把 777000 解析成实体，
        # 而部分 session 的缓存里没有它，解析失败会导致处理器永远不触发。
        # 收全部新消息、在回调里按发信人判断，稳得多。
        client.add_event_handler(on_message, events.NewMessage(incoming=True))
        self._watches[account.id] = watch

        # 码可能在开监听之前就到了，回扫一下最近的历史
        history = await self._scan_history(watch)
        watch.task = asyncio.create_task(self._expire_later(account.id, seconds))

        log.info("账号「%s」开始监听登录验证码，%d 秒", account.name, seconds)
        return {**self.status(account.id), "scanned_history": history}

    async def _scan_history(self, watch: Watch, limit: int = 15) -> int:
        """回扫服务号最近的消息，只认 10 分钟内的，避免翻出上次的旧码。"""
        msgs: Any = None
        errors: list[str] = []
        # 固定 peer 优先；个别 session 上它不灵时再退回按 id 解析
        for peer in (SERVICE_PEER, SERVICE_UID):
            try:
                msgs = await watch.client.get_messages(peer, limit=limit)
                break
            except Exception as e:
                errors.append(f"{type(e).__name__}: {e}")
        if msgs is None:
            watch.error = "读取 Telegram 服务号消息失败：" + "；".join(errors)
            log.warning("账号 %s %s", watch.account_id, watch.error)
            return 0

        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=10)
        found = 0
        for m in reversed(list(msgs or [])):
            when = getattr(m, "date", None)
            if when and when < cutoff:
                continue
            if self._absorb(watch, m, "history"):
                found += 1
        return found

    def _absorb(self, watch: Watch, message: Any, source: str) -> bool:
        if message is None or message.id in watch.seen_ids:
            return False
        watch.seen_ids.add(message.id)
        text = getattr(message, "message", "") or ""
        when = getattr(message, "date", None) or dt.datetime.now(dt.timezone.utc)

        # 原文一律留下：抽不出码时前端会把它展示出来，让人能自己读
        watch.seen_msgs.append({
            "text": text[:400],
            "at": when.isoformat() if when else None,
            "source": source,
        })
        watch.seen_msgs[:] = watch.seen_msgs[-10:]

        code = extract_code(text)
        if not code:
            log.info(
                "账号 %s 收到服务号消息但没抽出验证码（%s）：%s",
                watch.account_id, source, text[:80].replace("\n", " "),
            )
            return False
        watch.hits.append(CodeHit(code=code, text=text, at=when, source=source))
        log.info("账号 %s 收到登录验证码：%s（%s）", watch.account_id, code, source)
        return True

    # ------------------------------------------------------------------ 查询
    def status(self, account_id: int) -> dict[str, Any]:
        w = self._watches.get(account_id)
        if w is None:
            return {"active": False, "remaining": 0, "codes": [], "error": ""}
        return {
            "active": w.active,
            "remaining": w.remaining,
            # 最新的排前面
            "codes": [h.as_dict() for h in sorted(w.hits, key=lambda x: x.at, reverse=True)],
            "messages": list(reversed(w.seen_msgs)),
            "error": w.error,
        }

    # ------------------------------------------------------------------ 结束
    async def stop(self, account_id: int) -> None:
        w = self._watches.pop(account_id, None)
        if w is None:
            return
        if w.task and not w.task.done():
            w.task.cancel()
        try:
            if w.handler is not None:
                w.client.remove_event_handler(w.handler)
        except Exception as e:
            log.debug("移除验证码监听器失败：%s", e)

    async def stop_all(self) -> None:
        for aid in list(self._watches):
            await self.stop(aid)

    async def _expire_later(self, account_id: int, seconds: int) -> None:
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            return
        w = self._watches.get(account_id)
        if w is None:
            return
        # 到期只摘掉监听器，结果先留着让前端取走
        try:
            if w.handler is not None:
                w.client.remove_event_handler(w.handler)
                w.handler = None
        except Exception:
            pass
        log.info("账号 %s 的验证码监听已到期", account_id)
