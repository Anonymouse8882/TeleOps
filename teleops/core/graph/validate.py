"""保存整图时的校验。

只拦真正跑不起来的结构，不做"善意"的限制——尤其是**有环必须放行**：
规格明确要求允许 A→B→C→A 这样的永久循环，环不是错误，它只是意味着
消息会重新入队，而不是函数递归。
"""
from __future__ import annotations

from typing import Any

from .kinds import ALL_KINDS, BUILTIN_KINDS, PLUGIN_KINDS, in_ports, out_ports


class GraphError(ValueError):
    """图结构不合法，消息直接给用户看。"""


def validate_graph(nodes: list[dict[str, Any]], edges: list[dict[str, Any]],
                   registry: Any = None) -> None:
    ids: set[str] = set()
    by_id: dict[str, dict[str, Any]] = {}
    for n in nodes:
        nid = (n.get("node_id") or "").strip()
        if not nid:
            raise GraphError("有节点缺少 node_id")
        if nid in ids:
            raise GraphError(f"节点 id 重复：{nid}")
        kind = n.get("kind")
        if kind not in ALL_KINDS:
            raise GraphError(f"未知的节点类型：{kind}")
        _check_config(n, kind, registry)
        if kind == "source":
            _check_schedule(n)
        if kind == "merge":
            _check_merge(n)
        if kind == "router":
            _check_router(n)
        ids.add(nid)
        by_id[nid] = n

    seen_edges: set[tuple] = set()
    for e in edges:
        src, dst = e.get("src_node"), e.get("dst_node")
        if src not in ids:
            raise GraphError(f"连线的起点节点不存在：{src}")
        if dst not in ids:
            raise GraphError(f"连线的终点节点不存在：{dst}")
        sport = e.get("src_port") or "out"
        dport = e.get("dst_port") or "in"
        # 输出是终止节点：可以有多个上游，但不能有下游
        if by_id[src]["kind"] == "output":
            raise GraphError(f"「{_name(by_id[src])}」是输出节点，不能再往下连")
        valid = {p["name"] for p in out_ports(by_id[src]["kind"])}
        if sport not in valid:
            raise GraphError(f"「{_name(by_id[src])}」没有名为 {sport} 的出口")
        if dport not in {p["name"] for p in in_ports(by_id[dst]["kind"])}:
            raise GraphError(f"「{_name(by_id[dst])}」没有名为 {dport} 的入口")
        key = (src, sport, dst, dport)
        if key in seen_edges:
            raise GraphError(f"「{_name(by_id[src])}」到「{_name(by_id[dst])}」重复连线")
        seen_edges.add(key)

    # 环是合法的，这里只提醒一件事：一条消息都产不出来的图（没有信息源）
    if nodes and not any(n["kind"] == "source" for n in nodes):
        raise GraphError("至少要有一个信息源节点，否则这张图永远不会被触发")


def _check_config(n: dict[str, Any], kind: str, registry: Any) -> None:
    """节点配置要和线性编辑器一样严。

    以前这里只查「插件名字在不在」，于是 kind=source 配一个 sink 插件、或者配一个
    已禁用的插件都能存进去，等到运行时才抛——那时错误出现在队列消费者里，
    画布上只是一个红框，用户看不出是保存时就该拦的事。
    """
    if kind in BUILTIN_KINDS:
        _validate_against(BUILTIN_KINDS[kind]["config_schema"], n.get("config") or {},
                          f"{ALL_KINDS[kind]['label']}节点")
        return
    plugin = (n.get("plugin") or "").strip()
    if not plugin:
        raise GraphError(f"{ALL_KINDS[kind]['label']}节点没有选插件")
    if registry is None:
        return
    lp = registry.get(plugin)
    if lp is None:
        raise GraphError(f"插件不存在或未加载：{plugin}")
    if not lp.enabled:
        raise GraphError(f"插件「{plugin}」已被禁用，请先在插件页启用，或换一个插件")
    want = PLUGIN_KINDS[kind]["plugin_type"]
    if lp.meta.plugin_type != want:
        raise GraphError(
            f"「{plugin}」是{lp.meta.plugin_type}插件，不能放在{ALL_KINDS[kind]['label']}节点上")
    _validate_against(lp.cls.config_schema, lp.cls.merge_defaults(n.get("config") or {}),
                      f"「{_name(n)}」")


