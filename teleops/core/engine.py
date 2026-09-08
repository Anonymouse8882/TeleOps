"""引擎：把配置、插件注册表、TG 客户端、执行器、调度器组装起来。"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from ..config import Settings, get_settings
from ..db.session import Database, init_db
from ..tg.client import ClientManager
from ..tg.codewatch import CodeWatcher
from .appconfig import TelegramApi, load_telegram_api, seed_from_accounts
from .graph import GraphRuntime
from .registry import PluginRegistry
from .scheduler import Scheduler

log = logging.getLogger(__name__)


class Engine:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.registry = PluginRegistry(self.settings.plugins.dirs)
        self.clients = ClientManager(self.settings)
        self.code_watcher = CodeWatcher(self.clients)
        self.scheduler = Scheduler()
        self.graph = GraphRuntime(self.settings, self.registry, self.clients, self.scheduler)
        self.db: Database | None = None
        self.tg_api: TelegramApi = TelegramApi()
        self.started_at: float = 0.0
        self._watch_task: asyncio.Task[None] | None = None

    # ---------------------------------------------------------------- 生命周期
    async def start(self) -> None:
        self.started_at = time.time()
        self.db = await init_db(self.settings)
        self.registry.scan(force=True)
        log.info("已加载插件：%s", self.registry.stats())
        await self._purge_orphans()
        await self.reload_api_credentials(seed=True)
        self.scheduler.start()
        await self.migrate_legacy_workflows()
        await self.graph.start()
        if self.settings.plugins.auto_reload:
            self._watch_task = asyncio.create_task(self._watch_plugins())
        await self._cleanup_media()
        self._cleanup_tmp()
        log.info("TeleOps 引擎已启动")

    async def stop(self) -> None:
        if self._watch_task:
            self._watch_task.cancel()
            try:
                await self._watch_task
            except (asyncio.CancelledError, Exception):
                pass
        await self.graph.stop()
        self.scheduler.shutdown()
        await self.code_watcher.stop_all()
        await self.clients.close_all()
        if self.db:
            await self.db.dispose()
        log.info("TeleOps 引擎已停止")

    async def reload_api_credentials(self, *, seed: bool = False) -> TelegramApi:
        """从数据库读全局 API 凭据并推给客户端池。

        seed=True 时，若全局值为空则从老账号里提升一份（首次升级用）。
        """
        self.tg_api = await (seed_from_accounts() if seed else load_telegram_api())
        self.clients.set_global_api(self.tg_api.api_id, self.tg_api.api_hash)
        if not self.tg_api.configured:
            log.warning("尚未配置 Telegram API，请在后台「设置」里填写 api_id / api_hash")
        return self.tg_api

    # -------------------------------------------------------------- 插件热重载
    async def _watch_plugins(self) -> None:
        interval = max(2, self.settings.plugins.reload_interval)
        while True:
            try:
                await asyncio.sleep(interval)
                res = await asyncio.to_thread(self.registry.scan)
                if res.changed:
                    for n in res.added:
                        log.info("插件已加载：%s", n)
                    for n in res.updated:
                        log.info("插件已热更新：%s", n)
                    for n in res.removed:
                        log.info("插件已卸载：%s", n)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("插件监听出错：%s", e)

    # ------------------------------------------------------------------ 便捷
    async def resync(self) -> None:
        """工作流的启用状态或图改了之后，重建定时任务。

        所有改动工作流的接口都只认这一个入口——漏掉的后果是「停用」按钮点了
        没用：界面显示已停用，后台照样每 10 分钟往频道发。

        故意不收 workflow_id：sync_sources 是"按库里的图重算全部任务"的全量
        语义，收一个用不上的 id 只会让调用方以为是增量重建。
        """
        await self.graph.sync_sources()

    async def migrate_legacy_workflows(self) -> int:
        """把老库补齐成"每条工作流都有一张图、状态都在节点上"。

        执行只有图这一条路。老库里的工作流是线性配置，启动时按原来的执行顺序
        转一次（采集 → 去重 → 限量 → 格式化 → 输出）；插件状态也要从工作流级
        搬到节点级——线性执行器把游标和取样池写在 workflow_state，而图运行时的
        插件读的是节点状态，不搬的话增量源会走"首轮初始化"、随机源会把已经
        搬运过的旧帖重新纳入取样范围，两种都是重复发帖。

        两件事各自独立判断：已经有图但状态没搬的工作流（比如迁移做了一半），
        下次启动照样会把状态补上。
        """
        from sqlalchemy import select

        from ..db.models import (
            GraphEdge, GraphNode, GraphNodeState, Workflow, WorkflowState,
        )
        from ..db.session import session_scope
        from .graph import linear_to_graph

        done = 0
        try:
            async with session_scope() as s:
                workflows = (await s.execute(select(Workflow))).scalars().all()
                for wf in workflows:
                    nodes = (await s.execute(select(GraphNode).where(
                        GraphNode.workflow_id == wf.id))).scalars().all()

                    # ① 没有图就按线性配置转一张
                    if not nodes and wf.source_plugin:
                        await s.refresh(wf, ["targets"])
                        targets = [{"channel_id": t.channel_id, "enabled": t.enabled,
                                    "overrides": t.overrides or {}}
                                   for t in sorted(wf.targets, key=lambda x: x.position)]
                        built, edges = linear_to_graph(wf, targets, self.settings)
                        for n in built:
                            s.add(GraphNode(workflow_id=wf.id, **n))
                        for i, e in enumerate(edges):
                            s.add(GraphEdge(workflow_id=wf.id, position=i, **e))
                        await s.flush()
                        nodes = (await s.execute(select(GraphNode).where(
                            GraphNode.workflow_id == wf.id))).scalars().all()
                        log.info("工作流「%s」已转成图：%d 个节点", wf.name, len(built))
                        done += 1

                    # ② 状态还在工作流级就搬到源节点上
                    src = next((n for n in nodes if n.kind == "source"), None)
                    if src is None:
                        continue
                    states = (await s.execute(select(WorkflowState).where(
                        WorkflowState.workflow_id == wf.id))).scalars().all()
                    if not states:
                        continue
                    # 只逐个 key 判断"这一项搬过没有"。早先是看"这条工作流有没有
                    # 任意一行节点状态"，可输出节点的 sent: 账本、去重节点的 seen:、
                    # 源节点的 cursor_snapshot 随便一条都会让它以为搬完了——用户
                    # 先手工建图跑过一次，游标和取样池就永远留在 workflow_state：
                    # 增量源走"首轮初始化"漏抓中间全部历史，随机源把已搬运过的
                    # 旧帖重新纳入取样池重发。
                    have = {k for (k,) in (await s.execute(
                        select(GraphNodeState.key).where(
                            GraphNodeState.workflow_id == wf.id,
                            GraphNodeState.node_id == src.node_id))).all()}
                    moved = 0
                    for row in states:
                        # 节点上已有同名 key 的，以节点上那份为准（它是运行中一直
                        # 在演进的那份，工作流级那份是搬家前的旧快照）
                        if row.key in have:
                            continue
                        s.add(GraphNodeState(workflow_id=wf.id, node_id=src.node_id,
                                             key=row.key, value=dict(row.value or {})))
                        moved += 1
                    if moved:
                        log.info("工作流「%s」的 %d 项状态已搬到节点 %s 上",
                                 wf.name, moved, src.node_id)
                        done += 1
        except Exception as e:
            log.exception("老工作流迁移失败：%s", e)
        return done

    async def _purge_orphans(self) -> None:
        """清掉指向已删除工作流的残留数据（早期版本遗留）。

        工作流 id 会被后建的工作流复用，留着孤儿记录会让新工作流显示别人的历史。
        """
        from sqlalchemy import delete, select

        from ..db.models import (
            GraphBranchPause, GraphEdge, GraphMergeBuffer, GraphMessage, GraphNode, GraphNodeRun,
            GraphNodeState,
            RunLog, SeenItem, Workflow, WorkflowState, WorkflowTarget,
        )
        from ..db.session import session_scope

        try:
            async with session_scope() as s:
                alive = set((await s.execute(select(Workflow.id))).scalars().all())
                total = 0
                for model in (RunLog, SeenItem, WorkflowState, WorkflowTarget,
                              GraphNode, GraphEdge, GraphMessage, GraphNodeState, GraphNodeRun,
                              GraphMergeBuffer, GraphBranchPause):
                    stmt = delete(model)
                    stmt = stmt.where(model.workflow_id.notin_(alive)) if alive else stmt
                    total += (await s.execute(stmt)).rowcount or 0
            if total:
                log.info("清理孤儿数据 %d 条", total)
        except Exception as e:
            log.warning("清理孤儿数据失败：%s", e)

    def _cleanup_tmp(self) -> None:
        """清掉 data/tmp 里的陈旧中间文件（导入失败、进程被杀等情况的残留）。"""
        tmp = self.settings.storage.data_dir / "tmp"
        if not tmp.exists():
            return
        cutoff = time.time() - 3600
        removed = 0
        for p in tmp.iterdir():
            try:
                if p.is_file() and p.stat().st_mtime < cutoff:
                    p.unlink()
                    removed += 1
            except OSError:
                pass
        if removed:
            log.info("清理临时文件 %d 个", removed)

    async def _cleanup_media(self) -> None:
        days = self.settings.storage.media_retention_days
        if days <= 0:
            return
        cutoff = time.time() - days * 86400
        removed = 0
        for p in self.settings.storage.media_dir.rglob("*"):
            if p.is_file():
                try:
                    if p.stat().st_mtime < cutoff:
                        p.unlink()
                        removed += 1
                except OSError:
                    pass
        if removed:
            log.info("清理过期媒体文件 %d 个", removed)

    def status(self) -> dict[str, Any]:
        return {
            "uptime": int(time.time() - self.started_at) if self.started_at else 0,
            "plugins": self.registry.stats(),
            "jobs": self.scheduler.jobs(),
            "plugin_dirs": [str(d) for d in self.settings.plugins.dirs],
        }


_engine: Engine | None = None


def get_engine() -> Engine:
    if _engine is None:
        raise RuntimeError("引擎尚未启动")
    return _engine


def set_engine(engine: Engine) -> None:
    global _engine
    _engine = engine


def ensure_plugin_dirs(settings: Settings) -> None:
    for d in settings.plugins.dirs:
        for sub in ("sources", "filters", "formatters", "sinks"):
            Path(d, sub).mkdir(parents=True, exist_ok=True)
