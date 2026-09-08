"""插件管理：列表、启停、热重载、在线查看/编辑源码、上传、快速测试。"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, HTTPException, UploadFile
from sqlalchemy import select

from ...core.item import Item
from ...core.context import RunContext
from ...core.plugin import SourcePlugin
from ...db.models import Account, Workflow
from ..deps import DbDep, EngineDep, ok
from ..schemas import PluginSource, PluginTestIn, PluginToggle

router = APIRouter(prefix="/api/plugins", tags=["plugins"])

TYPE_DIR = {"source": "sources", "filter": "filters", "formatter": "formatters", "sink": "sinks"}
TYPE_LABEL = {"source": "信息源", "filter": "过滤规则", "formatter": "格式化", "sink": "输出端"}


def _dump(lp, engine) -> dict[str, Any]:
    m = lp.meta
    path = Path(m.file)
    try:
        mtime = dt.datetime.fromtimestamp(lp.mtime).isoformat(timespec="seconds")
    except (OSError, ValueError):
        mtime = None
    return {
        "name": m.name,
        "display_name": m.display_name,
        "type": m.plugin_type,
        "type_label": TYPE_LABEL.get(m.plugin_type, m.plugin_type),
        "version": m.version,
        "author": m.author,
        "description": m.description,
        "config_schema": m.config_schema,
        "requires_account": lp.cls.requires_account,
        "file": m.file,
        "filename": path.name,
        "enabled": lp.enabled,
        "updated_at": mtime,
    }


@router.get("")
async def list_plugins(engine: EngineDep, db: DbDep, type: str | None = None):
    plugins = [_dump(lp, engine) for lp in engine.registry.list(type)]
    # 统计每个插件被多少工作流使用
    usage: dict[str, int] = {}
    for wf in (await db.execute(select(Workflow))).scalars().all():
        names = [wf.source_plugin, wf.sink_plugin]
        names += [s.get("plugin") for s in (wf.filters or [])]
        names += [s.get("plugin") for s in (wf.formatters or [])]
        for n in names:
            if n:
                usage[n] = usage.get(n, 0) + 1
    for p in plugins:
        p["used_by"] = usage.get(p["name"], 0)
    return ok(
        plugins,
        stats=engine.registry.stats(),
        errors=[{"file": e.file, "message": e.message} for e in engine.registry.errors],
        dirs=[str(d) for d in engine.settings.plugins.dirs],
    )


@router.post("/reload")
async def reload_all(engine: EngineDep):
    res = engine.registry.reload()
    return ok(
        {
            "added": res.added,
            "updated": res.updated,
            "removed": res.removed,
            "errors": [{"file": e.file, "message": e.message} for e in res.errors],
        }
    )


@router.get("/{name}")
async def get_plugin(name: str, engine: EngineDep):
    lp = engine.registry.get(name)
    if lp is None:
        raise HTTPException(404, "插件不存在")
    return ok(_dump(lp, engine))


@router.post("/{name}/toggle")
async def toggle_plugin(name: str, payload: PluginToggle, engine: EngineDep):
    if not engine.registry.set_enabled(name, payload.enabled):
        raise HTTPException(404, "插件不存在")
    return ok({"enabled": payload.enabled})


@router.post("/{name}/reload")
async def reload_one(name: str, engine: EngineDep):
    res = engine.registry.reload(name)
    return ok({"updated": res.added + res.updated,
               "errors": [{"file": e.file, "message": e.message} for e in res.errors]})


@router.get("/{name}/source")
async def read_source(name: str, engine: EngineDep):
    lp = engine.registry.get(name)
    if lp is None:
        raise HTTPException(404, "插件不存在")
    path = _resolve(engine, lp.meta.file)
    return ok({"file": lp.meta.file, "code": path.read_text(encoding="utf-8")})


@router.put("/{name}/source")
async def write_source(name: str, payload: PluginSource, engine: EngineDep):
    lp = engine.registry.get(name)
    if lp is None:
        raise HTTPException(404, "插件不存在")
    path = _resolve(engine, lp.meta.file)
    backup = path.with_suffix(path.suffix + ".bak")
    backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    path.write_text(payload.code, encoding="utf-8")

    res = engine.registry.scan()
    if res.errors:
        path.write_text(backup.read_text(encoding="utf-8"), encoding="utf-8")  # 语法错误则回滚
        engine.registry.scan()
        raise HTTPException(400, "保存失败，已回滚：" + res.errors[0].message)
    return ok({"reloaded": res.added + res.updated})


@router.post("/upload")
async def upload_plugin(engine: EngineDep, type: str = "source", file: UploadFile = File(...)):
    if not file.filename or not file.filename.endswith(".py"):
        raise HTTPException(400, "只接受 .py 文件")
    sub = TYPE_DIR.get(type, "sources")
    target_dir = engine.settings.plugins.dirs[0] / sub
    target_dir.mkdir(parents=True, exist_ok=True)
    dest = target_dir / Path(file.filename).name
    if dest.exists():
        raise HTTPException(400, f"{dest.name} 已存在，请先删除或改名")

    content = (await file.read()).decode("utf-8", "replace")
    dest.write_text(content, encoding="utf-8")
    res = engine.registry.scan()
    if res.errors and any(Path(e.file).name == dest.name for e in res.errors):
        err = next(e for e in res.errors if Path(e.file).name == dest.name)
        dest.unlink(missing_ok=True)
        raise HTTPException(400, "插件有误，已删除：" + err.message)
    return ok({"added": res.added, "file": str(dest)})


@router.delete("/{name}")
async def delete_plugin(name: str, engine: EngineDep, db: DbDep):
    lp = engine.registry.get(name)
    if lp is None:
        raise HTTPException(404, "插件不存在")
    for wf in (await db.execute(select(Workflow))).scalars().all():
        used = [wf.source_plugin, wf.sink_plugin] + [
            s.get("plugin") for s in (wf.filters or []) + (wf.formatters or [])
        ]
        if name in used:
            raise HTTPException(400, f"工作流「{wf.name}」正在使用该插件")
    path = _resolve(engine, lp.meta.file)
    path.unlink(missing_ok=True)
    engine.registry.scan()
    return ok()


@router.post("/test")
async def test_plugin(payload: PluginTestIn, engine: EngineDep, db: DbDep):
    """不落库地跑一次插件，用于配置页的「测试」按钮。"""
    lp = engine.registry.get(payload.plugin)
    if lp is None:
        raise HTTPException(404, "插件不存在")

    account = None
    if payload.account_id:
        account = await db.get(Account, payload.account_id)
    elif lp.cls.requires_account:
        account = (
            await db.execute(select(Account).where(Account.enabled.is_(True)).limit(1))
        ).scalar_one_or_none()

    # 造一个内存里的临时工作流，id=0 表示不落库
    stub = Workflow(id=0, name="__test__", source_plugin=payload.plugin, source_config=payload.config)
    ctx = RunContext(stub, engine.settings, engine.clients, account, dry_run=True, trigger="test",
                     registry=engine.registry)
    try:
        plugin = lp.cls(payload.config)
        errs = plugin.validate_config(plugin.config)
        if errs:
            raise HTTPException(400, "；".join(errs))
        await plugin.setup(ctx)
        if isinstance(plugin, SourcePlugin):
            items: list[Item] = await plugin.fetch(ctx)
            data = {
                "count": len(items),
                "items": [i.to_dict() for i in items[:5]],
            }
        else:
            data = {"message": "该类型插件请在工作流里用「试运行」测试"}
        await plugin.teardown(ctx)
        return ok(data)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f"{type(e).__name__}: {e}")
    finally:
        await ctx.close()


def _resolve(engine, file: str) -> Path:
    p = Path(file)
    if not p.is_absolute():
        p = engine.settings.root / p
    if not p.exists():
        raise HTTPException(404, f"插件文件不存在：{file}")
    # 只允许操作插件目录内的文件
    roots = [d.resolve() for d in engine.settings.plugins.dirs]
    if not any(str(p.resolve()).startswith(str(r)) for r in roots):
        raise HTTPException(400, "只能操作插件目录内的文件")
    return p