def _validate_against(schema: list[dict[str, Any]], config: dict[str, Any], who: str) -> None:
    """按 config_schema 查必填项与数值类型。

    内建节点没有插件类，所以不能直接用 Plugin.validate_config；这里照它的规则
    重写一遍（判空的口径要保持一致：None、空串、空列表都算没填）。
    """
    for f in schema:
        if f.get("required"):
            v = config.get(f["name"], f.get("default"))
            if v is None or (isinstance(v, str) and not v.strip()) or v == []:
                raise GraphError(f"{who}：缺少必填项：{f['label']}（{f['name']}）")
        if f.get("type") not in ("int", "float"):
            continue
        v = config.get(f["name"])
        if v in (None, ""):
            continue
        try:
            num = float(v)
        except (TypeError, ValueError):
            raise GraphError(f"{who}：{f['label']} 要填数字，现在是 {v!r}") from None
        if f["name"] == "count" and num < 1:
            raise GraphError(f"{who}：{f['label']} 至少是 1")


def _check_merge(n: dict[str, Any]) -> None:
    """等待窗口必须有限。

    上游分支失败时不会产出消息（这是分支隔离的正确表现），一个永不超时的
    wait-all 桶就是静默的消息坟场：内容进去了，什么都没发生，日志里也没有。
    """
    cfg = n.get("config") or {}
    if str(cfg.get("strategy") or "append") == "append":
        return
    raw = str(cfg.get("timeout") or "").strip()
    if not raw or raw in ("0", "0s"):
        raise GraphError(f"「{_name(n)}」的最长等待不能留空或填 0——"
                         "上游一旦失败，这里会永远挂着")
    from ..scheduler import parse_interval_spec
    try:
        base, _ = parse_interval_spec(raw)
    except Exception as e:
        raise GraphError(f"「{_name(n)}」的最长等待写错了：{e}") from e
    if base < 5:
        raise GraphError(f"「{_name(n)}」的最长等待至少 5 秒")
    if base > 86400:
        raise GraphError(f"「{_name(n)}」的最长等待最多 1 天")


def _check_router(n: dict[str, Any]) -> None:
    from .expr import ExprError, check
    try:
        check(str((n.get("config") or {}).get("expr") or ""))
    except ExprError as e:
        raise GraphError(f"「{_name(n)}」的分流条件有问题：{e}") from e


def _check_schedule(n: dict[str, Any]) -> None:
    """信息源的调度表达式，和线性编辑器用同一套解析，别写第二份。"""
    stype = (n.get("schedule_type") or "manual").lower()
    value = (n.get("schedule_value") or "").strip()
    if stype in ("manual", "immediate"):
        return
    if stype == "cron":
        from apscheduler.triggers.cron import CronTrigger
        if not value:
            raise GraphError(f"「{_name(n)}」选了 cron 但没填表达式")
        try:
            CronTrigger.from_crontab(value)
        except Exception as e:
            raise GraphError(f"「{_name(n)}」的 cron 表达式不合法：{e}") from e
        return
    if stype == "interval":
        from ..scheduler import parse_interval_spec
        try:
            base, span = parse_interval_spec(value)
        except Exception as e:
            raise GraphError(f"「{_name(n)}」的间隔不合法：{e}") from e
        if base - span < 10:
            raise GraphError(f"「{_name(n)}」的采集间隔不能小于 10 秒")
        return
    raise GraphError(f"「{_name(n)}」的触发方式不认识：{stype}")


def _name(node: dict[str, Any]) -> str:
    return node.get("title") or node.get("plugin") or node.get("node_id") or "?"
