"""数据库模型。

实体关系：
    Account  1 ── n  Channel
    Workflow n ── n  Channel   （通过 WorkflowTarget，一个频道可挂载多个工作流）
    Workflow 1 ── n  RunLog / SeenItem / WorkflowState
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.ext.mutable import MutableDict, MutableList
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> dt.datetime:
    """当前 UTC 时间，naive。

    全库的时间列都是不带时区的 DateTime，写 aware 值进去 tzinfo 会被静默丢掉
    （而且不做换算），读回来是 naive——再拿它和 aware 的"现在"比较就抛
    TypeError。图运行时的空闲等待要算 MIN(visible_at) - 现在，这条路径在主
    循环里，抛出去整个运行时就停摆。所以这里统一返回 naive UTC，与
    overview.py 里已有的 dt.datetime.utcnow() 用法一致。
    """
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


class Account(Base):
    """一个 Telegram 账号（用户账号或 Bot）。"""

    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    api_id: Mapped[int] = mapped_column(Integer)
    api_hash: Mapped[str] = mapped_column(String(64))
    phone: Mapped[str] = mapped_column(String(32), default="")
    is_bot: Mapped[bool] = mapped_column(Boolean, default=False)
    bot_token: Mapped[str] = mapped_column(String(128), default="")
    proxy: Mapped[str] = mapped_column(String(255), default="")  # 形如 socks5://host:port
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(32), default="logged_out")
    me: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    channels: Mapped[list["Channel"]] = relationship(back_populates="account")

    @property
    def session_name(self) -> str:
        return f"acc_{self.id}"


class Channel(Base):
    """被运营的频道/群组。既可作为输出端，也可在插件里作为信息源。"""

    __tablename__ = "channels"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(128))
    peer: Mapped[str] = mapped_column(String(128))  # @username / -100xxxx / t.me/xxx
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(16), default="channel")  # channel/group/user
    note: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    resolved: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # 缓存 id/username/title
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    account: Mapped[Account | None] = relationship(back_populates="channels")
    targets: Mapped[list["WorkflowTarget"]] = relationship(
        back_populates="channel", cascade="all, delete-orphan"
    )


class Workflow(Base):
    """工作流 = 信息源 + 过滤链 + 格式化链 + 输出端 + 调度。"""

    __tablename__ = "workflows"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)

    source_plugin: Mapped[str] = mapped_column(String(64))
    source_config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # [{"plugin": "keyword", "config": {...}, "enabled": true}, ...]
    filters: Mapped[list[Any]] = mapped_column(JSON, default=list)
    formatters: Mapped[list[Any]] = mapped_column(JSON, default=list)
    sink_plugin: Mapped[str] = mapped_column(String(64), default="tg_send")
    sink_config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    # 调度：interval(秒，支持 "300-900" 区间随机) / cron(表达式) / manual
    schedule_type: Mapped[str] = mapped_column(String(16), default="interval")
    schedule_value: Mapped[str] = mapped_column(String(128), default="600")
    jitter: Mapped[int] = mapped_column(Integer, default=0)  # 额外随机抖动秒数

    # —— 什么时候允许搬运 ——
    # "09:00-23:00"；留空表示全天；支持跨夜写法 "22:00-06:00"
    active_hours: Mapped[str] = mapped_column(String(32), default="")
    # 允许运行的星期，1=周一 … 7=周日；空表示每天
    active_days: Mapped[list[Any]] = mapped_column(JSON, default=list)
    # 每天最多发布条数，0 表示不限
    daily_limit: Mapped[int] = mapped_column(Integer, default=0)
    timezone: Mapped[str] = mapped_column(String(48), default="")  # 空=服务器本地时区

    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    max_items_per_run: Mapped[int] = mapped_column(Integer, default=5)
    send_interval: Mapped[float] = mapped_column(Float, default=3.0)
    dedup: Mapped[bool] = mapped_column(Boolean, default=True)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)
    options: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    last_run_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    last_status: Mapped[str] = mapped_column(String(32), default="never")
    last_note: Mapped[str] = mapped_column(String(255), default="")  # 例如"不在活跃时段"
    next_run_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    total_sent: Mapped[int] = mapped_column(Integer, default=0)
    total_runs: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    targets: Mapped[list["WorkflowTarget"]] = relationship(
        back_populates="workflow", cascade="all, delete-orphan", order_by="WorkflowTarget.position"
    )


class WorkflowTarget(Base):
    """工作流 -> 频道 的挂载关系（一个频道可被多个工作流挂载）。"""

    __tablename__ = "workflow_targets"
    __table_args__ = (UniqueConstraint("workflow_id", "channel_id", name="uq_wf_channel"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    workflow_id: Mapped[int] = mapped_column(ForeignKey("workflows.id", ondelete="CASCADE"))
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    overrides: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    workflow: Mapped[Workflow] = relationship(back_populates="targets")
    channel: Mapped[Channel] = relationship(back_populates="targets")


class RunLog(Base):
    """一次工作流运行的结果摘要。"""

    __tablename__ = "run_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    workflow_id: Mapped[int] = mapped_column(ForeignKey("workflows.id", ondelete="CASCADE"), index=True)
    # 图模式下一次触发对应一条记录，用它去重更新；线性模式留空
    execution_id: Mapped[str] = mapped_column(String(40), default="")
    started_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="running")  # running/ok/partial/error
    trigger: Mapped[str] = mapped_column(String(16), default="schedule")  # schedule/manual/test
    fetched: Mapped[int] = mapped_column(Integer, default=0)
    kept: Mapped[int] = mapped_column(Integer, default=0)
    sent: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    detail: Mapped[list[Any]] = mapped_column(JSON, default=list)


class SeenItem(Base):
    """去重表：记录某工作流已经处理过的条目指纹。"""

    __tablename__ = "seen_items"
    __table_args__ = (UniqueConstraint("workflow_id", "key", name="uq_seen"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    workflow_id: Mapped[int] = mapped_column(ForeignKey("workflows.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class WorkflowState(Base):
    """工作流的游标/自定义持久化状态（插件通过 ctx.state 读写）。"""

    __tablename__ = "workflow_state"
    __table_args__ = (UniqueConstraint("workflow_id", "key", name="uq_state"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    workflow_id: Mapped[int] = mapped_column(ForeignKey("workflows.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(128))
    value: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class PluginRecord(Base):
    """插件启用状态与默认配置（插件本体在文件系统中，这里只存元数据）。"""

    __tablename__ = "plugins"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    default_config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


# ════════════════════════════════════════════════════════════ 图工作流
# 线性工作流的结构写死在 workflows 的列里（一个源、一条过滤链、一个出口）。
# 图模式把结构挪到下面这几张表：workflows 行只剩「名字、账号、总开关」这类
# 工作流级属性，采集/过滤/格式化/输出各是一个节点，调度下沉到 source 节点。


class GraphNode(Base):
    """图里的一个节点。kind 决定它是插件节点还是内建节点。"""

    __tablename__ = "graph_nodes"
    __table_args__ = (UniqueConstraint("workflow_id", "node_id", name="uq_graph_node"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    workflow_id: Mapped[int] = mapped_column(ForeignKey("workflows.id", ondelete="CASCADE"), index=True)
    # 前端生成的稳定标识，连线两端引用它（不用自增 id，这样整图可以离线编辑再一次性提交）
    node_id: Mapped[str] = mapped_column(String(40))
    # source / filter / formatter / output —— 插件节点，plugin 列指向插件名
    # merge / delay / router —— 内建节点，plugin 留空，行为由 config 决定
    kind: Mapped[str] = mapped_column(String(16))
    plugin: Mapped[str] = mapped_column(String(64), default="")
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    title: Mapped[str] = mapped_column(String(128), default="")
    x: Mapped[int] = mapped_column(Integer, default=0)
    y: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # —— 只有 source 节点用：什么时候产生新的输入事件 ——
    # manual（只能手动触发）/ immediate（启用时触发一次）/ interval / cron
    schedule_type: Mapped[str] = mapped_column(String(16), default="manual")
    schedule_value: Mapped[str] = mapped_column(String(128), default="")
    jitter: Mapped[int] = mapped_column(Integer, default=0)

    # 节点级熔断：连续失败到阈值后置为 degraded，到达的消息直接停住不再空烧
    status: Mapped[str] = mapped_column(String(16), default="ok")  # ok / degraded / paused
    status_note: Mapped[str] = mapped_column(String(255), default="")

    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class GraphEdge(Base):
    """一条连线。同一对端口只允许连一次。"""

    __tablename__ = "graph_edges"
    __table_args__ = (
        UniqueConstraint("workflow_id", "src_node", "src_port", "dst_node", "dst_port",
                         name="uq_graph_edge"),
        Index("ix_graph_edge_src", "workflow_id", "src_node"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    workflow_id: Mapped[int] = mapped_column(ForeignKey("workflows.id", ondelete="CASCADE"), index=True)
    src_node: Mapped[str] = mapped_column(String(40))
    src_port: Mapped[str] = mapped_column(String(32), default="out")
    dst_node: Mapped[str] = mapped_column(String(40))
    dst_port: Mapped[str] = mapped_column(String(32), default="in")
    # 多路入边的先后（merge 按它取输入）。整图保存是删光重插，靠自增 id 排序
    # 会随前端提交顺序漂移，所以显式存一列。
    position: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class GraphMessage(Base):
    """队列里的一条消息 = 一次"某节点收到输入"的事件。

    这张表就是队列本身：延迟是把 visible_at 放到未来，循环是重新插一行而不是
    函数重入，进程被强杀后靠 status='running' 的残留行恢复，不依赖优雅关停。

    出队必须写成子查询形态——LIMIT/ORDER BY 不能直接挂在 UPDATE 上（本机的
    SQLite 没编译 SQLITE_ENABLE_UPDATE_DELETE_LIMIT，实测直接语法错误）：

        UPDATE graph_messages SET status='running'
        WHERE id IN (SELECT id FROM graph_messages
                     WHERE status='pending' AND visible_at<=:now
                     ORDER BY visible_at LIMIT :n)
        RETURNING ...
    """

    __tablename__ = "graph_messages"
    __table_args__ = (
        UniqueConstraint("message_id", name="uq_graph_message"),
        # 出队扫描走这条：先按状态收窄，再按可见时间排序
        Index("ix_graph_msg_queue", "status", "visible_at"),
        Index("ix_graph_msg_origin", "workflow_id", "origin_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    message_id: Mapped[str] = mapped_column(String(40))
    workflow_id: Mapped[int] = mapped_column(ForeignKey("workflows.id", ondelete="CASCADE"), index=True)
    # 一次触发引发的整片传播共享一个 execution_id，用于在画布上按批回放
    execution_id: Mapped[str] = mapped_column(String(40), default="")
    # 因果链根：源头那条消息的 id。忙循环检测与游标结算都按它归集。
    # merge 节点会新开自己的 origin_id，并把上游的记进 parent_origins。
    origin_id: Mapped[str] = mapped_column(String(40), default="")
    parent_origins: Mapped[list[Any]] = mapped_column(MutableList.as_mutable(JSON), default=list)

    node_id: Mapped[str] = mapped_column(String(40))          # 这条消息要交给哪个节点
    src_node_id: Mapped[str] = mapped_column(String(40), default="")

    # pending 待处理 / running 处理中 / done 已消费 / failed 失败 / parked 被暂停（熔断或忙循环）
    status: Mapped[str] = mapped_column(String(16), default="pending")
    visible_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    payload: Mapped[dict[str, Any]] = mapped_column(MutableDict.as_mutable(JSON), default=dict)   # Item.to_wire()
    meta: Mapped[dict[str, Any]] = mapped_column(MutableDict.as_mutable(JSON), default=dict)
    hop: Mapped[int] = mapped_column(Integer, default=0)
    path: Mapped[list[Any]] = mapped_column(MutableList.as_mutable(JSON), default=list)           # 走过的 node_id，用于判真环
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")

    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class GraphNodeState(Base):
    """节点级 KV。

    线性模式下游标/去重都是 workflow 级（WorkflowState / SeenItem），图里两个
    tg_channel 源节点会抢同一行，必须按节点隔离。merge 的等待桶也存这里。
    """

    __tablename__ = "graph_node_state"
    __table_args__ = (UniqueConstraint("workflow_id", "node_id", "key", name="uq_graph_node_state"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    workflow_id: Mapped[int] = mapped_column(ForeignKey("workflows.id", ondelete="CASCADE"), index=True)
    node_id: Mapped[str] = mapped_column(String(40))
    key: Mapped[str] = mapped_column(String(128))
    value: Mapped[dict[str, Any]] = mapped_column(MutableDict.as_mutable(JSON), default=dict)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class GraphMergeBuffer(Base):
    """合流节点的等待桶。

    为什么不塞进 graph_node_state 的一行 JSON：那是一次跨 await 的读-改-写，
    两路消息几乎同时到达时后写的会把先写的整个覆盖掉，而且无异常无日志；
    wait-all 语义下丢一路 = 这个桶永远凑不齐、分支永久挂着。另外桶里装的是
    完整 Item，每来一路整行重写一次，N 路的写入量是 O(N²)。
    """

    __tablename__ = "graph_merge_buffer"
    __table_args__ = (
        Index("ix_graph_merge_bucket", "workflow_id", "node_id", "bucket_key"),
        # 入桶和"结掉这条消息"不在同一个事务里，中间被强杀会重跑一次。
        # 没有这条约束的话同一条消息会在桶里存两份：concat 正文重复一段、
        # wait_all 重复放行一条，频道里出现重复贴文。
        UniqueConstraint("workflow_id", "node_id", "bucket_key", "message_id",
                         name="uq_graph_merge_msg"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    workflow_id: Mapped[int] = mapped_column(ForeignKey("workflows.id", ondelete="CASCADE"), index=True)
    node_id: Mapped[str] = mapped_column(String(40))
    bucket_key: Mapped[str] = mapped_column(String(255))
    # 这一路是从哪条入边来的：wait-all 靠它判断"齐了没有"
    src_node: Mapped[str] = mapped_column(String(40), default="")
    message_id: Mapped[str] = mapped_column(String(40), default="")
    execution_id: Mapped[str] = mapped_column(String(40), default="")
    origin_id: Mapped[str] = mapped_column(String(40), default="")
    payload: Mapped[dict[str, Any]] = mapped_column(MutableDict.as_mutable(JSON), default=dict)
    meta: Mapped[dict[str, Any]] = mapped_column(MutableDict.as_mutable(JSON), default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class GraphBranchPause(Base):
    """被暂停的分支。

    规格要求忙循环跳闸时"pause branch + 标记 runaway_cycle + 记录诊断"，
    并且明确不许杀掉整个 workflow。分支在这里的定义是"同一条因果链"
    （origin_id）：暂停之后，属于这条链的消息一到就挂起，别的数据照常流。
    """

    __tablename__ = "graph_branch_pause"
    __table_args__ = (
        UniqueConstraint("workflow_id", "origin_id", name="uq_graph_branch_pause"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    workflow_id: Mapped[int] = mapped_column(ForeignKey("workflows.id", ondelete="CASCADE"), index=True)
    origin_id: Mapped[str] = mapped_column(String(40))
    node_id: Mapped[str] = mapped_column(String(40), default="")   # 在哪个节点上跳的闸
    reason: Mapped[str] = mapped_column(String(32), default="runaway_cycle")
    note: Mapped[str] = mapped_column(Text, default="")
    detail: Mapped[dict[str, Any]] = mapped_column(MutableDict.as_mutable(JSON), default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class GraphNodeRun(Base):
    """逐跳执行记录：画布上的实时状态和错误就是查它。

    对应线性模式的 RunLog，但粒度是「一个节点处理一条消息」而不是「一轮运行」。
    """

    __tablename__ = "graph_node_runs"
    __table_args__ = (Index("ix_graph_run_tail", "workflow_id", "id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    workflow_id: Mapped[int] = mapped_column(ForeignKey("workflows.id", ondelete="CASCADE"), index=True)
    node_id: Mapped[str] = mapped_column(String(40))
    message_id: Mapped[str] = mapped_column(String(40), default="")
    origin_id: Mapped[str] = mapped_column(String(40), default="")
    execution_id: Mapped[str] = mapped_column(String(40), default="")
    started_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    # ok 产出了下游消息 / drop 正常终止（被过滤等）/ error 失败 / park 被暂停
    status: Mapped[str] = mapped_column(String(16), default="ok")
    produced: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    detail: Mapped[dict[str, Any]] = mapped_column(MutableDict.as_mutable(JSON), default=dict)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
