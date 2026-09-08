"""调度器。

只负责持有 APScheduler 实例、回答"下次什么时候跑"、把时间写回库。
真正建任务的是图运行时（按信息源节点建，前缀 gwf:）——调度粒度在节点上，
因为一张图里的多个信息源各有各的节奏。

interval 表达式的解析（parse_interval_spec）留在这里，图运行时和保存图时的
校验都复用它。
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from ..db.models import Workflow
from ..db.session import session_scope
from .timing import utc_iso

log = logging.getLogger(__name__)

JOB_PREFIX = "wf:"      # 历史前缀，只用于识别旧库里可能残留的任务


def _job_workflow_id(jid: str) -> int | None:
    """从任务 id 解析工作流 id。线性是 wf:{id}，图是 gwf:{id}:{node_id}。"""
    try:
        if jid.startswith("gwf:"):
            return int(jid.split(":")[1])
        if jid.startswith(JOB_PREFIX):
            return int(jid[len(JOB_PREFIX):])
    except (ValueError, IndexError):
        return None
    return None


def parse_interval_spec(value: str) -> tuple[int, int]:
    """解析间隔配置，返回 (基准秒数, 抖动半径)。

    "600"      -> (600, 0)      每 10 分钟
    "10m"      -> (600, 0)
    "5m-15m"   -> (600, 300)    5～15 分钟之间随机
    """
    v = (value or "600").strip().lower()
    parts = [p for p in re.split(r"\s*[-–~至]\s*", v) if p]
    if len(parts) == 2:
        lo, hi = parse_interval(parts[0]), parse_interval(parts[1])
        if lo > hi:
            lo, hi = hi, lo
        return (lo + hi) // 2, (hi - lo) // 2
    return parse_interval(v), 0


def parse_interval(value: str) -> int:
    """把 "600" / "10m" / "2h" / "1d" 换算成秒。"""
    v = (value or "600").strip().lower()
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if v and v[-1] in units:
        try:
            return int(float(v[:-1]) * units[v[-1]])
        except ValueError as e:
            raise ValueError(f"无法解析间隔：{value}") from e
    try:
        return int(float(v))
    except ValueError as e:
        raise ValueError(f"无法解析间隔：{value}") from e


class Scheduler:
    def __init__(self) -> None:
        self.sched = AsyncIOScheduler(
            job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 300}
        )

    def start(self) -> None:
        if not self.sched.running:
            self.sched.start()
            log.info("调度器已启动")

    def shutdown(self) -> None:
        if self.sched.running:
            self.sched.shutdown(wait=False)
            log.info("调度器已停止")

    # ------------------------------------------------------------------ 查询
    def next_run(self, workflow_id: int) -> dt.datetime | None:
        """这条工作流下一次什么时候跑。线性任务和图的信息源任务一起看，取最早的。

        只查 wf:{id} 的话，图模式的工作流永远返回 None，前端会回落到库里那个
        再也没人更新的旧值，显示成一个过去的时刻。
        """
        best: dt.datetime | None = None
        for job in self.sched.get_jobs():
            if _job_workflow_id(job.id) != workflow_id:
                continue
            nxt = getattr(job, "next_run_time", None)
            if nxt and (best is None or nxt < best):
                best = nxt
        return best

    def jobs(self) -> list[dict[str, Any]]:
        """所有已排期的任务，线性的和图的都算。

        只认 wf: 前缀的话，切到图模式的工作流会在概览页显示成"没有已排期的任务"，
        而它其实每 10 分钟都在跑——把"正在跑"显示成"没在跑"是最糟的一种失真。
        """
        out = []
        for job in self.sched.get_jobs():
            wid = _job_workflow_id(job.id)
            if wid is None:
                continue
            out.append(
                {
                    "workflow_id": wid,
                    "name": job.name,
                    "next_run": utc_iso(job.next_run_time),
                    "trigger": str(job.trigger),
                    "graph": job.id.startswith("gwf:"),
                }
            )
        return sorted(out, key=lambda j: j["next_run"] or "9999")

    async def persist_next_run(self, workflow_id: int | None = None) -> None:
        """公开入口：图运行时重建完信息源任务之后也要调一次。"""
        await self._persist_next_run(workflow_id)

    async def _persist_next_run(self, workflow_id: int | None = None) -> None:
        """把下次运行时间写回数据库，方便前端直接展示。

        取所有已排期任务里最早的那个：图模式下一条工作流可能有多个信息源节点，
        各有各的节奏。没有任何任务时要显式写成 None，否则会一直显示一个过去的时刻。
        """
        try:
            by_wf: dict[int, Any] = {}
            for job in self.sched.get_jobs():
                wid = _job_workflow_id(job.id)
                nxt = getattr(job, "next_run_time", None)
                if wid is None or nxt is None:
                    continue
                if wid not in by_wf or nxt < by_wf[wid]:
                    by_wf[wid] = nxt
            async with session_scope() as s:
                ids = [workflow_id] if workflow_id else list(
                    (await s.execute(select(Workflow.id))).scalars().all())
                for wid in ids:
                    wf = await s.get(Workflow, wid)
                    if wf is None:
                        continue
                    nxt = by_wf.get(wid)
                    # APScheduler 给的是带时区的本地时间，库里其余时间戳一律是 UTC，
                    # 直接剥掉 tzinfo 会存成本地时间，前端按 UTC 解析就差一个时区
                    wf.next_run_at = (
                        nxt.astimezone(dt.timezone.utc).replace(tzinfo=None) if nxt else None
                    )
        except Exception as e:
            log.debug("写入 next_run 失败：%s", e)
