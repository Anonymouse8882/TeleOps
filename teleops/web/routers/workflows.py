"""工作流管理：增删改查、挂载频道、手动运行、试运行、运行记录。"""
from __future__ import annotations

from typing import Any

from apscheduler.triggers.cron import CronTrigger
from fastapi import APIRouter, HTTPException
from sqlalchemy import and_, delete, func, select, update
from sqlalchemy.orm import selectinload

from ...core.graph.runtime import job_id as source_job_id
from ...core.scheduler import parse_interval_spec
from ...core.timing import describe_days, describe_window, parse_window, utc_iso
from ...db.models import (
    Channel, GraphBranchPause, GraphEdge, GraphMergeBuffer, GraphMessage, GraphNode, GraphNodeRun,
    GraphNodeState,
    RunLog, SeenItem, Workflow, WorkflowState, WorkflowTarget,
)
from ..deps import DbDep, EngineDep, ok
from ..schemas import RunIn, WorkflowIn, WorkflowPatch

router = APIRouter(prefix="/api/workflows", tags=["workflows"])

_SCALARS = (
    "name", "description", "enabled", "source_plugin", "source_config",
    "sink_plugin", "sink_config", "schedule_type", "schedule_value", "jitter",
    "active_hours", "active_days", "daily_limit", "timezone",
    "account_id", "max_items_per_run", "send_interval", "dedup", "dry_run", "options",
)


def _schedule_label(stype: str, value: str, jitter: int = 0) -> str:
    """一个信息源节点的调度说明。图模式下调度在节点上，工作流那几列已经没人执行了。"""
    stype = (stype or "manual").lower()
    if stype == "manual":
        return "手动触发"
    if stype == "immediate":
        return "启用时触发一次"
    if stype == "cron":
        return f"cron {value}"
    # 空值不是"没配"：parse_interval_spec 对空串回落到 600 秒，_build_trigger
    # 也真按 600 秒建任务。这里跟着回落，否则卡片上显示的是"每 "，后面什么都没有。
    value = str(value or "").strip() or "600"
    unit = " 秒" if value.isdigit() else ""
    return f"每 {value}{unit}" + (f" ±{jitter} 秒" if jitter else "")


