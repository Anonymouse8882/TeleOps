"""概览：系统状态、汇总统计、近期运行、实时日志。"""
from __future__ import annotations

import datetime as dt
from typing import Any

from fastapi import APIRouter
from sqlalchemy import func, select

from ...core.timing import utc_iso
from ...db.models import Account, Channel, RunLog, SeenItem, Workflow
from ...logging_setup import recent_logs
from ..deps import DbDep, EngineDep, SettingsDep, ok

router = APIRouter(prefix="/api", tags=["overview"])


@router.get("/overview")
async def overview(db: DbDep, engine: EngineDep):
    accounts = (await db.execute(select(Account))).scalars().all()
    channels = (await db.execute(select(Channel))).scalars().all()
    workflows = (await db.execute(select(Workflow))).scalars().all()

    since = dt.datetime.utcnow() - dt.timedelta(hours=24)
    day_rows = (
        await db.execute(
            select(
                func.count(RunLog.id),
                func.coalesce(func.sum(RunLog.sent), 0),
                func.coalesce(func.sum(RunLog.failed), 0),
            ).where(RunLog.started_at >= since)
        )
    ).one()
    total_sent = (await db.execute(select(func.coalesce(func.sum(Workflow.total_sent), 0)))).scalar_one()
    seen_total = (await db.execute(select(func.count(SeenItem.id)))).scalar_one()

    recent = (
        await db.execute(select(RunLog).order_by(RunLog.id.desc()).limit(15))
    ).scalars().all()
    wf_names = {w.id: w.name for w in workflows}

    return ok(
        {
            "counters": {
                "accounts": len(accounts),
                "accounts_online": sum(1 for a in accounts if a.status == "online"),
                "channels": len(channels),
                "channels_enabled": sum(1 for c in channels if c.enabled),
                "workflows": len(workflows),
                "workflows_enabled": sum(1 for w in workflows if w.enabled),
                # 图模式也写 RunLog（一次触发汇总一条），所以这里只按 RunLog 算，
                # 不要再叠加逐跳记录——那会把同一轮数两遍
                "runs_24h": day_rows[0],
                "sent_24h": int(day_rows[1] or 0),
                "failed_24h": int(day_rows[2] or 0),
                "sent_total": int(total_sent or 0),
                "dedup_records": int(seen_total or 0),
            },
            "plugins": engine.registry.stats(),
            "jobs": engine.scheduler.jobs()[:10],
            "uptime": engine.status()["uptime"],
            "recent_runs": [
                {
                    "id": r.id,
                    "workflow_id": r.workflow_id,
                    "workflow": wf_names.get(r.workflow_id, "(已删除)"),
                    "status": r.status,
                    "trigger": r.trigger,
                    "sent": r.sent,
                    "failed": r.failed,
                    "fetched": r.fetched,
                    "error": r.error,
                    "started_at": r.started_at.isoformat() if r.started_at else None,
                }
                for r in recent
            ],
            "workflow_status": [
                {
                    "id": w.id,
                    "name": w.name,
                    "enabled": w.enabled,
                    "last_status": w.last_status,
                    "last_run_at": w.last_run_at.isoformat() if w.last_run_at else None,
                    "next_run_at": _next(engine, w),
                    "total_sent": w.total_sent,
                }
                for w in sorted(workflows, key=lambda x: (not x.enabled, x.id))
            ],
        }
    )


@router.get("/logs")
async def logs(limit: int = 200, level: str | None = None):
    return ok(recent_logs(limit=limit, level=level))


@router.get("/system")
async def system(engine: EngineDep, settings: SettingsDep):
    st = engine.status()
    st["settings"] = {
        "send_interval": settings.runtime.send_interval,
        "max_items_per_run": settings.runtime.max_items_per_run,
        "media_dir": str(settings.storage.media_dir),
        "auto_reload": settings.plugins.auto_reload,
        "auth_enabled": bool(settings.server.auth_token),
    }
    return ok(st)


def _next(engine, w: Workflow) -> Any:
    # 统一成不带时区的 UTC：调度器给的是带本地偏移的时间，前端认不出负偏移
    return utc_iso(engine.scheduler.next_run(w.id)) or utc_iso(w.next_run_at)
