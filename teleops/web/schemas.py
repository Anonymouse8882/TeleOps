"""API 请求/响应模型。"""
from __future__ import annotations

import datetime as dt
from typing import Any

from pydantic import BaseModel, Field


# ------------------------------------------------------------------ 账号
class AccountIn(BaseModel):
    name: str
    # api_id / api_hash 统一在「设置」里配，这里保留只为兼容老的调用方
    api_id: int = 0
    api_hash: str = ""
    phone: str = ""
    is_bot: bool = False
    bot_token: str = ""
    proxy: str = ""
    enabled: bool = True


class AccountPatch(BaseModel):
    name: str | None = None
    api_id: int | None = None
    api_hash: str | None = None
    phone: str | None = None
    is_bot: bool | None = None
    bot_token: str | None = None
    proxy: str | None = None
    enabled: bool | None = None


class TelegramApiIn(BaseModel):
    api_id: int
    api_hash: str = ""      # 留空表示沿用原值


class CodeWatchIn(BaseModel):
    seconds: int = 180


class CodeIn(BaseModel):
    code: str


class PasswordIn(BaseModel):
    password: str


# ------------------------------------------------------------------ 频道
class ChannelIn(BaseModel):
    title: str
    peer: str
    account_id: int | None = None
    kind: str = "channel"
    note: str = ""
    enabled: bool = True


class ChannelPatch(BaseModel):
    title: str | None = None
    peer: str | None = None
    account_id: int | None = None
    kind: str | None = None
    note: str | None = None
    enabled: bool | None = None


# ------------------------------------------------------------------ 工作流
class PipelineStep(BaseModel):
    plugin: str
    config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class TargetIn(BaseModel):
    channel_id: int
    enabled: bool = True
    overrides: dict[str, Any] = Field(default_factory=dict)


class WorkflowIn(BaseModel):
    name: str
    description: str = ""
    enabled: bool = False
    # 采集/过滤/格式化/输出都由画布上的节点定义，建工作流时不用填。
    # 这几个字段只在把老库里的线性配置转成图时还会被读到。
    source_plugin: str = ""
    source_config: dict[str, Any] = Field(default_factory=dict)
    filters: list[PipelineStep] = Field(default_factory=list)
    formatters: list[PipelineStep] = Field(default_factory=list)
    sink_plugin: str = "tg_send"
    sink_config: dict[str, Any] = Field(default_factory=dict)
    schedule_type: str = "interval"
    schedule_value: str = "600"
    jitter: int = 0
    active_hours: str = ""
    active_days: list[int] = Field(default_factory=list)
    daily_limit: int = 0
    timezone: str = ""
    account_id: int | None = None
    max_items_per_run: int = 5
    send_interval: float = 3.0
    dedup: bool = True
    dry_run: bool = False
    options: dict[str, Any] = Field(default_factory=dict)
    targets: list[TargetIn] = Field(default_factory=list)


class WorkflowPatch(WorkflowIn):
    name: str | None = None  # type: ignore[assignment]
    source_plugin: str | None = None  # type: ignore[assignment]
    targets: list[TargetIn] | None = None  # type: ignore[assignment]
    filters: list[PipelineStep] | None = None  # type: ignore[assignment]
    formatters: list[PipelineStep] | None = None  # type: ignore[assignment]


# ------------------------------------------------------------------ 图工作流
class GraphNodeIn(BaseModel):
    node_id: str
    kind: str
    plugin: str = ""
    config: dict[str, Any] = Field(default_factory=dict)
    title: str = ""
    x: int = 0
    y: int = 0
    enabled: bool = True
    # 只有 source 节点用
    schedule_type: str = "manual"
    schedule_value: str = ""
    jitter: int = 0


class GraphEdgeIn(BaseModel):
    src_node: str
    src_port: str = "out"
    dst_node: str
    dst_port: str = "in"
    enabled: bool = True


class GraphIn(BaseModel):
    nodes: list[GraphNodeIn] = Field(default_factory=list)
    edges: list[GraphEdgeIn] = Field(default_factory=list)
    # 画布视口，原样存进 workflows.options 里，下次打开还原
    viewport: dict[str, Any] = Field(default_factory=dict)


class RunIn(BaseModel):
    dry_run: bool = False
    limit: int | None = None


# ------------------------------------------------------------------ 插件
class PluginToggle(BaseModel):
    enabled: bool


class PluginSource(BaseModel):
    code: str


class PluginTestIn(BaseModel):
    plugin: str
    config: dict[str, Any] = Field(default_factory=dict)
    account_id: int | None = None


def dt_str(v: dt.datetime | None) -> str | None:
    return v.isoformat() if v else None
