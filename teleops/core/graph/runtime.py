"""图运行时：单消费者 + 精确唤醒 + 信息源调度。

为什么是单消费者：规格要的是**事件语义**（一条消息触发一次执行、循环靠重新
入队而不是函数重入），不是并发。输出侧本来就必须按 Telegram 账号串行，采集侧
也被 ClientManager 的账号锁串行着，多开 worker 只会换来 SQLite 写锁竞争和
"多分支各自 sleep 导致发送速率翻倍"的封号风险。真需要并发时把 WORKERS 改大即可。

空闲怎么做到真 idle：没有待办就无限期 await 一个 Event；有延迟消息就
wait_for 到最近的到期时刻。全程不轮询——存在环也不会让 CPU 转起来，
因为环里的每一跳都是一条要被消费的消息，没有消息就没有唤醒。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from typing import Any

from sqlalchemy import delete, select

from ...db.models import (
    GraphMergeBuffer, GraphMessage, GraphNode, GraphNodeRun, GraphNodeState, Workflow, utcnow,
)
from ...db.session import session_scope
from .bus import MessageBus, new_id
from .cursor import SNAPSHOT_PREFIX, settle as settle_cursors
from .executor import NodeExecutor
from .runaway import RunawayDetector
from .state import NodeStateStore

log = logging.getLogger(__name__)

JOB_PREFIX = "gwf:"
# 全局跳数上限。这一道和内容无关，纯粹保证"CPU 打满 / 磁盘写爆"在结构上不可能
# 发生：超速了就把这一跳往后推，而不是拒绝它——合法的长期循环照跑，只是被限速。
MAX_HOPS_PER_SEC = 20
# 单条消息的重试上限。超过就挂起，等人看诊断信息，而不是无限重试烧 CPU。
MAX_ATTEMPTS = 5
# 每多少跳顺手清理一次历史。不用定时器，免得破坏"空闲时不轮询"。
PRUNE_EVERY = 200


def job_id(workflow_id: int, node_id: str) -> str:
    return f"{JOB_PREFIX}{workflow_id}:{node_id}"


class GraphRuntime:
    def __init__(self, settings: Any, registry: Any, clients: Any, scheduler: Any) -> None:
        self.settings = settings
        self.scheduler = scheduler
        self.bus = MessageBus()
        self.detector = RunawayDetector()
        self.executor = NodeExecutor(settings, registry, clients, self.bus)
        self.executor.detector = self.detector
        self._task: asyncio.Task[None] | None = None
        self._hops: list[float] = []
        self._last_warn = 0.0
        self._since_prune = 0

    # ---------------------------------------------------------------- 生命周期
    async def start(self) -> None:
        await self.bus.recover_orphans()
        self._task = asyncio.create_task(self._consume(), name="graph-consumer")
        await self.sync_sources()
        log.info("图运行时已启动")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        log.info("图运行时已停止")

    # ------------------------------------------------------------------ 消费
    async def _consume(self) -> None:
        while True:
            msg = None
            try:
                msgs = await self.bus.claim(1)
                if not msgs:
                    await self._idle()
                    continue
                msg = msgs[0]
                # 忙循环防护走在执行之前：判定为失控就根本不执行这一跳
                verdict = self.detector.observe(msg)
                if verdict.action == "slow":
                    await self.bus.retry_later(
                        msg["id"], delay=verdict.delay,
                        error=f"疑似高速循环，先减速观察（第 {verdict.detail.get('brakes')} 次）")
                    continue
                if verdict.action == "trip":
                    await self.executor.trip_branch(msg, verdict.detail)
                    continue
                await self._throttle()
                wait = await self.executor.run(msg)
                await self._prune()
                if wait:
                    # 输出节点的发送间隔：在这里等，而不是在执行器里边发边等。
                    # 那样这条消息会一直挂在 running 上，中断即重发。
                    await asyncio.sleep(min(wait, 30))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # 消费者绝不能死。单条消息自身的失败在 executor 里已经兜住了，
                # 走到这里说明是执行框架出了问题（读不到库、序列化炸了之类）。
                # 关键是别把这条消息扔在 running 上不管——那样它到重启前都不会再被处理。
                log.exception("图消费者出错：%s", e)
                if msg is not None:
                    await self._retry_or_park(msg, f"{type(e).__name__}: {e}")
                await asyncio.sleep(1)

    async def _retry_or_park(self, msg: dict[str, Any], error: str) -> None:
        """退避重试，次数用尽就挂起——不重试会丢消息，无限重试会烧 CPU。"""
        attempts = int(msg.get("attempts") or 1)
        if attempts >= MAX_ATTEMPTS:
            reason = f"重试 {attempts} 次仍失败：{error}"
            async with session_scope() as s:
                await self.bus.finish(s, msg["id"], "parked", reason)
                # 也写一条执行记录：不写的话这条内容既不在执行记录里、也不在待办里，
                # 用户看到的就是"莫名其妙没发出去"，日志里才有一行 error
                s.add(GraphNodeRun(
                    workflow_id=msg["workflow_id"], node_id=msg["node_id"],
                    message_id=msg["message_id"], origin_id=msg.get("origin_id", ""),
                    execution_id=msg.get("execution_id", ""), finished_at=utcnow(),
                    status="park", error=reason[:2000]))
            log.error("消息 %s 重试 %d 次仍失败，已挂起", msg["message_id"], attempts)
            return
        await self.bus.retry_later(msg["id"], delay=min(2 ** attempts, 60), error=error)

    async def _idle(self) -> None:
        """没有到期消息时睡到最近的到期时刻，或者无限期睡到有人 notify。"""
        self.bus.wake.clear()
        deadline = await self.bus.next_deadline()
        if deadline is None:
            await self.bus.wake.wait()
            return
        gap = (deadline - utcnow()).total_seconds()
        if gap <= 0:
            return                      # 刚好到期，直接回去领
        try:
            await asyncio.wait_for(self.bus.wake.wait(), timeout=min(gap, 900))
        except asyncio.TimeoutError:
            pass

    async def _prune(self) -> None:
        """清掉过期的消息与执行记录。

        每一跳都会把完整 Item 复制一份存进 graph_messages，跑起来大约每天上千行，
        而线性那边的 run_logs 是有 run_log_keep 上限的。用"每 N 跳跑一次"来触发，
        不引入新的定时器（那会打破空闲不轮询这条）。
        """
        self._since_prune += 1
        if self._since_prune < PRUNE_EVERY:
            return
        self._since_prune = 0
        days = int(getattr(self.settings.storage, "graph_retain_days", 0) or 7)
        cutoff = utcnow() - dt.timedelta(days=days)
        try:
            async with session_scope() as s:
                # parked 也要清：停用、切模式、熔断、重试耗尽每次都会留下一批，
                # 只清 done/failed 的话它们永远堆着
                m = await s.execute(delete(GraphMessage).where(
                    GraphMessage.status.in_(("done", "failed", "parked")),
                    GraphMessage.updated_at < cutoff))
                r = await s.execute(delete(GraphNodeRun).where(GraphNodeRun.finished_at < cutoff))
                # 卡住的合流桶里存的是完整正文，不清就永久留在库里
                b = await s.execute(delete(GraphMergeBuffer).where(
                    GraphMergeBuffer.created_at < cutoff))
                # 触发没跑完就被停用/删节点时，游标快照会留下来（里面是整批正文）
                await s.execute(delete(GraphNodeState).where(
                    GraphNodeState.key.like(SNAPSHOT_PREFIX + "%"),
                    GraphNodeState.updated_at < cutoff))
            if (m.rowcount or 0) or (r.rowcount or 0) or (b.rowcount or 0):
                log.info("清理图历史：消息 %d 条、执行记录 %d 条、合流缓冲 %d 条",
                         m.rowcount or 0, r.rowcount or 0, b.rowcount or 0)
        except Exception as e:
            log.warning("清理图历史失败：%s", e)

    async def _throttle(self) -> None:
        now = time.monotonic()
        self._hops = [t for t in self._hops if now - t < 1.0]
        if len(self._hops) >= MAX_HOPS_PER_SEC:
            wait = 1.0 - (now - self._hops[0])
            # 正常的一轮扇出也会短暂触顶，别在这里喊"失控的环"——真的环检测是 P5
            # 的内容签名，那条日志的可信度要留给它
            if now - self._last_warn > 5:
                self._last_warn = now
                log.info("图执行速率已达上限 %d 跳/秒，正在限速", MAX_HOPS_PER_SEC)
            await asyncio.sleep(max(0.05, wait))
            now = time.monotonic()
            self._hops = [t for t in self._hops if now - t < 1.0]
        self._hops.append(now)

    # ------------------------------------------------------------------ 触发
    async def trigger(self, workflow_id: int, node_id: str, *, trigger: str = "manual") -> str:
        """给某个信息源节点投一条触发消息，返回本次触发的 execution_id。"""
        execution_id = new_id()
        if trigger == "schedule" and await self._busy(workflow_id, node_id):
            # 定时触发要合并：上一条还没被消费、或者上一批还没结算完，就别再投。
            # 不合并的话，发送端一旦卡住（tg_send 撞 FloodWait 最多等 600 秒），
            # 触发消息会每 10 分钟稳定堆积一条，堵塞解除后背靠背连发——
            # 对一个防封号的工具，这是最不该有的失效方向。线性执行器那边
            # 是靠"上一轮没结束就跳过本轮"达到同样效果的。
            log.info("工作流 %s 的信息源 %s 上一次触发还没处理完，本次跳过", workflow_id, node_id)
            return execution_id
        async with session_scope() as s:
            s.add(GraphMessage(**MessageBus.build(
                workflow_id=workflow_id, node_id=node_id, payload={},
                execution_id=execution_id,
                # 手动触发是用户当场的一个明确动作，停用状态下也该让它跑完这一批
                # ——"触发一次"就该是跑一次，不该反过来要求先把常驻定时打开。
                meta={"trigger": trigger, "manual": trigger == "manual"})))
        self.bus.notify()
        return execution_id

    async def _busy(self, workflow_id: int, node_id: str) -> bool:
        """这个信息源现在该不该跳过定时触发。

        两种情况都要跳过：
        1) 上一条触发消息还没被消费；
        2) 上一批还没结算完（游标快照还在）。第二条是关键——源那一跳跑完触发消息
           就变 done 了，但游标要等整次触发收口才推进。下游只要有滞后
           （Delay 节点、合流窗口、熔断冷却、FloodWait），下一次定时就会用同一个
           游标再抓一遍同一批内容并重新发出去，而放在源后面的去重节点兜不住
           （滞后期间一条都还没发出去）。线性执行器靠 per-workflow 锁把
           fetch→commit 整个包住，这一条就是等价的守卫。
        """
        async with session_scope() as s:
            row = (await s.execute(
                select(GraphMessage.id).where(
                    GraphMessage.workflow_id == workflow_id,
                    GraphMessage.node_id == node_id,
                    GraphMessage.src_node_id == "",          # 只看触发消息，不看流经的数据
                    GraphMessage.status.in_(("pending", "running"))).limit(1)
            )).first()
            if row is not None:
                return True
            keys = [k for (k,) in (await s.execute(
                select(GraphNodeState.key).where(
                    GraphNodeState.workflow_id == workflow_id,
                    GraphNodeState.node_id == node_id,
                    GraphNodeState.key.like(SNAPSHOT_PREFIX + "%"))
            )).all()]
        if not keys:
            return False

        # 快照还在，不等于那一批还在跑。settle() 在本次触发尚有挂起消息时会
        # 保留快照直接返回，而挂起的消息可能再也不会跑完（用户清掉、删节点
        # 带走），于是 _finalize_execution 永远不会为它补一次结算——快照就此
        # 永久残留，这个信息源的定时触发静默停摆，只能等 7 天 _prune 到期。
        # 所以先看那次触发是否真的还有在途消息；没有就当场把结算补上。
        store = NodeStateStore(workflow_id, node_id)
        for key in keys:
            execution_id = key[len(SNAPSHOT_PREFIX):]
            async with session_scope() as s:
                live = (await s.execute(
                    select(GraphMessage.id).where(
                        GraphMessage.workflow_id == workflow_id,
                        GraphMessage.execution_id == execution_id,
                        GraphMessage.status.in_(("pending", "running", "parked"))).limit(1)
                )).first()
            if live is not None:
                return True                     # 这一批真的还没跑完
            log.info("触发 %s 早已结束却没结算，补做一次游标结算", execution_id)
            await settle_cursors(self.settings, self.executor.registry,
                                 self.executor.clients, workflow_id, execution_id)
            if await store.get(key) is not None:
                # 结算失败会保留快照（游标没推进），这时仍要按"忙"处理，
                # 否则下一次定时会用同一个游标把同一批内容重抓重发。
                return True
        return False

    async def trigger_workflow(self, workflow_id: int, *, trigger: str = "manual") -> dict[str, Any]:
        """触发一条工作流里所有启用的信息源节点。"""
        async with session_scope() as s:
            nodes = (await s.execute(
                select(GraphNode).where(GraphNode.workflow_id == workflow_id,
                                        GraphNode.kind == "source",
                                        GraphNode.enabled.is_(True))
            )).scalars().all()
            ids = [n.node_id for n in nodes]
        execution_id = new_id()
        if not ids:
            return {"execution_id": execution_id, "sources": 0}
        async with session_scope() as s:
            for nid in ids:
                s.add(GraphMessage(**MessageBus.build(
                    workflow_id=workflow_id, node_id=nid, payload={},
                    execution_id=execution_id,
                    meta={"trigger": trigger, "manual": trigger == "manual"})))
        self.bus.notify()
        return {"execution_id": execution_id, "sources": len(ids)}

    # ------------------------------------------------------------------ 调度
    async def sync_sources(self) -> dict[str, Any]:
        """按库里的图重建信息源节点的定时任务。

        调度逻辑和采集逻辑是解耦的：这里只负责"到点了投一条触发消息"，
        真正的 fetch 由消费者执行。
        """
        sched = self.scheduler.sched
        async with session_scope() as s:
            rows = (await s.execute(
                select(Workflow, GraphNode)
                .join(GraphNode, GraphNode.workflow_id == Workflow.id)
                .where(GraphNode.kind == "source")
            )).all()
            candidates: dict[str, tuple[int, str, GraphNode, Workflow]] = {}
            for wf, node in rows:
                if not (wf.enabled and node.enabled):
                    continue
                candidates[job_id(wf.id, node.node_id)] = (wf.id, node.node_id, node, wf)
            s.expunge_all()

        errors = []
        # 只有真正建出任务的才算"该存在"。候选和该存在不是一回事：改成手动触发、
        # 或者间隔表达式建不出触发器，都会让这个节点不再有任务——这时旧任务必须
        # 被删掉。按候选集去删的话，旧任务会原地留着按老间隔一直跑，界面上的
        # "下次"也一直是老节奏，用户看到的就是"我改了它不生效"，只能重启才好。
        wanted: set[str] = set()
        for jid, (wid, nid, node, wf) in candidates.items():
            try:
                trigger = self._build_trigger(node)
            except Exception as e:
                errors.append(f"{wf.name}/{node.title or nid}: {e}")
                continue
            if trigger is None:
                continue
            wanted.add(jid)
            sched.add_job(self._fire, trigger=trigger, id=jid, args=[wid, nid],
                          name=f"{wf.name} · {node.title or nid}", replace_existing=True)

        for job in sched.get_jobs():
            if job.id.startswith(JOB_PREFIX) and job.id not in wanted:
                sched.remove_job(job.id)
        # 把下次运行时间写回库：前端的"下次运行"读的是这一列
        await self.scheduler.persist_next_run()
        return {"jobs": len([1 for j in sched.get_jobs() if j.id.startswith(JOB_PREFIX)]),
                "errors": errors}

    @staticmethod
    def _build_trigger(node: GraphNode):
        from apscheduler.triggers.cron import CronTrigger
        from apscheduler.triggers.interval import IntervalTrigger

        from ..scheduler import parse_interval_spec

        stype = (node.schedule_type or "manual").lower()
        if stype in ("manual", "immediate"):
            return None                  # immediate 只在"启用"那一刻触发一次，不建周期任务
        if stype == "cron":
            return CronTrigger.from_crontab(node.schedule_value)
        base, span = parse_interval_spec(node.schedule_value)
        jitter = (span + int(node.jitter or 0)) or None
        return IntervalTrigger(seconds=base, jitter=jitter)

    async def _fire(self, workflow_id: int, node_id: str) -> None:
        await self.trigger(workflow_id, node_id, trigger="schedule")

    # ------------------------------------------------------------------ 查询
    async def status(self) -> dict[str, Any]:
        return {
            "running": bool(self._task and not self._task.done()),
            "pending": await self.bus.pending_count(),
            "jobs": [j.id for j in self.scheduler.sched.get_jobs() if j.id.startswith(JOB_PREFIX)],
        }
