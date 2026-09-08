"""频道管理。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select

from ...db.models import Account, Channel, Workflow, WorkflowTarget
from ..deps import DbDep, EngineDep, ok
from ..schemas import ChannelIn, ChannelPatch

router = APIRouter(prefix="/api/channels", tags=["channels"])


class BulkImport(BaseModel):
    account_id: int
    peers: list[dict[str, Any]]


def _dump(c: Channel, workflow_count: int = 0) -> dict[str, Any]:
    return {
        "id": c.id,
        "title": c.title,
        "peer": c.peer,
        "account_id": c.account_id,
        "kind": c.kind,
        "note": c.note,
        "enabled": c.enabled,
        "resolved": c.resolved or {},
        "workflow_count": workflow_count,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


@router.get("")
async def list_channels(db: DbDep):
    counts = dict(
        (
            await db.execute(
                select(WorkflowTarget.channel_id, func.count(WorkflowTarget.id)).group_by(
                    WorkflowTarget.channel_id
                )
            )
        ).all()
    )
    rows = (await db.execute(select(Channel).order_by(Channel.id))).scalars().all()
    return ok([_dump(c, counts.get(c.id, 0)) for c in rows])


@router.get("/{channel_id}")
async def get_channel(channel_id: int, db: DbDep):
    ch = await _need(db, channel_id)
    rows = (
        await db.execute(
            select(Workflow, WorkflowTarget)
            .join(WorkflowTarget, WorkflowTarget.workflow_id == Workflow.id)
            .where(WorkflowTarget.channel_id == channel_id)
        )
    ).all()
    data = _dump(ch, len(rows))
    data["workflows"] = [
        {
            "id": wf.id,
            "name": wf.name,
            "enabled": wf.enabled,
            "source_plugin": wf.source_plugin,
            "target_enabled": wt.enabled,
            "last_status": wf.last_status,
            "last_run_at": wf.last_run_at.isoformat() if wf.last_run_at else None,
        }
        for wf, wt in rows
    ]
    return ok(data)


@router.post("")
async def create_channel(payload: ChannelIn, db: DbDep):
    ch = Channel(**payload.model_dump())
    db.add(ch)
    await db.flush()
    return ok(_dump(ch))


@router.post("/bulk")
async def bulk_import(payload: BulkImport, db: DbDep):
    """从「我的对话」批量导入频道。"""
    existing = {
        p for (p,) in (await db.execute(select(Channel.peer))).all()
    }
    created = 0
    for p in payload.peers:
        peer = str(p.get("peer") or "").strip()
        if not peer or peer in existing:
            continue
        db.add(
            Channel(
                title=p.get("title") or peer,
                peer=peer,
                account_id=payload.account_id,
                kind=p.get("kind") or "channel",
                resolved={k: p.get(k) for k in ("id", "username", "participants") if p.get(k)},
            )
        )
        existing.add(peer)
        created += 1
    await db.flush()
    return ok({"created": created})


@router.patch("/{channel_id}")
async def update_channel(channel_id: int, payload: ChannelPatch, db: DbDep):
    ch = await _need(db, channel_id)
    for k, v in payload.model_dump(exclude_none=True).items():
        setattr(ch, k, v)
    await db.flush()
    return ok(_dump(ch))


@router.delete("/{channel_id}")
async def delete_channel(channel_id: int, db: DbDep):
    ch = await _need(db, channel_id)
    await db.delete(ch)
    return ok()


@router.post("/{channel_id}/resolve")
async def resolve_channel(channel_id: int, db: DbDep, engine: EngineDep):
    """向 Telegram 校验该频道是否可达，并缓存标题/成员数。"""
    ch = await _need(db, channel_id)
    account_id = ch.account_id
    if account_id is None:
        raise HTTPException(400, "该频道未绑定账号")
    acc = await db.get(Account, account_id)
    if acc is None:
        raise HTTPException(400, "绑定的账号不存在")
    try:
        info = await engine.clients.describe(acc, ch.peer)
    except Exception as e:
        raise HTTPException(400, str(e))
    ch.resolved = info
    if info.get("title") and ch.title in ("", ch.peer):
        ch.title = info["title"]
    await db.flush()
    return ok(info)


async def _need(db, channel_id: int) -> Channel:
    ch = await db.get(Channel, channel_id)
    if ch is None:
        raise HTTPException(404, "频道不存在")
    return ch
