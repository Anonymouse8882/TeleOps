"""节点级键值存储。

线性模式下游标和去重都是工作流级（WorkflowState / SeenItem），图里两个
tg_channel 源节点会抢同一行游标，必须按节点隔离。接口和 context.StateStore
保持一致，插件里 `await ctx.state.get(...)` 的写法一个字都不用改。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import delete, select

from ...db.models import GraphNodeState
from ...db.session import session_scope


class NodeStateStore:
    def __init__(self, workflow_id: int, node_id: str, *, readonly: bool = False) -> None:
        self.workflow_id = workflow_id
        self.node_id = node_id
        # readonly 给试运行用：读走真表（插件要看到和真跑一样的游标起点），
        # 写只进内存。不这么做的话，增量抓取的源在试运行里读不到游标，
        # 会走"首轮初始化"分支只记游标然后返回空——预览永远显示没有内容，
        # 而定时跑得好好的。
        self.readonly = readonly
        self.persistent = workflow_id > 0
        self._cache: dict[str, Any] = {}

    def _where(self, key: str | None = None):
        conds = [GraphNodeState.workflow_id == self.workflow_id,
                 GraphNodeState.node_id == self.node_id]
        if key is not None:
            conds.append(GraphNodeState.key == key)
        return conds

    async def get(self, key: str, default: Any = None) -> Any:
        if key in self._cache:
            return self._cache[key]
        if not self.persistent:
            return default
        async with session_scope() as s:
            row = (await s.execute(select(GraphNodeState).where(*self._where(key)))).scalar_one_or_none()
        val = row.value.get("v", default) if row else default
        self._cache[key] = val
        return val

    async def set(self, key: str, value: Any) -> None:
        self._cache[key] = value
        if not self.persistent or self.readonly:
            return
        async with session_scope() as s:
            row = (await s.execute(select(GraphNodeState).where(*self._where(key)))).scalar_one_or_none()
            if row is None:
                s.add(GraphNodeState(workflow_id=self.workflow_id, node_id=self.node_id,
                                     key=key, value={"v": value}))
            else:
                row.value = {"v": value}     # JSON 列已包 Mutable，整体赋值最稳

    async def delete(self, key: str) -> None:
        self._cache.pop(key, None)
        if not self.persistent or self.readonly:
            return
        async with session_scope() as s:
            await s.execute(delete(GraphNodeState).where(*self._where(key)))

    async def clear(self) -> None:
        self._cache.clear()
        if not self.persistent or self.readonly:
            return
        async with session_scope() as s:
            await s.execute(delete(GraphNodeState).where(*self._where()))
