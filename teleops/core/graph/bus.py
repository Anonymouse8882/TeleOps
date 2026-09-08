"""消息总线：graph_messages 表就是队列。

三条硬约束决定了它的写法：
  * 出队必须原子——`UPDATE ... WHERE id IN (子查询 ORDER BY ... LIMIT n) RETURNING`。
    LIMIT/ORDER BY 不能直接挂在 UPDATE 上，本机的 SQLite 没编译那个选项。
  * 空闲必须真的空闲——没有待办就无限期 await 一个 Event，有延迟消息就
    wait_for 到最近的到期时刻，绝不轮询。
  * 崩溃恢复不能依赖优雅关停——Windows 上 Electron 是 taskkill /T /F 零宽限，
    所以靠启动时把 running 打回 pending，而不是靠退出前 drain。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import uuid
from typing import Any

from sqlalchemy import DateTime, bindparam, func, select, text, update

from ...db.models import GraphMessage, utcnow
from ...db.session import session_scope

log = logging.getLogger(__name__)

TERMINAL = ("done", "failed", "parked")


def new_id() -> str:
    return uuid.uuid4().hex[:16]


JSON_COLS = ("payload", "meta", "path", "parent_origins")


def _decode(row: dict[str, Any]) -> dict[str, Any]:
    for k in JSON_COLS:
        v = row.get(k)
        if isinstance(v, str):
            try:
                row[k] = json.loads(v)
            except ValueError:
                row[k] = {} if k in ("payload", "meta") else []
        elif v is None:
            row[k] = {} if k in ("payload", "meta") else []
    return row


class MessageBus:
    """队列的读写口 + 一个唤醒信号。"""

    def __init__(self) -> None:
        # 有新消息入队、或有消息变成可见时 set，消费者靠它从无限等待里醒来
        self.wake = asyncio.Event()

    # ------------------------------------------------------------------ 入队
    async def enqueue(self, session: Any, msgs: list[dict[str, Any]]) -> None:
        """在调用方的事务里插入消息。事务提交后记得 notify()。"""
        for m in msgs:
            session.add(GraphMessage(**m))

    def notify(self) -> None:
        self.wake.set()

    @staticmethod
    def build(*, workflow_id: int, node_id: str, payload: dict[str, Any],
              execution_id: str, origin_id: str = "", src_node_id: str = "",
              meta: dict[str, Any] | None = None, hop: int = 0,
              path: list[str] | None = None, visible_at: dt.datetime | None = None,
              parent_origins: list[str] | None = None) -> dict[str, Any]:
        mid = new_id()
        return {
            "message_id": mid, "workflow_id": workflow_id, "execution_id": execution_id,
            "origin_id": origin_id or mid, "parent_origins": list(parent_origins or []),
            "node_id": node_id, "src_node_id": src_node_id, "status": "pending",
            "visible_at": visible_at or utcnow(), "payload": payload or {},
            "meta": dict(meta or {}), "hop": hop, "path": list(path or []),
        }

    # ------------------------------------------------------------------ 出队
    async def claim(self, limit: int = 1) -> list[dict[str, Any]]:
        """原子领取若干条到期消息，置为 running 并返回快照。

        返回字典而不是 ORM 对象：领取和执行不共用会话，执行期间要调插件（可能
        几十秒），不能占着事务。
        """
        now = utcnow()
        async with session_scope() as s:
            rows = (await s.execute(
                text("""
                    UPDATE graph_messages
                       SET status='running', attempts = attempts + 1, updated_at = :now
                     WHERE id IN (
                           SELECT id FROM graph_messages
                            WHERE status='pending' AND visible_at <= :now
                            ORDER BY visible_at, id
                            LIMIT :limit)
                 RETURNING id, message_id, workflow_id, execution_id, origin_id, parent_origins,
                           node_id, src_node_id, payload, meta, hop, path, attempts
                """).bindparams(bindparam("now", type_=DateTime)),
                {"now": now, "limit": limit},
            )).mappings().all()
        # 走的是原始 SQL，SQLAlchemy 不知道列类型，JSON 列回来是字符串，得自己解
        return [_decode(dict(r)) for r in rows]

    # -------------------------------------------------------------- 状态流转
    async def finish(self, session: Any, msg_id: int, status: str, error: str = "") -> None:
        await session.execute(
            update(GraphMessage).where(GraphMessage.id == msg_id)
            .values(status=status, error=error[:2000], updated_at=utcnow())
        )

    async def retry_later(self, msg_id: int, delay: float, error: str = "") -> None:
        async with session_scope() as s:
            await s.execute(
                update(GraphMessage).where(GraphMessage.id == msg_id).values(
                    status="pending", error=error[:2000],
                    visible_at=utcnow() + dt.timedelta(seconds=delay), updated_at=utcnow())
            )
        self.notify()

    # ------------------------------------------------------------------ 调度
    async def next_deadline(self) -> dt.datetime | None:
        """最近一条待办的可见时刻。None 表示队列里没有待办，可以无限期睡。"""
        async with session_scope() as s:
            return (await s.execute(
                select(func.min(GraphMessage.visible_at)).where(GraphMessage.status == "pending")
            )).scalar()

    async def recover_orphans(self) -> int:
        """启动时把上次没跑完的 running 打回 pending。

        单消费者模型下这是精确的：进程刚起来，不可能有别人正在处理它们。
        """
        async with session_scope() as s:
            r = await s.execute(
                update(GraphMessage).where(GraphMessage.status == "running")
                .values(status="pending", updated_at=utcnow())
            )
            n = r.rowcount or 0
        if n:
            log.info("恢复上次中断的消息 %d 条", n)
        return n

    async def pending_count(self, workflow_id: int | None = None) -> int:
        async with session_scope() as s:
            q = select(func.count(GraphMessage.id)).where(GraphMessage.status == "pending")
            if workflow_id:
                q = q.where(GraphMessage.workflow_id == workflow_id)
            return (await s.execute(q)).scalar() or 0