async def _source_map(db, engine, ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    """每条工作流的信息源节点：真实的调度和各自的下次运行时间。

    卡片原来显示的是 workflow.schedule_value，那是线性时代的字段，图模式下
    没有任何东西执行它——一条每 10 秒采一次的工作流，卡片上照样写"每 600 秒"。
    """
    out: dict[int, list[dict[str, Any]]] = {}
    if not ids:
        return out
    rows = (await db.execute(
        select(GraphNode).where(GraphNode.workflow_id.in_(ids), GraphNode.kind == "source")
        .order_by(GraphNode.id)
    )).scalars().all()
    sched = engine.scheduler.sched if engine else None
    for n in rows:
        job = sched.get_job(source_job_id(n.workflow_id, n.node_id)) if sched else None
        out.setdefault(n.workflow_id, []).append({
            "node_id": n.node_id,
            "title": n.title or n.node_id,
            "enabled": n.enabled,
            "schedule_type": n.schedule_type,
            "schedule_label": _schedule_label(n.schedule_type, n.schedule_value, n.jitter or 0),
            "next_run_at": utc_iso(getattr(job, "next_run_time", None)),
        })
    return out


def _dump(wf: Workflow, channels: dict[int, Channel], engine=None,
          sources: dict[int, list[dict[str, Any]]] | None = None) -> dict[str, Any]:
    next_run = engine.scheduler.next_run(wf.id) if engine else None
    return {
        "id": wf.id,
        "name": wf.name,
        "description": wf.description,
        "enabled": wf.enabled,
        "source_plugin": wf.source_plugin,
        "source_config": wf.source_config or {},
        "filters": wf.filters or [],
        "formatters": wf.formatters or [],
        "sink_plugin": wf.sink_plugin,
        "sink_config": wf.sink_config or {},
        "schedule_type": wf.schedule_type,
        "schedule_value": wf.schedule_value,
        "jitter": wf.jitter,
        "active_hours": wf.active_hours or "",
        "active_days": wf.active_days or [],
        "daily_limit": wf.daily_limit or 0,
        "timezone": wf.timezone or "",
        "window_label": describe_window(wf.active_hours or ""),
        "days_label": describe_days(wf.active_days or []),
        "account_id": wf.account_id,
        "max_items_per_run": wf.max_items_per_run,
        "send_interval": wf.send_interval,
        "dedup": wf.dedup,
        "dry_run": wf.dry_run,
        "options": wf.options or {},
        "last_run_at": wf.last_run_at.isoformat() if wf.last_run_at else None,
        "last_status": wf.last_status,
        "last_note": wf.last_note or "",
        "next_run_at": utc_iso(next_run) or utc_iso(wf.next_run_at),
        "sources": (sources or {}).get(wf.id, []),
        "total_sent": wf.total_sent,
        "total_runs": wf.total_runs or 0,
        "running": False,
        "targets": [
            {
                "channel_id": t.channel_id,
                "enabled": t.enabled,
                "overrides": t.overrides or {},
                "title": channels[t.channel_id].title if t.channel_id in channels else "(已删除)",
                "peer": channels[t.channel_id].peer if t.channel_id in channels else "",
            }
            for t in sorted(wf.targets, key=lambda x: x.position)
        ],
    }


async def _channel_map(db) -> dict[int, Channel]:
    rows = (await db.execute(select(Channel))).scalars().all()
    return {c.id: c for c in rows}


@router.get("")
async def list_workflows(db: DbDep, engine: EngineDep):
    rows = (
        await db.execute(select(Workflow).options(selectinload(Workflow.targets)).order_by(Workflow.id))
    ).scalars().all()
    chans = await _channel_map(db)
    srcs = await _source_map(db, engine, [wf.id for wf in rows])
    return ok([_dump(wf, chans, engine, srcs) for wf in rows])


@router.get("/{workflow_id}")
async def get_workflow(workflow_id: int, db: DbDep, engine: EngineDep):
    wf = await _need(db, workflow_id)
    return ok(_dump(wf, await _channel_map(db), engine,
                    await _source_map(db, engine, [wf.id])))


@router.post("")
async def create_workflow(payload: WorkflowIn, db: DbDep, engine: EngineDep):
    _validate(engine, payload)
    wf = Workflow(
        **{k: getattr(payload, k) for k in _SCALARS},
        filters=[s.model_dump() for s in payload.filters],
        formatters=[s.model_dump() for s in payload.formatters],
    )
    db.add(wf)
    await db.flush()
    await _set_targets(db, wf, [t.model_dump() for t in payload.targets])
    await db.flush()
    await db.refresh(wf, ["targets"])
    await db.commit()  # 调度器在另一个连接里读库，必须先提交
    await engine.resync()
    return ok(_dump(wf, await _channel_map(db), engine,
                    await _source_map(db, engine, [wf.id])))


@router.patch("/{workflow_id}")
async def update_workflow(workflow_id: int, payload: WorkflowPatch, db: DbDep, engine: EngineDep):
    wf = await _need(db, workflow_id)
    data = payload.model_dump(exclude_unset=True)

    for k in _SCALARS:
        if k in data and data[k] is not None:
            setattr(wf, k, data[k])
    if data.get("filters") is not None:
        wf.filters = [s if isinstance(s, dict) else s.model_dump() for s in data["filters"]]
    if data.get("formatters") is not None:
        wf.formatters = [s if isinstance(s, dict) else s.model_dump() for s in data["formatters"]]
    if data.get("targets") is not None:
        await _set_targets(db, wf, data["targets"])

    # 合并后再校验，这样只改调度不带插件字段的请求也能查出问题
    _validate(engine, wf)

    await db.flush()
    await db.refresh(wf, ["targets"])
    await db.commit()
    await engine.resync()
    return ok(_dump(wf, await _channel_map(db), engine,
                    await _source_map(db, engine, [wf.id])))


@router.delete("/{workflow_id}")
async def delete_workflow(workflow_id: int, db: DbDep, engine: EngineDep):
    wf = await _need(db, workflow_id)
    # 显式清干净附属数据：id 会被后建的工作流复用，留下孤儿记录会串档
    for model in (RunLog, SeenItem, WorkflowState, WorkflowTarget,
                  GraphNode, GraphEdge, GraphMessage, GraphNodeState, GraphNodeRun,
                  GraphMergeBuffer, GraphBranchPause):
        await db.execute(delete(model).where(model.workflow_id == workflow_id))
    await db.delete(wf)
    await db.commit()
    # 删掉之后要重建定时任务，否则残留的任务到点还会触发一条已经不存在的工作流
    await engine.resync()
    return ok()


@router.post("/{workflow_id}/toggle")
async def toggle(workflow_id: int, db: DbDep, engine: EngineDep):
    wf = await _need(db, workflow_id)
    wf.enabled = not wf.enabled
    parked = 0
    if not wf.enabled:
        # 「停用」是急停：队列里在途的消息（包括手动触发那一批）一起挂起，
        # 不然有 delay 节点时它们还会在几小时后冒出来
        r = await db.execute(
            update(GraphMessage)
            .where(GraphMessage.workflow_id == workflow_id,
                   GraphMessage.status.in_(("pending", "running")))
            .values(status="parked", error="工作流已停用")
        )
        parked = r.rowcount or 0
    await db.commit()
    await engine.resync()
    fired: list[str] = []
    if wf.enabled:
        # 「启用时触发一次」得真的触发一次。_build_trigger 对 immediate 返回 None
        # （不建周期任务），这里再不投的话这个选项就是一句空话：节点永远不会自动跑，
        # 而卡片上白纸黑字写着"启用时触发一次"。
        for nid in (await db.execute(
            select(GraphNode.node_id).where(
                GraphNode.workflow_id == workflow_id,
                GraphNode.kind == "source",
                GraphNode.enabled.is_(True),
                func.lower(GraphNode.schedule_type) == "immediate")
        )).scalars().all():
            # 按 schedule 投而不是 manual：上一批没跑完时该跳过就跳过，
            # 反复开关不该把队列越堆越长
            await engine.graph.trigger(workflow_id, nid, trigger="schedule")
            fired.append(nid)
    return ok({"enabled": wf.enabled, "parked": parked, "triggered": fired})


@router.post("/{workflow_id}/run")
async def run_now(workflow_id: int, payload: RunIn, db: DbDep, engine: EngineDep):
    await _need(db, workflow_id)
    await db.commit()  # 释放连接，运行可能耗时较久

    if payload.dry_run:
        # 试运行不走队列：一次有界的同步遍历，不发送、不推游标、不写去重账，
        # 这样"点一下看预览"的交互才成立
        from ...core.graph.dryrun import dry_run as graph_dry_run
        try:
            d = await graph_dry_run(engine.graph.executor, workflow_id)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return ok({"status": "ok", **d})
    busy = (await db.execute(
        select(func.count(GraphMessage.id)).where(
            GraphMessage.workflow_id == workflow_id,
            GraphMessage.status.in_(("pending", "running")))
    )).scalar() or 0
    if busy:
        raise HTTPException(409, f"上一批还在跑（队列里还有 {busy} 条），等它跑完再触发。")
    r = await engine.graph.trigger_workflow(workflow_id, trigger="manual")
    return ok({"status": "queued", **r})


@router.get("/{workflow_id}/runs")
async def runs(workflow_id: int, db: DbDep, limit: int = 30):
    # 夹住上限：下面要把这一页的 execution_id 塞进 IN(...)，而 SQLite 的变量
    # 上限是 32766，超了整个接口 500。负数也要挡——SQLAlchemy 原样下发
    # LIMIT -1，SQLite 视为不限行数，会把整张 run_logs 读出来。
    limit = max(1, min(limit, 500))
    rows = (
        await db.execute(
            select(RunLog).where(RunLog.workflow_id == workflow_id)
            .order_by(RunLog.id.desc()).limit(limit)
        )
    ).scalars().all()
    # 一条工作流可以有好几个信息源，各自按各自的节奏触发，各自留一条运行记录。
    # 不说是哪个源的话，"采集 0 条"（某个增量源本轮没有新内容）和"发送失败"
    # 在表格里长得一模一样，只能靠猜。
    ids = [r.execution_id for r in rows if r.execution_id]
    origin: dict[str, str] = {}
    if ids:
        # 标题是可以重名的，而且默认就是插件名——两个 tg_channel 源摆在一起，
        # 只写标题等于没说。重名的才补上节点号，不重名的保持干净。
        titles = [(n.node_id, n.title or n.node_id) for n in (await db.execute(
            select(GraphNode).where(GraphNode.workflow_id == workflow_id,
                                    GraphNode.kind == "source")
        )).scalars().all()]
        dup = {t for i, (_, t) in enumerate(titles) if any(
            t == t2 for j, (_, t2) in enumerate(titles) if j != i)}
        name_of = {nid: (f"{t}（{nid}）" if t in dup else t) for nid, t in titles}
        for eid, nid in (await db.execute(
            select(GraphNodeRun.execution_id, GraphNodeRun.node_id)
            .join(GraphNode, and_(GraphNode.workflow_id == GraphNodeRun.workflow_id,
                                  GraphNode.node_id == GraphNodeRun.node_id))
            .where(GraphNodeRun.workflow_id == workflow_id,
                   GraphNodeRun.execution_id.in_(ids),
                   GraphNode.kind == "source")
            .order_by(GraphNodeRun.id)
        )).all():
            prev = origin.get(eid)
            name = name_of.get(nid, nid)
            if prev is None:
                origin[eid] = name
            elif name not in prev.split("、"):
                origin[eid] = f"{prev}、{name}"   # 整条工作流一起触发时会有多个源
    return ok(
        [
            {
                "id": r.id,
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                "status": r.status,
                "trigger": r.trigger,
                "source": origin.get(r.execution_id, ""),
                "fetched": r.fetched,
                "kept": r.kept,
                "sent": r.sent,
                "failed": r.failed,
                "error": r.error,
                "detail": r.detail or [],
            }
            for r in rows
        ]
    )


@router.post("/{workflow_id}/reset")
async def reset_state(workflow_id: int, db: DbDep, mode: str = "all"):
    """重置游标 / 去重记录。mode: all | cursor | dedup

    两种执行模式的状态存在不同的表里：线性执行器的游标和取样池在 workflow_state、
    去重在 seen_items；图模式的都在 graph_node_state（去重是 seen: 前缀的键）。
    两边都清——只清一边的话，切过模式的工作流点了没有任何效果，界面还照样弹「已重置」。

    游标重置连带清掉在途的结算快照：留着的话，那批内容跑完会拿旧现场把游标又推回去。
    """
    await _need(db, workflow_id)
    cleared = {"cursor": 0, "dedup": 0}
    if mode in ("all", "cursor"):
        r = await db.execute(
            delete(WorkflowState).where(WorkflowState.workflow_id == workflow_id))
        cleared["cursor"] += r.rowcount or 0
        r = await db.execute(delete(GraphNodeState).where(
            GraphNodeState.workflow_id == workflow_id,
            ~GraphNodeState.key.like("seen:%")))
        cleared["cursor"] += r.rowcount or 0
    if mode in ("all", "dedup"):
        r = await db.execute(delete(SeenItem).where(SeenItem.workflow_id == workflow_id))
        cleared["dedup"] += r.rowcount or 0
        r = await db.execute(delete(GraphNodeState).where(
            GraphNodeState.workflow_id == workflow_id,
            GraphNodeState.key.like("seen:%")))
        cleared["dedup"] += r.rowcount or 0
    await db.commit()
    return ok({"mode": mode, **cleared})


@router.get("/{workflow_id}/state")
async def get_state(workflow_id: int, db: DbDep):
    rows = (
        await db.execute(select(WorkflowState).where(WorkflowState.workflow_id == workflow_id))
    ).scalars().all()
    graph_rows = (
        await db.execute(select(GraphNodeState).where(GraphNodeState.workflow_id == workflow_id))
    ).scalars().all()
    seen = (
        await db.execute(select(SeenItem.id).where(SeenItem.workflow_id == workflow_id))
    ).all()
    return ok({
        "state": {r.key: (r.value or {}).get("v") for r in rows},
        "graph_state": {f"{r.node_id}/{r.key}": (r.value or {}).get("v") for r in graph_rows
                        if not r.key.startswith("seen:")},
        "seen_count": len(seen) + sum(1 for r in graph_rows if r.key.startswith("seen:")),
    })


# ------------------------------------------------------------------- 内部
async def _need(db, workflow_id: int) -> Workflow:
    wf = (
        await db.execute(
            select(Workflow).options(selectinload(Workflow.targets)).where(Workflow.id == workflow_id)
        )
    ).scalar_one_or_none()
    if wf is None:
        raise HTTPException(404, "工作流不存在")
    return wf


async def _set_targets(db, wf: Workflow, targets: list[dict[str, Any]]) -> None:
    await db.execute(delete(WorkflowTarget).where(WorkflowTarget.workflow_id == wf.id))
    for i, t in enumerate(targets or []):
        db.add(
            WorkflowTarget(
                workflow_id=wf.id,
                channel_id=int(t["channel_id"]),
                position=i,
                enabled=bool(t.get("enabled", True)),
                overrides=t.get("overrides") or {},
            )
        )


def _validate(engine, payload) -> None:
    """校验工作流级的设置。

    采集节奏、插件、过滤链这些都在节点上，由保存图时的 validate_graph 负责；
    这里只管仍然挂在工作流行上、被图运行时读取的那几项：
    活跃时段、活跃星期、日配额。
    """
    try:
        parse_window(getattr(payload, "active_hours", "") or "")
    except ValueError as e:
        raise HTTPException(400, str(e))

    for d in getattr(payload, "active_days", None) or []:
        if int(d) not in range(1, 8):
            raise HTTPException(400, f"运行星期取值应为 1–7，收到 {d}")

    # —— 插件与其配置 ——
    reg = engine.registry
    checks: list[tuple[str, dict[str, Any]]] = []
    if getattr(payload, "source_plugin", None):
        checks.append((payload.source_plugin, getattr(payload, "source_config", None) or {}))
    if getattr(payload, "sink_plugin", None):
        checks.append((payload.sink_plugin, getattr(payload, "sink_config", None) or {}))
    for step in (getattr(payload, "filters", None) or []) + (getattr(payload, "formatters", None) or []):
        # 创建时是 pydantic 对象，更新时是已落库的 dict，两种都要吃得下
        name = step["plugin"] if isinstance(step, dict) else step.plugin
        cfg = (step.get("config") if isinstance(step, dict) else step.config) or {}
        checks.append((name, cfg))

    for name, cfg in checks:
        lp = reg.get(name)
        if lp is None:
            raise HTTPException(400, f"插件 {name} 不存在（可能未加载或已删除）")
        errs = lp.cls.validate_config(lp.cls.merge_defaults(cfg))
        if errs:
            raise HTTPException(400, f"[{lp.meta.display_name}] " + "；".join(errs))
