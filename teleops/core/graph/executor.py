"""节点执行器：一条消息 = 一次「某个节点收到输入」。

严格三段式：
    读（一个短事务，把要用的东西读出来并 expunge）
  → 调插件（**不持任何事务、不持锁**，可能几十秒）
  → 写（一个短事务：结掉这条消息 + 把产出入队 + 记一条执行记录）

中间那段绝不能碰事务：插件里有网络请求，占着 SQLite 写锁几十秒会让整个库卡住。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import traceback
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import and_, delete, func, select, update
from sqlalchemy.orm import selectinload

from ...db.models import (
    Account, Channel, GraphBranchPause, GraphEdge, GraphMergeBuffer, GraphMessage, GraphNode,
    GraphNodeRun, RunLog, Workflow, utcnow,
)
from ...db.session import session_scope
from ..context import RunContext
from ..item import Item
from ..plugin import FilterPlugin, FormatterPlugin, SinkPlugin, SourcePlugin, Target
from ..scheduler import parse_interval_spec
from ..timing import DAILY_KEY, check_quota, check_window, quota_state
from .bus import MessageBus
from .expr import ExprError, evaluate
from .cursor import save_snapshot, settle as settle_cursors
from .state import NodeStateStore

log = logging.getLogger(__name__)

# path 只留最近这么多跳。永久循环里它会一直长，而判"真环"只需要最近的重复。
PATH_KEEP = 128
# 一个合流桶最多存多少条。环里同一个 origin 会反复到达，不设上限就是无界增长。
MERGE_BUCKET_MAX = 200
# 拼接时最多带多少个媒体。Telegram 一个相册就是 10 个，telethon 会按 10 个一组
# 自己切且组之间不等待，而输出限速是这一跳结束之后才生效的。
CONCAT_MEDIA_MAX = 10
# 同一节点连续失败多少次就熔断
FAIL_STREAK_MAX = 3
# 熔断后多久放一条消息进去探路
DEGRADED_COOLDOWN = 600.0


@dataclass
class NodeResult:
    items: list[Item] = field(default_factory=list)
    status: str = "ok"                  # ok 有产出 / drop 正常终止 / error 失败
    error: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
    delay: float = 0.0                  # 产出的消息延后多久可见（delay 节点用）
    throttle: float = 0.0               # 本跳写完之后消费者要等多久（输出限速用）
    port: str = "out"                   # 从哪个出口出去（分流节点用）
    # 合流提前结算时被撤掉的那些到期消息所属的触发，写完之后要补记一次运行记录
    orphan_execs: set = field(default_factory=set)
    # 被合掉的各路因果链。不留住的话，合流这一步就把 N-1 条链断掉了
    parent_origins: list = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)   # 追加到下游消息的 meta


class NodeExecutor:
    def __init__(self, settings: Any, registry: Any, clients: Any, bus: MessageBus) -> None:
        self.settings = settings
        self.registry = registry
        self.clients = clients
        self.bus = bus
        self.detector: Any = None
        # 同一节点连续失败多少次就熔断。放内存：重启本来就等于一次重试机会。
        self._fail_streak: dict[tuple[int, str], int] = {}

    # ------------------------------------------------------------------ 入口
    async def run(self, msg: dict[str, Any]) -> float:
        """执行一跳，返回本跳之后消费者应当等待的秒数（输出节点的发送限速）。"""
        started = utcnow()
        wid, nid = msg["workflow_id"], msg["node_id"]
        loaded = await self._load(wid, nid)
        if loaded is None:
            # 节点在消息排队期间被删了：正常终止，不算失败
            await self._write(msg, NodeResult(status="drop", error="节点已不存在"), [], started)
            return 0.0
        wf, node, edges, account, channels = loaded

        # 这条因果链被判定为失控之后，属于它的消息一到就挂起。
        # 别的数据照常流——规格明确要求跳闸只暂停分支，不许杀掉整个工作流。
        pause = await self._branch_pause(msg["workflow_id"], msg.get("origin_id") or "")
        if pause is not None:
            await self._park(msg, f"所属分支已被暂停：{pause}")
            return 0.0

        # 停用之后，队列里可能还压着在途消息（有 delay 节点时窗口就是延迟时长本身）。
        # 不拦的话，用户点了「停用」频道里还在冒新帖。
        # 但手动触发是例外：那是用户当场的明确动作，"触发一次"就该跑一次，
        # 不该要求先把每 10 分钟的常驻定时打开。真要叫停，点「停用」会把
        # 在途的消息一并挂起（含手动那批）。
        if not wf.enabled and not msg["meta"].get("manual"):
            await self._park(msg, "工作流已停用")
            return 0.0
        if not node.enabled:
            # 停用一个中间节点，用户的心理预期是"把这一步摘掉、内容照流"，
            # 绝不是"这批内容就此作废"。所以它不算有结论，游标不能越过。
            await self._write(msg, NodeResult(status="drop", error="节点已停用",
                                              detail={"settles": False}), edges, started)
            return 0.0
        if node.status == "degraded":
            # 熔断不是永久终态：过了冷却期放一条进去探路。一次网络抖动就把节点
            # 永久钉死、还要人工干预才恢复，对无人值守的桌面应用太脆。
            cooled = (utcnow() - (node.updated_at or utcnow())).total_seconds()
            if cooled < DEGRADED_COOLDOWN:
                await self._park(
                    msg, f"节点已熔断：{node.status_note}"
                         f"（{int(DEGRADED_COOLDOWN - cooled)} 秒后自动试一次）")
                return 0.0
            ctx_log = logging.getLogger(f"wf.{wid}")
            ctx_log.info("节点 %s 熔断冷却期已过，放一条消息进去探路", nid)

        ctx = RunContext(wf, self.settings, self.clients, account,
                         dry_run=bool(wf.dry_run), trigger=msg["meta"].get("trigger", "graph"),
                         registry=self.registry)
        ctx.state = NodeStateStore(wid, nid)
        ctx.log = logging.getLogger(f"wf.{wid}.{wf.name}.{nid}")
        plugin = None
        try:
            handler = getattr(self, f"_run_{node.kind}", None)
            if handler is None:
                result = NodeResult(status="error", error=f"不认识的节点类型：{node.kind}")
            else:
                plugin = await self._build_plugin(node, ctx)
                result = await handler(node, ctx, msg, plugin=plugin, channels=channels)
        except asyncio.CancelledError:
            # 关停时被取消：把消息放回 pending 再往外抛，别让它停在 running——
            # 那样重启后 recover_orphans 会当成孤儿重跑，输出节点就重复发一遍。
            await self.bus.retry_later(msg["id"], delay=0, error="进程关停，稍后重试")
            if plugin is not None:
                try:
                    await plugin.teardown(ctx)
                except Exception:
                    pass
            await ctx.close()
            raise
        except Exception as e:
            result = NodeResult(status="error", error=f"{type(e).__name__}: {e}",
                                detail={"tb": traceback.format_exc()[-2000:]})
            ctx.log.error("节点 %s 执行失败：%s", nid, e)
        finally:
            if plugin is not None:
                try:
                    await plugin.teardown(ctx)
                except Exception as e:
                    ctx.log.debug("teardown 出错：%s", e)
            # 不删临时文件：线性模式下一轮从采集到发送是一个 ctx，删掉没问题；
            # 图模式里采集和发送是两跳、两个 ctx，这里删掉的话下游拿到的是
            # 一个已经不存在的路径。改由媒体保留策略统一回收。
            ctx._temp_files.clear()
            await ctx.close()

        await self._write(msg, result, edges, started)
        return result.throttle

    # ------------------------------------------------------------------ 读
    async def _load(self, wid: int, nid: str):
        async with session_scope() as s:
            wf = (await s.execute(
                select(Workflow).where(Workflow.id == wid)
            )).scalar_one_or_none()
            if wf is None:
                return None
            node = (await s.execute(
                select(GraphNode).where(GraphNode.workflow_id == wid, GraphNode.node_id == nid)
            )).scalar_one_or_none()
            if node is None:
                return None
            edges = (await s.execute(
                select(GraphEdge).where(GraphEdge.workflow_id == wid, GraphEdge.src_node == nid,
                                        GraphEdge.enabled.is_(True))
                .order_by(GraphEdge.position, GraphEdge.id)
            )).scalars().all()
            account = await s.get(Account, wf.account_id) if wf.account_id else None
            channels = {c.id: c for c in (await s.execute(select(Channel))).scalars().all()}
            s.expunge_all()
        return wf, node, edges, account, channels

    async def _build_plugin(self, node: GraphNode, ctx: RunContext):
        if not node.plugin:
            return None
        # 每跳新建实例：和线性执行器一样的生命周期契约，插件把本轮状态放在
        # self.* 上的写法（tg_channel 就是）才不会串。热重载也因此天然生效。
        p = self.registry.cls(node.plugin)(node.config or {})
        await p.setup(ctx)
        return p

    # -------------------------------------------------------------- 各类节点
    async def _run_source(self, node, ctx, msg, *, plugin, **_):
        if not isinstance(plugin, SourcePlugin):
            return NodeResult(status="error", error=f"{node.plugin} 不是信息源插件")
        # 时段闸门只管定时触发，手动一律放行——和线性执行器一致
        if msg["meta"].get("trigger") == "schedule":
            gate = check_window(ctx.workflow)
            if not gate.allowed:
                return NodeResult(status="drop", error=gate.reason)
        items = await plugin.fetch(ctx)
        if ctx.on_commit and not ctx.dry_run:
            # 这个源要推游标。试运行一条都不发，绝不能存快照去推它。图模式里"有没有结论"要等整次触发跑完才知道，
            # 所以把现场存下来（插件 fetch 期间记在实例属性上的扫描窗口 + 本轮产出），
            # 等 _finalize_execution 收口时再补做一次结算。
            await save_snapshot(node.workflow_id, node.node_id,
                                msg.get("execution_id") or "", plugin, items)
        return NodeResult(items=items, status="ok" if items else "drop",
                          detail={"fetched": len(items)})

    async def _run_filter(self, node, ctx, msg, *, plugin, **_):
        if not isinstance(plugin, FilterPlugin):
            return NodeResult(status="error", error=f"{node.plugin} 不是过滤插件")
        item = self._item(msg)
        kept = [i for i in await plugin.apply([item], ctx) if not i.dropped]
        return NodeResult(items=kept, status="ok" if kept else "drop",
                          error="" if kept else "被过滤")

    async def _run_formatter(self, node, ctx, msg, *, plugin, **_):
        if not isinstance(plugin, FormatterPlugin):
            return NodeResult(status="error", error=f"{node.plugin} 不是格式化插件")
        out = await plugin.format(self._item(msg), ctx)
        if out is None or out.dropped:
            return NodeResult(status="drop", error="格式化时被丢弃")
        return NodeResult(items=[out])

    async def _run_dedup(self, node, ctx, msg, **_):
        item = self._item(msg)
        cfg = node.config or {}
        # uid 模式下没有 uid 就退回内容哈希——Item.fingerprint 本身就是这个语义；
        # 选 content 则强制用内容哈希，忽略 uid
        key = item.fingerprint() if cfg.get("key") != "content" else item.content_hash()
        if (node.config or {}).get("scope") == "node":
            seen = bool(await ctx.state.get(f"seen:{key}"))
        else:
            seen = await ctx.is_seen(key)
        if seen:
            return NodeResult(status="drop", error="已经处理过")
        # 指纹随消息往下传，由输出节点在**发送成功之后**记账。
        # 在这里就记的话，任何一次下游失败都等于这条内容永久丢失。
        return NodeResult(items=[item], meta={"_dedup_key": key,
                                              "_dedup_scope": (node.config or {}).get("scope", "workflow"),
                                              "_dedup_node": node.node_id})

    async def _run_limit(self, node, ctx, msg, **_):
        cap = int((node.config or {}).get("count") or 0)
        if cap <= 0:
            return NodeResult(items=[self._item(msg)])
        # 计数口径 = 同一次触发（execution_id）内本节点已经放行过几条。
        # 不用额外的状态表：执行记录本来就要写，直接数它。
        async with session_scope() as s:
            passed = (await s.execute(
                select(func.count(GraphNodeRun.id)).where(
                    GraphNodeRun.workflow_id == node.workflow_id,
                    GraphNodeRun.node_id == node.node_id,
                    GraphNodeRun.execution_id == msg["execution_id"],
                    GraphNodeRun.status == "ok")
            )).scalar() or 0
        if passed >= cap:
            return NodeResult(status="drop", error=f"本轮已放行 {passed} 条，达到上限 {cap}")
        return NodeResult(items=[self._item(msg)])

    async def _run_delay(self, node, ctx, msg, **_):
        value = str((node.config or {}).get("value") or "10m")
        try:
            base, span = parse_interval_spec(value)
        except Exception as e:
            return NodeResult(status="error", error=f"延迟时长不合法：{e}")
        # 区间写法取中值：随机在这里没有意义（同一批消息应当一起到期）
        return NodeResult(items=[self._item(msg)], delay=float(base))

    async def _run_router(self, node, ctx, msg, **_):
        item = self._item(msg)
        expr = str((node.config or {}).get("expr") or "")
        try:
            # 放线程里跑：正则回溯卡住时线程杀不掉，但事件循环还活着，
            # 界面和「停用」按钮还能用，用户可以自己止血。
            hit = await asyncio.to_thread(evaluate, expr, item)
        except ExprError as e:
            return NodeResult(status="error", error=f"分流条件有问题：{e}")
        return NodeResult(items=[item], port="match" if hit else "else",
                          detail={"matched": hit})

    async def _run_merge(self, node, ctx, msg, **_):
        """合流。普通节点是"谁先到就处理谁"，只有它负责等待和聚合。"""
        cfg = node.config or {}
        strategy = str(cfg.get("strategy") or "append")
        if strategy == "append":
            # 到一条放一条，不等待——这是最常用的"把几条分支并成一股"
            return NodeResult(items=[self._item(msg)])

        flush_key = msg["meta"].get("_merge_flush")
        if flush_key is not None:
            # 这是窗口到期时自己给自己投的那条消息
            return await self._flush_bucket(node, msg, str(flush_key), timed_out=True)

        bucket = self._bucket_key(node, msg, cfg)
        _first, dropped = await self._buffer_put(node, msg, bucket)
        # 不能只在"桶为空"时排到期消息：那条消息可能被挂起（停用工作流、切回线性、
        # 重试耗尽、节点熔断都会 park 它），而桶里还留着行。之后每一条到达都不是
        # "第一条"，就再也不会有新的到期消息——合流节点从此只进不出。
        if not await self._has_pending_flush(node, bucket):
            await self._schedule_flush(node, msg, bucket, self._merge_timeout(cfg))
        if strategy in ("wait_all", "key_based"):
            if await self._bucket_ready(node, bucket):
                return await self._flush_bucket(node, msg, bucket, timed_out=False)
            # 上游已经确定失败/被挂起时不要傻等：那一路永远不会来了，
            # 干等满窗口（最长一天）之后按 on_timeout 结算，等于白白滞后一整轮
            if await self._upstream_dead(node, msg):
                return await self._flush_bucket(node, msg, bucket, timed_out=True)
        note = "已进入合流缓冲，等待其它分支"
        if dropped:
            note += f"（缓冲超上限，挤掉了最旧的 {dropped} 条）"
        # 进了缓冲还没结论；被挤掉的更没有。两种都不能让游标越过。
        return NodeResult(status="drop", error=note, detail={"settles": False})

    # ---------------------------------------------------------------- 合流细节
    @staticmethod
    def _merge_timeout(cfg: dict[str, Any]) -> float:
        """等待窗口。不允许无限等：上游一旦失败就不再产出消息，
        无限等的桶会变成静默的消息坟场——内容进去了，什么都没发生，日志里也没有。"""
        raw = str(cfg.get("timeout") or "10m")
        try:
            base, _ = parse_interval_spec(raw)
        except Exception:
            base = 600
        return float(min(max(base, 5), 86400))

    @staticmethod
    def _bucket_key(node: GraphNode, msg: dict[str, Any], cfg: dict[str, Any]) -> str:
        """哪些消息算一组。

        默认是**单桶**，即按到达顺序配对。这是唯一能让"两个各自定时的源汇入一个
        合流节点"真正配上对的口径——两个源各自触发时因果链本来就不同，按因果链
        分组的话它们永远凑不齐，只能等到超时各自放行，等于合流没生效。
        需要按因果链配对时填 origin（同一次采集产出的内容），或者填字段名/meta.键名。
        """
        expr = str(cfg.get("key") or "").strip()
        if not expr:
            return "-"
        if expr == "origin":
            return msg.get("origin_id") or msg["message_id"]
        if expr == "execution":
            return msg.get("execution_id") or "-"
        item = Item.from_wire(msg.get("payload") or {})
        if expr.startswith("meta."):
            return str((item.meta or {}).get(expr[5:], ""))[:200] or "-"
        return str(getattr(item, expr, ""))[:200] or "-"

    async def _buffer_put(self, node: GraphNode, msg: dict[str, Any], bucket: str) -> tuple[bool, int]:
        """把这一路存进桶，返回 (是不是这个桶的第一条, 被挤掉的旧条数)。"""
        async with session_scope() as s:
            rows = (await s.execute(
                select(GraphMergeBuffer).where(
                    GraphMergeBuffer.workflow_id == node.workflow_id,
                    GraphMergeBuffer.node_id == node.node_id,
                    GraphMergeBuffer.bucket_key == bucket).order_by(GraphMergeBuffer.id)
            )).scalars().all()
            dropped = 0
            # 桶不能无限长：环里同一个 origin 会反复到达，不设上限就是无界增长
            while len(rows) >= MERGE_BUCKET_MAX:
                await s.delete(rows.pop(0))
                dropped += 1
            s.add(GraphMergeBuffer(
                workflow_id=node.workflow_id, node_id=node.node_id, bucket_key=bucket,
                src_node=msg.get("src_node_id") or "", message_id=msg["message_id"],
                execution_id=msg.get("execution_id") or "", origin_id=msg.get("origin_id") or "",
                payload=msg.get("payload") or {}, meta=msg.get("meta") or {}))
            return (not rows and not dropped), dropped

    @staticmethod
    def _flush_filter(node: GraphNode, bucket: str):
        return (GraphMessage.workflow_id == node.workflow_id,
                GraphMessage.node_id == node.node_id,
                GraphMessage.status == "pending",
                func.json_extract(GraphMessage.meta, "$._merge_flush") == bucket)

    async def _has_pending_flush(self, node: GraphNode, bucket: str) -> bool:
        async with session_scope() as s:
            return (await s.execute(
                select(GraphMessage.id).where(*self._flush_filter(node, bucket)).limit(1)
            )).first() is not None

    async def _bucket_ready(self, node: GraphNode, bucket: str) -> bool:
        """wait-all / key-based：每条启用的入边都到了至少一条才算齐。"""
        async with session_scope() as s:
            # 只等"边启用**且**上游节点启用"的分支。把某个上游节点停用是最自然的
            # 临时静音操作，不排除的话这个桶永远凑不齐，每一批都要等满整个窗口。
            expected = {r for r in (await s.execute(
                select(GraphEdge.src_node)
                .join(GraphNode, and_(GraphNode.workflow_id == GraphEdge.workflow_id,
                                      GraphNode.node_id == GraphEdge.src_node))
                .where(GraphEdge.workflow_id == node.workflow_id,
                       GraphEdge.dst_node == node.node_id,
                       GraphEdge.enabled.is_(True),
                       GraphNode.enabled.is_(True))
            )).scalars().all()}
            got = {r for r in (await s.execute(
                select(GraphMergeBuffer.src_node).where(
                    GraphMergeBuffer.workflow_id == node.workflow_id,
                    GraphMergeBuffer.node_id == node.node_id,
                    GraphMergeBuffer.bucket_key == bucket)
            )).scalars().all()}
        return bool(expected) and expected.issubset(got)

    async def _upstream_dead(self, node: GraphNode, msg: dict[str, Any]) -> bool:
        """本次触发里有没有节点已经失败或被挂起。"""
        execution_id = msg.get("execution_id") or ""
        if not execution_id:
            return False
        async with session_scope() as s:
            return (await s.execute(
                select(GraphNodeRun.id).where(
                    GraphNodeRun.workflow_id == node.workflow_id,
                    GraphNodeRun.execution_id == execution_id,
                    GraphNodeRun.status.in_(("error", "park"))).limit(1)
            )).first() is not None

    async def _schedule_flush(self, node: GraphNode, msg: dict[str, Any],
                              bucket: str, timeout: float) -> None:
        """给自己投一条到期消息。用队列本身做定时器，不引入新的计时机制。"""
        async with session_scope() as s:
            s.add(GraphMessage(**MessageBus.build(
                workflow_id=node.workflow_id, node_id=node.node_id, payload={},
                execution_id=msg.get("execution_id") or "", origin_id=msg.get("origin_id") or "",
                meta={"_merge_flush": bucket, "trigger": "merge"},
                # 路径要继承：不继承的话，环里每一次超时结算都把 path 清零，
                # 忙循环检测最基础的"真环判定"就永远为假
                path=list(msg.get("path") or []), hop=int(msg.get("hop") or 0),
                visible_at=utcnow() + dt.timedelta(seconds=timeout))))
        self.bus.notify()

    async def _flush_bucket(self, node: GraphNode, msg: dict[str, Any],
                            bucket: str, *, timed_out: bool) -> NodeResult:
        cfg = node.config or {}
        orphan_execs: set[str] = set()
        strategy = str(cfg.get("strategy") or "append")
        async with session_scope() as s:
            rows = (await s.execute(
                select(GraphMergeBuffer).where(
                    GraphMergeBuffer.workflow_id == node.workflow_id,
                    GraphMergeBuffer.node_id == node.node_id,
                    GraphMergeBuffer.bucket_key == bucket).order_by(GraphMergeBuffer.id)
            )).scalars().all()
            items = [Item.from_wire(r.payload) for r in rows]
            srcs = [r.src_node for r in rows]
            # 每一路自己的去重指纹。下游只会带一份 meta（最后到达那条的），
            # 不汇总的话其余分支的内容永远不落去重账，下一轮会被再抓再发一遍。
            dedup_keys = [
                {"key": (r.meta or {}).get("_dedup_key"),
                 "scope": (r.meta or {}).get("_dedup_scope", "workflow"),
                 "node": (r.meta or {}).get("_dedup_node", "")}
                for r in rows if (r.meta or {}).get("_dedup_key")
            ]
            origins = [r.origin_id for r in rows if r.origin_id]
            for r in rows:
                await s.delete(r)
            if not timed_out and items:
                # 提前凑齐就把还没到期的"结算提醒"撤掉，免得到点再空跑一次。
                # 但它往往是**另一次触发**在途的最后一条消息——删掉之后那次触发
                # 零 pending 零 running，再也没有一跳会去写它的运行记录，
                # 所以先把 execution_id 收起来，写完事务补结算一次。
                doomed = (await s.execute(
                    select(GraphMessage.execution_id).where(*self._flush_filter(node, bucket))
                )).scalars().all()
                orphan_execs.update(x for x in doomed if x)
                await s.execute(delete(GraphMessage).where(*self._flush_filter(node, bucket)))
        if not items:
            return NodeResult(status="drop", error="这组已经结算过了",
                              detail={"settles": False}, orphan_execs=orphan_execs)

        if timed_out and strategy in ("wait_all", "key_based"):
            if str(cfg.get("on_timeout") or "emit_partial") == "drop":
                return NodeResult(status="drop", orphan_execs=orphan_execs,
                                  error=f"等待超时，只等到 {len(items)} 路，按配置丢弃")
        detail = {"bucket": bucket, "count": len(items), "sources": srcs, "timed_out": timed_out}

        merged_meta = {"_dedup_keys": dedup_keys} if dedup_keys else {}
        if strategy == "latest":
            return NodeResult(items=[items[-1]], detail=detail,
                              meta=merged_meta, orphan_execs=orphan_execs, parent_origins=origins)
        if strategy == "concat":
            head = items[0]
            head.text = "\n\n".join(i.text for i in items if i.text)
            media = [m for i in items for m in (i.media or [])]
            if len(media) > CONCAT_MEDIA_MAX:
                detail["media_dropped"] = len(media) - CONCAT_MEDIA_MAX
                media = media[:CONCAT_MEDIA_MAX]
            head.media = media
            head.meta = {**(head.meta or {}), "merged_count": len(items)}
            return NodeResult(items=[head], detail=detail, meta=merged_meta,
                              orphan_execs=orphan_execs, parent_origins=origins)
        # wait_all / key_based：逐条放行，保持"一条消息一次执行"的下游语义
        for i in items:
            i.meta = {**(i.meta or {}), "merge_group": bucket}
        return NodeResult(items=items, detail=detail, meta=merged_meta,
                          orphan_execs=orphan_execs, parent_origins=origins)

    async def _run_output(self, node, ctx, msg, *, plugin, channels, **_):
        if not isinstance(plugin, SinkPlugin):
            return NodeResult(status="error", error=f"{node.plugin} 不是输出插件")
        # 发送发生在写事务之前，中间关停会让这条消息回到 pending 重跑。
        # 发之前先看这条消息是不是已经发过了——重复发帖是不可回滚的。
        state = NodeStateStore(node.workflow_id, node.node_id)
        sent_key = f"sent:{msg['message_id']}"
        if await state.get(sent_key):
            # 内容其实已经投出去了。不标出来的话游标会当它"没结论"，
            # 下一轮重抓、用一个新的 message_id 重发一遍——幂等键拦不住新消息。
            return NodeResult(status="drop", detail={"already_sent": True},
                              error="这条消息之前已经发送过了（进程中断后重跑）")

        item = self._item(msg)
        cfg = node.config or {}
        targets = [t for t in (cfg.get("targets") or []) if t.get("enabled", True)]
        if not targets:
            return NodeResult(status="error", error="这个输出节点没有勾选任何频道")

        wf = ctx.workflow
        # 每日配额只管定时触发，和线性执行器一致（runner 只在 trigger=="schedule" 时查）。
        # 手动运行和试运行一律放行——用户想确认模板改对没有的时候，
        # 不该被一个和内容无关的闸门挡住。
        if wf.daily_limit and ctx.trigger == "schedule" and not ctx.dry_run:
            gate = check_quota(wf.daily_limit, await self._quota(wf))
            if not gate.allowed:
                return NodeResult(status="drop", error=gate.reason)

        sent, detail = 0, []
        for t in targets:
            ch = channels.get(t["channel_id"])
            if ch is None:
                detail.append({"target": t["channel_id"], "ok": False, "error": "频道已删除"})
                continue
            if not ch.enabled:
                # 频道页上那个开关是用户唯一的紧急止血阀，线性执行器认它（runner.py:326），
                # 图模式也必须认
                detail.append({"target": ch.title, "ok": False, "error": "频道已停用"})
                continue
            target = Target(channel_id=ch.id, title=ch.title, peer=ch.peer,
                            account_id=ch.account_id, overrides=t.get("overrides") or {})
            try:
                if ctx.dry_run:
                    ctx.log.info("[试运行] -> %s：%s", target.title, item.preview())
                else:
                    info = await plugin.send(item, target, ctx)
                    detail.append({"target": target.title, "ok": True, **(info or {})})
                sent += 1
            except Exception as e:
                detail.append({"target": target.title, "ok": False,
                               "error": f"{type(e).__name__}: {e}"})
                ctx.log.error("发送到 %s 失败：%s", target.title, e)

        if not sent:
            # 全部目标都被停用不算失败，否则会一直重试
            if all("停用" in (d.get("error") or "") for d in detail):
                return NodeResult(status="drop", error="所有目标频道都已停用",
                                  detail={"targets": detail})
            return NodeResult(status="error", error="全部目标发送失败", detail={"targets": detail})

        if not ctx.dry_run:
            await state.set(sent_key, True)          # 先记账再做别的，中断也不会重发
            # 发出去之后才记去重账，失败的下次还能重来
            # 一条消息可能带着多路的指纹（合流把几路并成了一股），逐个记账。
            # 只记一个的话，其余那几路的内容下一轮会被再抓再发一遍。
            marks = list(msg["meta"].get("_dedup_keys") or [])
            if msg["meta"].get("_dedup_key"):
                marks.append({"key": msg["meta"]["_dedup_key"],
                              "scope": msg["meta"].get("_dedup_scope", "workflow"),
                              "node": msg["meta"].get("_dedup_node", "")})
            for m in marks:
                if not m.get("key"):
                    continue
                if m.get("scope") == "node" and m.get("node"):
                    await NodeStateStore(node.workflow_id, m["node"]).set(f"seen:{m['key']}", True)
                else:
                    await ctx.mark_seen(m["key"])
            if wf.daily_limit:
                await self._quota_bump(wf, sent)
            await self._bump_stats(wf, sent)
        return NodeResult(status="ok", detail={"targets": detail, "sent": sent},
                          # 限速不在这里 sleep：那样会把这条消息卡在 running 上，
                          # 中断即重发。改成让消费者在写完事务之后再等。
                          throttle=float(cfg.get("send_interval") or 0))

    # ------------------------------------------------------------------ 写
    async def _write(self, msg: dict[str, Any], result: NodeResult,
                     edges: list[GraphEdge], started: dt.datetime) -> None:
        visible = utcnow() + dt.timedelta(seconds=result.delay) if result.delay else utcnow()
        produced = 0
        async with session_scope() as s:
            await self.bus.finish(s, msg["id"],
                                  "failed" if result.status == "error" else "done",
                                  result.error)
            if result.status == "ok" and result.items and edges:
                path = (list(msg["path"]) + [msg["node_id"]])[-PATH_KEEP:]
                meta = {**msg["meta"], **result.meta}
                for e in edges:
                    if e.src_port != result.port:
                        continue        # 分流节点只走命中的那个出口
                    for it in result.items:
                        s.add(GraphMessage(**MessageBus.build(
                            workflow_id=msg["workflow_id"], node_id=e.dst_node,
                            payload=it.to_wire(), execution_id=msg["execution_id"],
                            origin_id=msg["origin_id"], src_node_id=msg["node_id"],
                            meta=meta, hop=msg["hop"] + 1, path=path, visible_at=visible,
                            # 合流会把 N 路并成下游的一股，被合掉的那几条因果链
                            # 记在这里，不然它们在这一步就断了
                            parent_origins=(list(msg["parent_origins"]) + result.parent_origins
                                            if result.parent_origins else msg["parent_origins"]),
                        )))
                        produced += 1
            s.add(GraphNodeRun(
                workflow_id=msg["workflow_id"], node_id=msg["node_id"],
                message_id=msg["message_id"], origin_id=msg["origin_id"],
                execution_id=msg["execution_id"], started_at=started, finished_at=utcnow(),
                status=result.status, produced=produced, error=result.error[:2000],
                detail={**(result.detail or {}), "trigger": msg["meta"].get("trigger", "")},
            ))
        await self._track_failure(msg["workflow_id"], msg["node_id"], result.status)
        for ex in result.orphan_execs:
            # 这些触发的最后一条在途消息刚被合流撤掉了，没人会再替它们收口
            await self._finalize_execution(msg["workflow_id"], ex)
        if produced:
            self.bus.notify()
        else:
            # 这一跳没有产出下游，本次触发可能已经跑完了——跑完就汇总成一条运行记录，
            # 否则「记录」按钮和概览的「最近运行」在图模式下永远是空的
            await self._finalize_execution(msg["workflow_id"], msg.get("execution_id") or "")

    async def _finalize_execution(self, wid: int, execution_id: str) -> None:
        """一次触发的消息全部处理完之后，把逐跳记录汇总成一条 RunLog。

        图的粒度是"某个节点处理了一条消息"，而运行记录页和概览要的是"这一轮
        采集了多少、发了多少"。两者都要，各记各的。
        """
        if not execution_id:
            return
        try:
            async with session_scope() as s:
                left = (await s.execute(
                    select(GraphMessage.id).where(
                        GraphMessage.workflow_id == wid,
                        GraphMessage.execution_id == execution_id,
                        GraphMessage.status.in_(("pending", "running"))).limit(1)
                )).first()
                if left:
                    return                       # 还没跑完
                rows = (await s.execute(
                    select(GraphNodeRun, GraphNode.kind)
                    .join(GraphNode, and_(GraphNode.workflow_id == GraphNodeRun.workflow_id,
                                          GraphNode.node_id == GraphNodeRun.node_id))
                    .where(GraphNodeRun.workflow_id == wid,
                           GraphNodeRun.execution_id == execution_id)
                )).all()
                if not rows:
                    return
                fetched = sum(r.produced for r, k in rows if k == "source")
                out_rows = [(r, k) for r, k in rows if k == "output"]
                sent = sum(1 for r, _ in out_rows if r.status == "ok")
                # park 也要算进"没成"：整轮被熔断/停用/失控挂起时，
                # 只数 error 的话算出来是 ok，界面上一片绿——
                # 而这条记录本来就是为了解释"这一轮为什么没发"
                failed = sum(1 for r, _ in rows if r.status in ("error", "park"))
                detail = [{"node": r.node_id, "status": r.status, "error": r.error}
                          for r, _ in rows if r.error][:50]
                started = min((r.started_at for r, _ in rows if r.started_at), default=utcnow())
                finished = max((r.finished_at for r, _ in rows if r.finished_at), default=utcnow())
                # 整轮都是"没启用/已切走"这类拦截，不算运行失败——那会让卡片和
                # 概览显示成红色的失败，而实际上只是它没被允许跑
                skipped = all(r.status == "park" and "已停用" in (r.error or "")
                              for r, _ in rows)
                status = ("skipped" if skipped else
                          "error" if failed and not sent else
                          "partial" if failed else "ok")
                first_bad = next((r for r, _ in rows if r.status in ("error", "park")), None)
                row = (await s.execute(
                    select(RunLog).where(RunLog.workflow_id == wid,
                                         RunLog.execution_id == execution_id)
                )).scalar_one_or_none()
                fresh = row is None
                if row is None:
                    trig = next((r.detail.get("trigger") for r, _ in
                                 sorted(rows, key=lambda x: x[0].id)
                                 if isinstance(r.detail, dict) and r.detail.get("trigger")), "")
                    row = RunLog(workflow_id=wid, execution_id=execution_id,
                                 trigger=trig or "schedule", started_at=started)
                    s.add(row)
                row.finished_at = finished
                row.status = status
                row.fetched = fetched
                row.kept = len(out_rows)
                row.sent = sent
                row.failed = failed
                row.detail = detail
                # 只认真正出问题的节点。detail 里也收着 drop 的说明（"本轮已放行 2 条，
                # 达到上限 2" 这种正常限量就是 drop），拿它兜底会让一次完全正常的运行
                # 在记录里挂一行红字，把"限量生效了"读成"出错了"。
                row.error = ((first_bad.error if first_bad else "") or "")[:500]
                # 工作流行上的"上次运行/状态"也在这里写。只在发送成功时写的话，
                # 一轮全部失败之后卡片上仍然显示"上次运行：很久以前 · 正常"。
                # total_runs 只在第一次写这条记录时 +1：合流的到期消息会让同一次触发
                # 被重复收口，每次都加就把"轮数"数成了"收口次数"
                vals = {"last_run_at": finished, "last_status": status,
                        "last_note": ((first_bad.error if first_bad else "") or "")[:200]}
                if fresh:
                    vals["total_runs"] = Workflow.total_runs + 1
                await s.execute(update(Workflow).where(Workflow.id == wid).values(**vals))
        except Exception as e:                   # 汇总失败不该影响消息处理
            log.warning("汇总运行记录失败：%s", e)
            return
        # 这一轮到此为止，可以判定每条内容的归宿了——顺序抓取的游标在这里推进
        try:
            await settle_cursors(self.settings, self.registry, self.clients, wid, execution_id)
        except Exception as e:
            log.warning("游标结算失败：%s", e)

    async def _track_failure(self, workflow_id: int, node_id: str, status: str) -> None:
        """连续失败到阈值就熔断这个节点。

        针对的是"配置错了或插件本身炸了"这类故障：不熔断的话，经过它的每一条
        消息都要各自重试到耗尽再各自失败，把重试预算和执行记录一起烧光——
        而填错一个频道标识就是本应用最常见的故障。
        """
        key = (workflow_id, node_id)
        if status != "error":
            self._fail_streak.pop(key, None)
        # 成功一次就解除熔断：探路的那条成了，说明故障已经过去
        if status == "ok":
            async with session_scope() as s:
                await s.execute(update(GraphNode).where(
                    GraphNode.workflow_id == workflow_id, GraphNode.node_id == node_id,
                    GraphNode.status == "degraded").values(status="ok", status_note=""))
        streak = self._fail_streak.get(key, 0) + 1
        self._fail_streak[key] = streak
        if streak < FAIL_STREAK_MAX:
            return
        note = f"连续 {streak} 次失败，已暂停执行。修好配置后在画布上点「恢复」"
        async with session_scope() as s:
            await s.execute(update(GraphNode).where(
                GraphNode.workflow_id == workflow_id, GraphNode.node_id == node_id,
                GraphNode.status != "degraded").values(status="degraded", status_note=note))
        log.warning("节点 %s/%s 已熔断：%s", workflow_id, node_id, note)

    async def _branch_pause(self, workflow_id: int, origin_id: str) -> str | None:
        if not origin_id:
            return None
        async with session_scope() as s:
            row = (await s.execute(
                select(GraphBranchPause).where(
                    GraphBranchPause.workflow_id == workflow_id,
                    GraphBranchPause.origin_id == origin_id)
            )).scalar_one_or_none()
            return row.note or row.reason if row else None

    async def trip_branch(self, msg: dict[str, Any], detail: dict[str, Any]) -> None:
        """判定为忙循环：暂停这条分支、挂起这条消息、把诊断记下来。

        不杀工作流、不停节点——同一个节点上别的因果链还在正常工作。
        """
        if detail.get("reason") == "rate":
            note = (f"这条链已经连续 {int(detail.get('over_seconds') or 0)} 秒占用四分之一的执行预算"
                    f"（60 秒内 {detail.get('chain_hops')} 跳）。内容一直在变，所以不算原地打转，"
                    f"但速率明显不正常——确认没问题的话点恢复，恢复之后不会再按速率拦它")
        else:
            note = (f"同一条内容在 {detail.get('revisits')} 次重访里只出现过 "
                    f"{detail.get('distinct_sigs')} 种内容，60 秒内跑了 "
                    f"{detail.get('chain_hops')} 跳，判定为原地打转")
        async with session_scope() as s:
            exists = (await s.execute(
                select(GraphBranchPause).where(
                    GraphBranchPause.workflow_id == msg["workflow_id"],
                    GraphBranchPause.origin_id == msg["origin_id"])
            )).scalar_one_or_none()
            if exists is None:
                s.add(GraphBranchPause(
                    workflow_id=msg["workflow_id"], origin_id=msg["origin_id"],
                    node_id=msg["node_id"],
                    reason="runaway_rate" if detail.get("reason") == "rate" else "runaway_cycle",
                    note=note, detail=detail))
            await self.bus.finish(s, msg["id"], "parked", f"runaway_cycle：{note}")
            s.add(GraphNodeRun(
                workflow_id=msg["workflow_id"], node_id=msg["node_id"],
                message_id=msg["message_id"], origin_id=msg["origin_id"],
                execution_id=msg.get("execution_id") or "", finished_at=utcnow(),
                status="park", error=f"runaway_cycle：{note}", detail=detail))
        log.warning("工作流 %s 的分支 %s 判定为失控循环，已暂停：%s",
                    msg["workflow_id"], msg["origin_id"], note)
        await self._finalize_execution(msg["workflow_id"], msg.get("execution_id") or "")

    async def _park(self, msg: dict[str, Any], reason: str) -> None:
        async with session_scope() as s:
            await self.bus.finish(s, msg["id"], "parked", reason)
            s.add(GraphNodeRun(
                workflow_id=msg["workflow_id"], node_id=msg["node_id"],
                message_id=msg["message_id"], origin_id=msg["origin_id"],
                execution_id=msg["execution_id"], finished_at=utcnow(),
                status="park", error=reason,
            ))
        # 停用、切模式、熔断、重试耗尽——这几个最需要一条记录解释"这一轮为什么没发"
        await self._finalize_execution(msg["workflow_id"], msg.get("execution_id") or "")

    # ------------------------------------------------------------------ 杂项
    @staticmethod
    def _item(msg: dict[str, Any]) -> Item:
        return Item.from_wire(msg.get("payload") or {})

    async def _quota(self, wf: Workflow) -> dict[str, Any]:
        raw = await NodeStateStore(wf.id, "__wf__").get(DAILY_KEY)
        if raw is None:                       # 兼容线性模式写在 workflow_state 里的那份
            from ...db.models import WorkflowState
            async with session_scope() as s:
                row = (await s.execute(
                    select(WorkflowState).where(WorkflowState.workflow_id == wf.id,
                                                WorkflowState.key == DAILY_KEY)
                )).scalar_one_or_none()
                raw = (row.value or {}).get("v") if row else None
        return quota_state(raw, wf.timezone or "")

    async def _bump_stats(self, wf: Workflow, sent: int) -> None:
        """把发布结果回写到 workflows 行上。

        概览页、工作流卡片、累计发布数读的都是这几列。图模式只写 GraphNodeRun
        的话，这些数字会全线冻结——工作流其实在正常发帖，后台看起来像它死了，
        而这正是用户最可能误判并去乱动配置的场景。
        """
        async with session_scope() as s:
            # 只累加发布数。"上次运行/状态/轮数"由 _finalize_execution 按整轮写，
            # 那里才知道这一轮到底成没成。
            await s.execute(update(Workflow).where(Workflow.id == wf.id).values(
                total_sent=Workflow.total_sent + sent,
            ))

    async def _quota_bump(self, wf: Workflow, sent: int) -> None:
        state = await self._quota(wf)
        state["count"] = int(state.get("count") or 0) + sent
        await NodeStateStore(wf.id, "__wf__").set(DAILY_KEY, state)
