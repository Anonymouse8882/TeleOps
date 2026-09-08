"""运行上下文：插件在执行期间能拿到的一切资源。"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

import httpx
from sqlalchemy import delete, select

from ..config import Settings
from ..db.models import Account, SeenItem, Workflow, WorkflowState
from ..db.session import session_scope
from ..tg.client import ClientManager
from .item import Item

log = logging.getLogger(__name__)

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)


class StateStore:
    """工作流级别的键值存储，插件用来记游标（例如上次爬到哪条）。"""

    def __init__(self, workflow_id: int) -> None:
        self.workflow_id = workflow_id
        # workflow_id <= 0 表示临时/测试上下文，只在内存里保存，不落库
        self.persistent = workflow_id > 0
        self._cache: dict[str, Any] = {}

    async def get(self, key: str, default: Any = None) -> Any:
        if key in self._cache:
            return self._cache[key]
        if not self.persistent:
            return default
        async with session_scope() as s:
            row = (
                await s.execute(
                    select(WorkflowState).where(
                        WorkflowState.workflow_id == self.workflow_id, WorkflowState.key == key
                    )
                )
            ).scalar_one_or_none()
        val = row.value.get("v", default) if row else default
        self._cache[key] = val
        return val

    async def set(self, key: str, value: Any) -> None:
        self._cache[key] = value
        if not self.persistent:
            return
        async with session_scope() as s:
            row = (
                await s.execute(
                    select(WorkflowState).where(
                        WorkflowState.workflow_id == self.workflow_id, WorkflowState.key == key
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                s.add(WorkflowState(workflow_id=self.workflow_id, key=key, value={"v": value}))
            else:
                row.value = {"v": value}

    async def delete(self, key: str) -> None:
        self._cache.pop(key, None)
        if not self.persistent:
            return
        async with session_scope() as s:
            await s.execute(
                delete(WorkflowState).where(
                    WorkflowState.workflow_id == self.workflow_id, WorkflowState.key == key
                )
            )

    async def clear(self) -> None:
        self._cache.clear()
        if not self.persistent:
            return
        async with session_scope() as s:
            await s.execute(delete(WorkflowState).where(WorkflowState.workflow_id == self.workflow_id))


class RunContext:
    """一次工作流运行的上下文。"""

    def __init__(
        self,
        workflow: Workflow,
        settings: Settings,
        clients: ClientManager,
        account: Account | None,
        *,
        dry_run: bool = False,
        trigger: str = "schedule",
        registry: Any = None,
    ) -> None:
        self.workflow = workflow
        self.workflow_id = workflow.id
        self.settings = settings
        self.clients = clients
        self.account = account
        self.dry_run = dry_run
        self.trigger = trigger
        self.registry = registry
        self.state = StateStore(workflow.id)
        self.log = logging.getLogger(f"wf.{workflow.id}.{workflow.name}")
        self.traces: list[dict[str, str]] = []
        # 成功投递出去的条目；提交钩子据此推进游标
        self.sent_items: list[Item] = []
        # 被过滤链/格式化丢弃的条目——已经有结论，游标可以安全越过
        self.dropped_items: list[Item] = []
        # 发送或格式化出错的条目——没有结论，游标必须停在它前面等下轮重试
        self.failed_items: list[Item] = []
        self.on_commit: list[Callable[["RunContext"], Any]] = []
        self._http: httpx.AsyncClient | None = None
        self._temp_files: list[Path] = []

    def add_commit_hook(self, fn: Callable[["RunContext"], Any]) -> None:
        """注册"发送成功后"回调。插件用它来安全地推进游标——发送失败的内容
        不会被跳过，下一轮还会重新处理。"""
        self.on_commit.append(fn)

    async def commit(self) -> None:
        for hook in self.on_commit:
            try:
                await hook(self)
            except Exception as e:
                self.log.warning("提交钩子出错：%s", e)

    # ------------------------------------------------------------------ HTTP
    @property
    def http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=30,
                follow_redirects=True,
                headers={"User-Agent": DEFAULT_UA},
            )
        return self._http

    async def http_get(self, url: str, **kw: Any) -> str:
        r = await self.http.get(url, **kw)
        r.raise_for_status()
        return r.text

    async def http_json(self, url: str, **kw: Any) -> Any:
        r = await self.http.get(url, **kw)
        r.raise_for_status()
        return r.json()

    # -------------------------------------------------------------- Telegram
    async def client(self):
        """当前工作流绑定账号的 Telethon 客户端。"""
        if self.account is None:
            raise RuntimeError("该工作流未绑定 Telegram 账号")
        return await self.clients.get(self.account)

    async def client_for(self, account_id: int | None):
        if account_id is None or (self.account and account_id == self.account.id):
            return await self.client()
        async with session_scope() as s:
            acc = await s.get(Account, account_id)
        if acc is None:
            raise RuntimeError(f"账号 {account_id} 不存在")
        return await self.clients.get(acc)

    # ------------------------------------------------------------------ 存储
    def media_dir(self) -> Path:
        d = self.settings.storage.media_dir / f"wf_{self.workflow_id}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def register_temp(self, path: str | Path) -> None:
        """登记临时文件，运行结束后自动清理。"""
        self._temp_files.append(Path(path))

    # ------------------------------------------------------------------ 去重
    async def is_seen(self, key: str) -> bool:
        if self.workflow_id <= 0:
            return False
        async with session_scope() as s:
            row = (
                await s.execute(
                    select(SeenItem.id).where(
                        SeenItem.workflow_id == self.workflow_id, SeenItem.key == key
                    )
                )
            ).first()
        return row is not None

    async def mark_seen(self, key: str) -> None:
        if self.dry_run or self.workflow_id <= 0:
            return
        try:
            async with session_scope() as s:
                s.add(SeenItem(workflow_id=self.workflow_id, key=key))
        except Exception:  # 并发下唯一约束冲突可忽略
            pass

    # ------------------------------------------------------------------ 追踪
    def trace(self, item: Item | None, message: str) -> None:
        self.traces.append({"uid": item.uid if item else "", "msg": message})
        self.log.debug("%s %s", item.uid if item else "-", message)

    # ------------------------------------------------------------------ 清理
    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None
        for p in self._temp_files:
            try:
                if p.exists():
                    p.unlink()
            except OSError:
                pass
        self._temp_files.clear()


ProgressHook = Callable[[str, dict[str, Any]], None]
