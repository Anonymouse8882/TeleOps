"""把线性工作流转成图。

严格照着 runner.py 的执行顺序转，保证转出来的图跑起来和今天逐条一致：

    fetch → 去重 → 过滤链 → 限量 → 格式化链 → 输出

去重和限量在老代码里是 workflow 上的两个字段，图里必须变成显式节点，
不然「限量在过滤之后」这个位置信息就丢了——挂到 source 的 fetch 上会让
产出条数悄悄变化（抓 5 条截成 1 条再去重，可能一条都发不出去）。
"""
from __future__ import annotations

import logging
from typing import Any

from ...db.models import Workflow

log = logging.getLogger(__name__)

X_STEP = 240
Y_BASE = 120


def linear_to_graph(wf: Workflow, targets: list[dict[str, Any]],
                    settings: Any = None) -> tuple[list[dict], list[dict]]:
    """返回 (nodes, edges)，都是可以直接塞进 GraphNode/GraphEdge 的字典。

    注意：max_items_per_run 和 send_interval 的 0 在 runner 里是「用全局默认值」，
    不是「不限量 / 不间隔」。直接照抄 0 会把限流兜底弄丢——正好是防 Telegram
    封号的那个字段。

    以下几列**不搬进节点**，继续留在 workflows 行上，由调度器和输出节点执行前读取：
    active_hours / active_days / timezone（时段闸门）、daily_limit（日配额）、
    dry_run（试运行开关）、account_id（默认账号）、enabled（总开关）。
    单机单用户、每天上百条的量级，为它们各造一个节点不划算。
    """
    rt = getattr(settings, "runtime", None)
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    seq = 0
    prev: str | None = None

    def add(kind: str, *, plugin: str = "", config: dict | None = None, title: str = "") -> str:
        nonlocal seq, prev
        seq += 1
        nid = f"n{seq}"
        nodes.append({
            "node_id": nid, "kind": kind, "plugin": plugin, "config": config or {},
            "title": title, "x": (seq - 1) * X_STEP, "y": Y_BASE, "enabled": True,
        })
        if prev is not None:
            edges.append({"src_node": prev, "src_port": "out", "dst_node": nid,
                          "dst_port": "in", "enabled": True})
        prev = nid
        return nid

    # ① 信息源：调度从工作流下沉到节点
    add("source", plugin=wf.source_plugin, config=dict(wf.source_config or {}),
        title=wf.source_plugin)
    nodes[0]["schedule_type"] = wf.schedule_type or "interval"
    nodes[0]["schedule_value"] = wf.schedule_value or "600"
    nodes[0]["jitter"] = int(wf.jitter or 0)

    # ② 去重（老代码里在过滤之前）
    if wf.dedup:
        add("dedup", config={"key": "uid", "scope": "workflow"}, title="去重")

    # ③ 过滤链
    for step in (wf.filters or []):
        if not step.get("enabled", True):
            continue
        add("filter", plugin=step["plugin"], config=dict(step.get("config") or {}),
            title=step["plugin"])

    # ④ 限量（老代码里在过滤之后、格式化之前）
    cap = int(wf.max_items_per_run or 0) or int(getattr(rt, "max_items_per_run", 0) or 0)
    if cap > 0:
        # 标题里不写数字：用户改了配置之后标题就对不上了，
        # 画布上的副标题会显示当前的实际条数
        add("limit", config={"count": cap}, title="限量")

    # ⑤ 格式化链
    for step in (wf.formatters or []):
        if not step.get("enabled", True):
            continue
        add("formatter", plugin=step["plugin"], config=dict(step.get("config") or {}),
            title=step["plugin"])

    # ⑥ 输出：频道列表放在节点配置里，与现有 Target/overrides 契约一致
    add("output", plugin=wf.sink_plugin or "tg_send",
        config={**dict(wf.sink_config or {}), "targets": targets,
                "send_interval": float(wf.send_interval or getattr(rt, "send_interval", 0) or 0)},
        title=f"发布到 {len(targets)} 个频道" if targets else "输出")

    kept = {k: getattr(wf, k, None) for k in
            ("active_hours", "active_days", "daily_limit", "timezone", "dry_run", "account_id")}
    log.info("迁移工作流 %s：%d 个节点；以下设置仍留在工作流行上生效 %s", wf.id, len(nodes), kept)
    return nodes, edges
