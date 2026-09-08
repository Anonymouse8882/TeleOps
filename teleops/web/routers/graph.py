"""图工作流：整图读写、从线性迁移、节点类型清单。

整图保存用「删光重插」，和现有 _set_targets 的做法一致；节点的运行时状态
（熔断标记）会在重插时按 node_id 带回来，不然每保存一次画布就把运行时状态
抹掉了。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import selectinload

from ...core.graph import GraphError, describe, linear_to_graph, validate_graph
from ...db.backup import backup_database
from ...db.models import (
    Channel, GraphBranchPause, GraphEdge, GraphMergeBuffer, GraphMessage, GraphNode,
    GraphNodeRun, GraphNodeState, Workflow, utcnow,
)
from ..deps import DbDep, EngineDep, ok
from ..schemas import GraphIn

router = APIRouter(prefix="/api", tags=["graph"])


def _viewport(wf: Workflow) -> dict[str, Any]:
    return ((wf.options or {}).get("graph") or {}).get("viewport") or {}


def _set_graph_option(wf: Workflow, **kw: Any) -> None:
    # options 是 JSON 列，原地改检测不到，必须整体重新赋值
    options = dict(wf.options or {})
    g = dict(options.get("graph") or {})
    g.update(kw)
    options["graph"] = g
    wf.options = options


async def _need_idle(db: Any, workflow_id: int) -> None:
    """上一批还没跑完就别再投。

    定时触发早就有这道闸（GraphRuntime._busy），手动触发一直没有：一次采集
    要跑几秒到几十秒，用户看不到反馈就会继续点，每点一次往队列里再压一批，
    单消费者串行处理、输出端还有发送间隔，几下就能让它持续跑好几分钟。
    """
    # 排期到将来的消息不算"还在跑"：Delay 节点等待中的消息状态就是 pending
    # （靠 visible_at 排期），常驻环路更是设计上永远有一条在途消息。把它们算
    # 进来的话，一个 2 小时的 Delay 会让手动触发在这两小时里一直 409，环路
    # 工作流则永远没法手动触发。
    n = (await db.execute(
        select(func.count(GraphMessage.id)).where(
            GraphMessage.workflow_id == workflow_id,
            GraphMessage.status.in_(("pending", "running")),
            or_(GraphMessage.status == "running", GraphMessage.visible_at <= utcnow()))
    )).scalar() or 0
    if n:
        raise HTTPException(409, f"上一批还在跑（队列里还有 {n} 条），等它跑完再触发。")


async def _need(db: Any, workflow_id: int, *, with_targets: bool = False) -> Workflow:
    if with_targets:
        # 异步会话下不能靠惰性加载取关系，必须显式 selectinload
        wf = (await db.execute(
            select(Workflow).options(selectinload(Workflow.targets))
            .where(Workflow.id == workflow_id)
        )).scalar_one_or_none()
    else:
        wf = await db.get(Workflow, workflow_id)
    if wf is None:
        raise HTTPException(404, "工作流不存在")
    return wf


def _dump_node(n: GraphNode) -> dict[str, Any]:
    return {
        "node_id": n.node_id, "kind": n.kind, "plugin": n.plugin, "config": n.config or {},
        "title": n.title, "x": n.x, "y": n.y, "enabled": n.enabled,
        "schedule_type": n.schedule_type, "schedule_value": n.schedule_value, "jitter": n.jitter,
        "status": n.status, "status_note": n.status_note,
    }


def _dump_edge(e: GraphEdge) -> dict[str, Any]:
    return {"src_node": e.src_node, "src_port": e.src_port,
            "dst_node": e.dst_node, "dst_port": e.dst_port, "enabled": e.enabled}


@router.get("/graph/node-types")
async def node_types(engine: EngineDep):
    """节点面板：每种节点的端口、配置表单、可选插件。"""
    return ok(describe(engine.registry))


@router.get("/workflows/{workflow_id}/graph")
async def get_graph(workflow_id: int, db: DbDep):
    wf = await _need(db, workflow_id)
    nodes = (await db.execute(
        select(GraphNode).where(GraphNode.workflow_id == workflow_id).order_by(GraphNode.id)
    )).scalars().all()
    edges = (await db.execute(
        select(GraphEdge).where(GraphEdge.workflow_id == workflow_id)
        .order_by(GraphEdge.position, GraphEdge.id)
    )).scalars().all()
    chans = (await db.execute(select(Channel))).scalars().all()
    return ok({
        "workflow": {"id": wf.id, "name": wf.name, "enabled": wf.enabled,
                     "account_id": wf.account_id, "description": wf.description},
        "viewport": _viewport(wf),
        "nodes": [_dump_node(n) for n in nodes],
        "edges": [_dump_edge(e) for e in edges],
        "channels": [{"id": c.id, "title": c.title, "peer": c.peer, "enabled": c.enabled}
                     for c in chans],
    })


@router.put("/workflows/{workflow_id}/graph")
async def put_graph(workflow_id: int, payload: GraphIn, db: DbDep, engine: EngineDep,
                    confirm: str = ""):
    wf = await _need(db, workflow_id)
    nodes = [n.model_dump() for n in payload.nodes]
    edges = [e.model_dump() for e in payload.edges]
    try:
        validate_graph(nodes, edges, engine.registry)
    except GraphError as e:
        raise HTTPException(400, str(e)) from e

    old = (await db.execute(
        select(GraphNode).where(GraphNode.workflow_id == workflow_id)
    )).scalars().all()

    # 把整张图清空是不可逆的，必须显式确认——前端一个渲染 bug 让 nodes 变成空数组，
    # 用户画了半天的东西就没了，而这条路径一次备份都不做。
    if old and not nodes and confirm != "clear":
        raise HTTPException(400, "这会清空整张图。确认请带上 ?confirm=clear")

    # 运行时状态按 (id, 类型, 插件) 带回来。只认 node_id 不行：前端建新节点时会
    # 复用被删掉的最小空号，一个全新的节点会继承旧节点的熔断状态。
    # updated_at 也要一起带回来：熔断冷却是拿它算已冷却时长的，而这里是"删光
    # 重插"，新行的 updated_at 是当前时刻——不带回来的话每存一次画布，600 秒
    # 冷却就从头开始，用户可以无限把自己的节点推迟在熔断里。
    keep = {(n.node_id, n.kind, n.plugin): (n.status, n.status_note, n.updated_at, n.config or {})
            for n in old}

    # 被删掉的节点，它的游标/去重账本/执行记录/在途消息一起清掉。留着的话，
    # 下一个复用同一个 id 的新节点会带着别人的游标开局（历史消息整段漏抓）。
    gone = {n.node_id for n in old} - {n["node_id"] for n in nodes}
    if gone:
        for model in (GraphNodeState, GraphNodeRun, GraphMessage, GraphMergeBuffer):
            await db.execute(delete(model).where(
                model.workflow_id == workflow_id, model.node_id.in_(gone)))

    await db.execute(delete(GraphEdge).where(GraphEdge.workflow_id == workflow_id))
    await db.execute(delete(GraphNode).where(GraphNode.workflow_id == workflow_id))
    now = utcnow()
    for n in nodes:
        status, note, since, cfg = keep.get(
            (n["node_id"], n["kind"], n["plugin"]), ("ok", "", now, None))
        if cfg is not None and (n.get("config") or {}) != cfg:
            # 配置改了就当作"用户已经在修了"，熔断标记清掉重新试一次。否则改好
            # 配置保存完，节点仍显示熔断、消息继续被 park，用户看不到任何好转。
            status, note, since = "ok", "", now
        db.add(GraphNode(workflow_id=workflow_id, status=status, status_note=note,
                         updated_at=since or now, **n))
    for i, e in enumerate(edges):
        db.add(GraphEdge(workflow_id=workflow_id, position=i, **e))
    if payload.viewport:
        _set_graph_option(wf, viewport=payload.viewport)
    await db.commit()
    # 信息源节点的调度写在节点上，图一改就要重建定时任务
    r = await engine.graph.sync_sources()
    # 建不出定时任务的节点要说出来。吞掉的话，这个信息源从此不再自动采集，
    # 而画布上保存成功、节点也不显示异常，用户只能等到发现"它不动了"才知道。
    return ok({"nodes": len(nodes), "edges": len(edges),
               "schedule_errors": r.get("errors") or []})


@router.post("/workflows/{workflow_id}/graph/migrate")
async def migrate_graph(workflow_id: int, db: DbDep, engine: EngineDep):
    """把线性配置转成图。已经有图了就拒绝，免得把用户画的东西覆盖掉。"""
    wf = await _need(db, workflow_id, with_targets=True)
    existing = (await db.execute(
        select(GraphNode.id).where(GraphNode.workflow_id == workflow_id).limit(1)
    )).first()
    if existing:
        raise HTTPException(400, "这条工作流已经有图了，如需重来请先清空画布")

    backup = await backup_database(engine.settings, tag=f"before-graph-wf{workflow_id}")

    targets = [
        {"channel_id": t.channel_id, "enabled": t.enabled, "overrides": t.overrides or {}}
        for t in sorted(wf.targets, key=lambda x: x.position)
    ]
    nodes, edges = linear_to_graph(wf, targets, engine.settings)
    try:
        validate_graph(nodes, edges, engine.registry)
    except GraphError as e:
        raise HTTPException(400, f"转换出的图不合法：{e}") from e

    for n in nodes:
        db.add(GraphNode(workflow_id=workflow_id, **n))
    for i, e in enumerate(edges):
        db.add(GraphEdge(workflow_id=workflow_id, position=i, **e))
    await db.commit()
    # 调度写在刚插进去的信息源节点上，和 put_graph 一样要重建定时任务。
    # 漏掉的话，转成图之后这条工作流就再也不会自动采集，界面上还看不出来。
    r = await engine.graph.sync_sources()
    return ok({"nodes": len(nodes), "edges": len(edges), "backup": str(backup),
               "schedule_errors": r.get("errors") or []})


@router.post("/workflows/{workflow_id}/graph/run")
async def run_graph(workflow_id: int, db: DbDep, engine: EngineDep):
    """手动触发这条工作流的所有信息源节点，立刻返回本次触发的 execution_id。"""
    await _need(db, workflow_id)
    await _need_idle(db, workflow_id)
    r = await engine.graph.trigger_workflow(workflow_id, trigger="manual")
    return ok(r)


@router.post("/workflows/{workflow_id}/graph/nodes/{node_id}/trigger")
async def trigger_node(workflow_id: int, node_id: str, db: DbDep, engine: EngineDep):
    """只触发某一个信息源节点。多源的图里，逐个试比整张图一起跑好定位问题。"""
    await _need(db, workflow_id)
    node = (await db.execute(
        select(GraphNode).where(GraphNode.workflow_id == workflow_id,
                                GraphNode.node_id == node_id)
    )).scalar_one_or_none()
    if node is None:
        raise HTTPException(404, "节点不存在")
    if node.kind != "source":
        raise HTTPException(400, "只有信息源节点可以被触发")
    await _need_idle(db, workflow_id)
    execution_id = await engine.graph.trigger(workflow_id, node_id, trigger="manual")
    return ok({"execution_id": execution_id, "node_id": node_id})


@router.get("/workflows/{workflow_id}/graph/runs")
async def graph_runs(workflow_id: int, db: DbDep, limit: int = 60, execution_id: str = ""):
    """逐跳执行记录。画布上的实时状态和错误就是查它。"""
    q = select(GraphNodeRun).where(GraphNodeRun.workflow_id == workflow_id)
    if execution_id:
        q = q.where(GraphNodeRun.execution_id == execution_id)
    rows = (await db.execute(q.order_by(GraphNodeRun.id.desc()).limit(min(limit, 200)))).scalars().all()
    # parked 也要列出来：被挂起的消息既不在执行记录里也不在待办里的话，
    # 用户看到的就是"这条内容莫名其妙没发出去"，而且没有任何重试的入口
    pending = (await db.execute(
        select(GraphMessage).where(GraphMessage.workflow_id == workflow_id,
                                   GraphMessage.status.in_(("pending", "running", "parked")))
        .order_by(GraphMessage.visible_at.desc()).limit(50)
    )).scalars().all()
    pauses = (await db.execute(
        select(GraphBranchPause).where(GraphBranchPause.workflow_id == workflow_id)
        .order_by(GraphBranchPause.id.desc()).limit(20)
    )).scalars().all()
    nodes = (await db.execute(
        select(GraphNode).where(GraphNode.workflow_id == workflow_id,
                                GraphNode.status != "ok")
    )).scalars().all()
    return ok({
        "pauses": [{
            "id": p.id, "origin_id": p.origin_id, "node_id": p.node_id,
            "reason": p.reason, "note": p.note, "detail": p.detail or {},
            "created_at": p.created_at.isoformat() if p.created_at else None,
        } for p in pauses],
        "bad_nodes": [{"node_id": n.node_id, "status": n.status, "note": n.status_note}
                      for n in nodes],
        "runs": [{
            "id": r.id, "node_id": r.node_id, "status": r.status, "produced": r.produced,
            "message_id": r.message_id,
            "error": r.error, "detail": r.detail or {}, "execution_id": r.execution_id,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
        } for r in rows],
        "queue": [{
            "message_id": m.message_id, "node_id": m.node_id, "status": m.status,
            "visible_at": m.visible_at.isoformat() if m.visible_at else None,
            "hop": m.hop, "execution_id": m.execution_id,
        } for m in pending],
    })


@router.post("/workflows/{workflow_id}/graph/messages/{message_id}/requeue")
async def requeue(workflow_id: int, message_id: str, db: DbDep, engine: EngineDep,
                  force: int = 0):
    """把一条被挂起的消息放回队列。挂起是终态，没有这个入口就只能眼看着它烂在那。

    输出节点上 failed 的消息要显式确认：那是"可能已经发出去一部分"的状态
    （幂等标记只在至少成功一个目标时才写），重投就是整条重发，而发帖不可回滚。
    """
    msg = (await db.execute(
        select(GraphMessage).where(GraphMessage.workflow_id == workflow_id,
                                   GraphMessage.message_id == message_id)
    )).scalar_one_or_none()
    if msg is not None and msg.status == "failed" and not force:
        node = (await db.execute(
            select(GraphNode).where(GraphNode.workflow_id == workflow_id,
                                    GraphNode.node_id == msg.node_id)
        )).scalar_one_or_none()
        if node is not None and node.kind == "output":
            raise HTTPException(
                409, "这条消息在输出节点上失败，可能已经发出去一部分，重投会重复发帖。"
                     "确认要重投请带上 ?force=1")
    r = await db.execute(
        update(GraphMessage)
        .where(GraphMessage.workflow_id == workflow_id,
               GraphMessage.message_id == message_id,
               # failed 的也让重投：分流条件在真实数据上抛错、插件临时故障，
               # 这些都是改完配置就该能重来的
               GraphMessage.status.in_(("parked", "failed")))
        .values(status="pending", attempts=0, error="", visible_at=utcnow())
    )
    await db.commit()
    if not r.rowcount:
        raise HTTPException(404, "没有找到这条可以重投的消息")
    engine.graph.bus.notify()
    return ok({"message_id": message_id})


@router.post("/workflows/{workflow_id}/graph/pauses/{pause_id}/resume")
async def resume_branch(workflow_id: int, pause_id: int, db: DbDep, engine: EngineDep):
    """恢复一条被判定为失控的分支：撤销暂停，并把它挂起的消息放回队列。"""
    p = (await db.execute(
        select(GraphBranchPause).where(GraphBranchPause.workflow_id == workflow_id,
                                       GraphBranchPause.id == pause_id)
    )).scalar_one_or_none()
    if p is None:
        raise HTTPException(404, "没有找到这条暂停记录")
    origin = p.origin_id
    await db.delete(p)
    r = await db.execute(
        update(GraphMessage)
        .where(GraphMessage.workflow_id == workflow_id, GraphMessage.origin_id == origin,
               GraphMessage.status == "parked")
        .values(status="pending", attempts=0, error="", visible_at=utcnow())
    )
    await db.commit()
    # 检测器的内存计数也要清，不然刚恢复就又按旧账跳闸
    # 用户明确恢复过的链，速率兜底不再拦它——否则十分钟后又被拦，
    # 用户只会陷入"恢复 → 又暂停"的循环。内容原地打转仍然照拦。
    engine.graph.detector.forget(workflow_id, origin, rate_exempt=True)
    engine.graph.bus.notify()
    return ok({"origin_id": origin, "requeued": r.rowcount or 0})


@router.post("/workflows/{workflow_id}/graph/nodes/{node_id}/reset")
async def reset_node(workflow_id: int, node_id: str, db: DbDep, engine: EngineDep):
    """解除节点熔断。"""
    r = await db.execute(
        update(GraphNode)
        .where(GraphNode.workflow_id == workflow_id, GraphNode.node_id == node_id)
        .values(status="ok", status_note="")
    )
    if not r.rowcount:
        raise HTTPException(404, "节点不存在")
    # 熔断期间被挡下的消息要一起放回来，否则"恢复"了却什么都没发生，
    # 用户只能去执行记录里一条条点重投
    q = await db.execute(
        update(GraphMessage)
        .where(GraphMessage.workflow_id == workflow_id, GraphMessage.node_id == node_id,
               GraphMessage.status == "parked",
               GraphMessage.error.like("节点已熔断%"))
        .values(status="pending", attempts=0, error="", visible_at=utcnow())
    )
    await db.commit()
    engine.graph.executor._fail_streak.pop((workflow_id, node_id), None)
    if q.rowcount:
        engine.graph.bus.notify()
    return ok({"node_id": node_id, "requeued": q.rowcount or 0})
