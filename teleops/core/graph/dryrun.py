"""图的试运行：一次同步的有界遍历，不入队、不落库、不发送。

为什么不走队列：试运行是这个后台最高频的调试动作，现在的交互是"点一下就看到
预览条目"。改成入队就变成两步（去画布看记录），而 dry-run 按定义不发送、不推
游标、不写去重账，本来就不需要队列的任何保证。

有界的意思是：跳数预算 + 墙钟超时。图里允许有永久环，没有这两个上限的话，
一个环会让这个 HTTP 请求永远不返回。
"""
from __future__ import annotations

import logging
import time
from collections import deque
from typing import Any

from sqlalchemy import select

from ...db.models import Account, Channel, GraphEdge, GraphNode, Workflow
from ...db.session import session_scope
from ..context import RunContext
from ..item import Item
from .executor import NodeResult
from .state import NodeStateStore

log = logging.getLogger(__name__)

MAX_HOPS = 300
MAX_SECONDS = 25.0


async def dry_run(executor: Any, workflow_id: int, *, max_hops: int = MAX_HOPS,
                  max_seconds: float = MAX_SECONDS) -> dict[str, Any]:
    async with session_scope() as s:
        wf = (await s.execute(select(Workflow).where(Workflow.id == workflow_id))).scalar_one_or_none()
        if wf is None:
            raise ValueError("工作流不存在")
        nodes = {n.node_id: n for n in (await s.execute(
            select(GraphNode).where(GraphNode.workflow_id == workflow_id))).scalars().all()}
        edges = (await s.execute(
            select(GraphEdge).where(GraphEdge.workflow_id == workflow_id,
                                    GraphEdge.enabled.is_(True))
            .order_by(GraphEdge.position, GraphEdge.id))).scalars().all()
        account = await s.get(Account, wf.account_id) if wf.account_id else None
        channels = {c.id: c for c in (await s.execute(select(Channel))).scalars().all()}
        s.expunge_all()

    if not nodes:
        raise ValueError("这条工作流还没有图")

    out_edges: dict[str, list[GraphEdge]] = {}
    for e in edges:
        out_edges.setdefault(e.src_node, []).append(e)

    result: dict[str, Any] = {
        "fetched": 0, "kept": 0, "sent": 0, "failed": 0,
        "items": [], "trace": [], "truncated": False, "error": "",
    }
    queue: deque[tuple[str, Item | None, int]] = deque(
        (n.node_id, None, 0) for n in nodes.values() if n.kind == "source" and n.enabled)
    if not queue:
        raise ValueError("图里没有启用的信息源节点")

    deadline = time.monotonic() + max_seconds
    hops = 0
    fetched_sources: set[str] = set()
    limit_counts: dict[str, int] = {}
    last_hop = [0.0]

    async def _pace() -> None:
        """对外请求的节奏不能比真跑还快。真实运行有 20 跳/秒的闸门，这里对齐。"""
        import asyncio
        gap = 0.05 - (time.monotonic() - last_hop[0])
        if gap > 0:
            await asyncio.sleep(gap)
        last_hop[0] = time.monotonic()
    while queue:
        if hops >= max_hops or time.monotonic() > deadline:
            result["truncated"] = True
            result["error"] = (f"试运行到 {hops} 跳就停了（上限 {max_hops} 跳 / {max_seconds:.0f} 秒）。"
                               "图里有环时这是正常的，只是预览不会跑完。")
            break
        nid, item, hop = queue.popleft()
        node = nodes.get(nid)
        if node is None or not node.enabled:
            continue
        if node.kind == "source":
            # 图里允许有环，而环回到信息源就意味着再去 Telegram 拉一次。
            # 试运行里一个源只采一次：否则点一次预览就是几十次真实请求打同一个账号，
            # 而且预览上还会摆着一个虚高的采集条数。
            if nid in fetched_sources:
                result["trace"].append({"node_id": nid, "title": node.title or nid,
                                        "kind": node.kind, "status": "drop", "produced": 0,
                                        "error": "试运行里同一个信息源只采集一次"})
                continue
            fetched_sources.add(nid)
        hops += 1
        await _pace()

        ctx = RunContext(wf, executor.settings, executor.clients, account,
                         dry_run=True, trigger="test", registry=executor.registry)
        # 读真表（增量源要看到真实游标起点），写只进内存
        ctx.state = NodeStateStore(workflow_id, nid, readonly=True)
        plugin = None
        try:
            plugin = await executor._build_plugin(node, ctx)
            res = await _run_node(executor, node, ctx, item, plugin, channels, limit_counts)
        except Exception as e:
            res = NodeResult(status="error", error=f"{type(e).__name__}: {e}")
        finally:
            if plugin is not None:
                try:
                    await plugin.teardown(ctx)
                except Exception:
                    pass
            await ctx.close()

        result["trace"].append({
            "node_id": nid, "title": node.title or node.plugin or nid, "kind": node.kind,
            "status": res.status, "produced": len(res.items), "error": res.error,
        })
        if node.kind == "source":
            result["fetched"] += len(res.items)
        if res.status == "error":
            result["failed"] += 1
        if node.kind == "output" and res.status == "ok":
            result["sent"] += 1
            result["kept"] += 1
            if item is not None and len(result["items"]) < 10:
                result["items"].append(item.to_dict())

        if res.status != "ok":
            continue
        for e in out_edges.get(nid, []):
            if e.src_port != res.port:
                continue
            for it in res.items:
                queue.append((e.dst_node, it, hop + 1))

    return result


async def _run_node(executor: Any, node: GraphNode, ctx: RunContext, item: Item | None,
                    plugin: Any, channels: dict[int, Channel],
                    counts: dict[str, int] | None = None) -> NodeResult:
    """按节点类型跑一跳。两类节点在试运行里要特殊处理。"""
    msg = {
        "id": 0, "message_id": f"dry:{node.node_id}", "workflow_id": node.workflow_id,
        "execution_id": "", "origin_id": "dry", "parent_origins": [], "node_id": node.node_id,
        "src_node_id": "", "payload": item.to_wire() if item else {},
        "meta": {"trigger": "test"}, "hop": 0, "path": [], "attempts": 1,
    }
    if node.kind == "delay":
        # 试运行不等，直接放行并说明——不然预览要等到天亮
        return NodeResult(items=[item] if item else [],
                          detail={"note": "试运行跳过了延迟"})
    if node.kind == "merge":
        # 合流要跨消息攒数据、要写缓冲表，试运行里一律当直通，并在轨迹里注明
        return NodeResult(items=[item] if item else [],
                          detail={"note": "试运行里合流按直通处理"})
    if node.kind == "limit":
        # 执行器那套是按 execution_id 数执行记录的，而试运行不写执行记录，
        # 照搬的话计数恒为 0、限量等于不存在，预览的条数会比真跑多
        cap = int((node.config or {}).get("count") or 0)
        counts = counts if counts is not None else {}
        passed = counts.get(node.node_id, 0)
        if cap > 0 and passed >= cap:
            return NodeResult(status="drop", error=f"本轮已放行 {passed} 条，达到上限 {cap}")
        counts[node.node_id] = passed + 1
        return NodeResult(items=[item] if item else [])
    handler = getattr(executor, f"_run_{node.kind}", None)
    if handler is None:
        return NodeResult(status="error", error=f"不认识的节点类型：{node.kind}")
    return await handler(node, ctx, msg, plugin=plugin, channels=channels)
