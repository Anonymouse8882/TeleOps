"""顺序抓取（incremental）的游标结算。

线性执行器里，一轮从采集到发送是一个 RunContext，结束时调 ctx.commit()，
插件的提交钩子拿 ctx.sent_items / dropped_items 算出"哪些内容有结论"，
把游标推到第一个没结论的条目之前。

图模式里这件事被拆开了：采集是一跳，发送是很多跳之后（中间可能隔着 Delay、
可能扇出到多条分支、可能永远走不到输出）。所以要等**整次触发跑完**才知道
每条内容的归宿，再补一次结算。两个难点：

1. 采集那一跳的插件实例早就没了，而钩子依赖它 fetch 期间记在实例属性上的
   扫描窗口和产出 id（tg_channel 的 _scan_max / _produced_ids）。
   做法是采集后把实例状态快照下来，结算时还原给同一个类的新实例——
   插件契约一个字都不用改，也不用要求插件自己去落库。
2. "有结论"的口径必须和线性一致：成功发送、或被过滤链/去重明确丢弃，才算有结论；
   **被限量截断的不算**（下轮还要重新处理），失败的更不算。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import and_, select

from ...db.models import Account, GraphMessage, GraphNode, GraphNodeRun, Workflow
from ...db.session import session_scope
from ..context import RunContext
from ..item import Item
from .state import NodeStateStore

log = logging.getLogger(__name__)

SNAPSHOT_PREFIX = "cursor_snapshot:"
# 这些属性不快照：config 会从节点配置重建，log 不可序列化
SKIP_ATTRS = ("config", "log")
# 丢弃发生在这些节点上才算"有结论"。限量节点的丢弃不算——那条内容根本没被处理过，
# 下一轮还要重新来，把它当成有结论会让游标越过它，内容就永久漏掉了。
SETTLING_DROPS = ("filter", "formatter", "dedup", "router", "merge")


def snapshot_plugin(plugin: Any) -> dict[str, Any]:
    """把插件 fetch 之后的实例状态抠出来，只留能 JSON 化的部分。"""
    out: dict[str, Any] = {}
    for k, v in vars(plugin).items():
        if k in SKIP_ATTRS or k.startswith("__"):
            continue
        try:
            # 严格判定，不给 default：加了 default=str 的话任何对象都能"序列化"成
            # 一段字符串，还原时插件就会拿到一个字符串冒充的客户端。
            json.dumps(v, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError):
            continue
        out[k] = v
    return out


def restore_plugin(plugin: Any, snap: dict[str, Any]) -> None:
    for k, v in (snap or {}).items():
        if k in SKIP_ATTRS:
            continue
        setattr(plugin, k, v)


async def save_snapshot(workflow_id: int, node_id: str, execution_id: str,
                        plugin: Any, items: list[Item]) -> None:
    """采集之后存一份现场：插件状态 + 本轮产出的条目原样。"""
    if not execution_id:
        return
    await NodeStateStore(workflow_id, node_id).set(
        SNAPSHOT_PREFIX + execution_id,
        {"plugin": snapshot_plugin(plugin), "items": [it.to_wire() for it in items]},
    )


async def settle(settings: Any, registry: Any, clients: Any,
                 workflow_id: int, execution_id: str) -> None:
    """一次触发跑完之后补做游标结算。没有快照就说明这轮没有顺序抓取的源，直接返回。"""
    if not execution_id:
        return
    async with session_scope() as s:
        sources = (await s.execute(
            select(GraphNode).where(GraphNode.workflow_id == workflow_id,
                                    GraphNode.kind == "source")
        )).scalars().all()
        s.expunge_all()
    if not sources:
        return

    # 这次触发里还有被挂起的消息，就不算真的结束：用户点「恢复」之后它们会被
    # 真的发出去，那时才知道归宿。现在结算并删掉快照的话，恢复发出去的那批
    # 内容永远等不到游标推进，下一轮会被重抓重发。
    async with session_scope() as s:
        parked = (await s.execute(
            select(GraphMessage.id).where(
                GraphMessage.workflow_id == workflow_id,
                GraphMessage.execution_id == execution_id,
                GraphMessage.status == "parked").limit(1)
        )).first()
    if parked:
        log.info("触发 %s 还有挂起的消息，游标结算先等着", execution_id)
        return

    fates: dict[str, str] | None = None
    for node in sources:
        store = NodeStateStore(workflow_id, node.node_id)
        snap = await store.get(SNAPSHOT_PREFIX + execution_id)
        if not snap:
            continue
        try:
            if fates is None:
                fates = await _fates(workflow_id, execution_id)
            await _settle_one(settings, registry, clients, workflow_id, node, snap, fates)
            # 成功才删快照。失败就留着，下次收口（或用户恢复之后）还有机会——
            # 删掉的话这批内容的游标永远不会推进，而 RunLog 已经写成 ok，
            # 用户只能从日志里一行 warning 发现。
            await store.delete(SNAPSHOT_PREFIX + execution_id)
        except Exception as e:
            log.warning("工作流 %s 节点 %s 的游标结算失败（快照已保留）：%s",
                        workflow_id, node.node_id, e)
            await _note_failure(workflow_id, str(e))


async def _fates(workflow_id: int, execution_id: str) -> dict[str, str]:
    """这次触发里每条内容（按 uid 归集）的归宿：sent / dropped / stuck。

    uid 在整条链路上不变（格式化插件只改正文，不动 uid），所以能拿它把
    "采集到的那条"和"最后发出去的那条"对上。

    判定不看节点类型：同一种节点会产生两类完全不同的丢弃——"被过滤规则拦下"
    是有结论的，"这个节点被停用了"不是。所以由产生丢弃的那一处在 detail 里
    自己声明 settles，这里只读它。

    优先级 sent > stuck > dropped：任何一条"没结论"都不能被后来的丢弃覆盖，
    否则游标会越过一条从没发出去、而且不会再重试的内容。
    """
    async with session_scope() as s:
        rows = (await s.execute(
            select(GraphMessage.payload, GraphNodeRun.status, GraphNodeRun.produced,
                   GraphNodeRun.detail, GraphNode.kind)
            .select_from(GraphNodeRun)
            .join(GraphMessage, GraphMessage.message_id == GraphNodeRun.message_id)
            .join(GraphNode, and_(GraphNode.workflow_id == GraphNodeRun.workflow_id,
                                  GraphNode.node_id == GraphNodeRun.node_id))
            .where(GraphNodeRun.workflow_id == workflow_id,
                   GraphNodeRun.execution_id == execution_id)
            .order_by(GraphNodeRun.id)
        )).all()

    fate: dict[str, str] = {}
    for payload, status, produced, detail, kind in rows:
        uid = (payload or {}).get("uid") or ""
        if not uid:
            continue
        cur = fate.get(uid, "")
        if cur == "sent":
            continue                                  # 发出去了就是最终态
        detail = detail if isinstance(detail, dict) else {}

        if kind == "output" and status == "ok":
            fate[uid] = "sent"
            continue
        # 强杀之后重跑时输出节点的幂等早退：内容其实已经投出去了
        if kind == "output" and status == "drop" and detail.get("already_sent"):
            fate[uid] = "sent"
            continue
        if status in ("error", "park"):
            fate[uid] = "stuck"
            continue
        if cur == "stuck":
            continue                                  # 已经没结论了，别被后面的丢弃盖掉
        if status == "drop":
            settles = detail.get("settles")
            if settles is None:                       # 老记录没有这个字段，回落到类型判断
                settles = kind in SETTLING_DROPS
            fate[uid] = "dropped" if settles else "stuck"
        elif status == "ok" and not produced:
            # 每一跳都正常，但走到这里没有下游了（分流的某个出口悬空、
            # 出边被取消勾选……）。内容确实走完了整张图，算有结论。
            fate[uid] = "dropped"
    return fate


async def _note_failure(workflow_id: int, err: str) -> None:
    """结算失败要在界面上看得见，光写日志用户不会发现。"""
    from sqlalchemy import update
    try:
        async with session_scope() as s:
            await s.execute(update(Workflow).where(Workflow.id == workflow_id).values(
                last_note=f"游标结算失败，下轮可能重复采集：{err}"[:200]))
    except Exception:
        pass


async def _settle_one(settings: Any, registry: Any, clients: Any, workflow_id: int,
                      node: GraphNode, snap: dict[str, Any], fates: dict[str, str]) -> None:
    async with session_scope() as s:
        wf = (await s.execute(
            select(Workflow).where(Workflow.id == workflow_id))).scalar_one_or_none()
        if wf is None:
            return
        account = await s.get(Account, wf.account_id) if wf.account_id else None
        s.expunge_all()
    if wf.dry_run:
        # 试运行一条都不发，游标绝不能动。存快照那一步已经拦过一次，
        # 这里是双保险：开关可能在存完快照之后才被打开。
        log.info("工作流 %s 处于试运行模式，跳过游标结算", workflow_id)
        return

    ctx = RunContext(wf, settings, clients, account, dry_run=False,
                     trigger="settle", registry=registry)
    ctx.state = NodeStateStore(workflow_id, node.node_id)
    ctx.log = logging.getLogger(f"wf.{workflow_id}.cursor.{node.node_id}")

    plugin = registry.cls(node.plugin)(node.config or {})
    await plugin.setup(ctx)          # 钩子在这里注册、游标在这里读出来
    restore_plugin(plugin, snap.get("plugin") or {})

    for wire in snap.get("items") or []:
        it = Item.from_wire(wire)
        fate = fates.get(it.uid, "stuck")
        if fate == "sent":
            ctx.sent_items.append(it)
        elif fate == "dropped":
            ctx.dropped_items.append(it)
        else:
            ctx.failed_items.append(it)

    await ctx.commit()
    ctx.log.info("游标结算：发出 %d 条、丢弃 %d 条、留待下轮 %d 条",
                 len(ctx.sent_items), len(ctx.dropped_items), len(ctx.failed_items))
    try:
        await plugin.teardown(ctx)
    finally:
        ctx._temp_files.clear()
        await ctx.close()
