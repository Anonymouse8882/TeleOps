"""图里有哪些节点类型，以及每种的端口与配置。

分两类：
  * 插件节点（source / filter / formatter / output）—— 行为由插件决定，
    配置表单来自插件自己的 config_schema，这里只定端口。
  * 内建节点（dedup / limit / merge / delay / router）—— 行为由运行时实现，
    配置表单在这里定义。

端口规则来自规格：output 是终止节点，只能有入边；其余节点入边可以有多条
（多输入是"谁先到就处理谁"，不等待、不聚合，只有 merge 负责同步）。
source 允许有入边——规格里的永久循环 A→B→C→A 中 A 就是被下游回灌的。
"""
from __future__ import annotations

from typing import Any

from ..plugin import field

# 插件节点：kind 直接对应插件类型
PLUGIN_KINDS = {
    "source": {"label": "信息源", "plugin_type": "source", "color": "#4b8bf5"},
    "filter": {"label": "过滤", "plugin_type": "filter", "color": "#e0a33e"},
    "formatter": {"label": "格式化", "plugin_type": "formatter", "color": "#43b581"},
    "output": {"label": "输出", "plugin_type": "sink", "color": "#e0555f"},
}

# 内建节点：运行时实现，配置在这里声明
BUILTIN_KINDS: dict[str, dict[str, Any]] = {
    "dedup": {
        "label": "去重",
        "color": "#8f7fe8",
        "description": "已经处理过的内容不再往下走。发送成功后才记账，失败的下次还会重来。",
        "config_schema": [
            field("key", "指纹来源", "select", default="uid",
                  options=[{"value": "uid", "label": "源内唯一标识（推荐）"},
                           {"value": "content", "label": "正文 + 媒体哈希"}],
                  help="uid 形如 tg:@channel:12345；内容哈希适合没有稳定 id 的源"),
            field("scope", "作用范围", "select", default="workflow",
                  options=[{"value": "workflow", "label": "整条工作流共用一份记录"},
                           {"value": "node", "label": "本节点独立记账"}]),
        ],
    },
    "limit": {
        "label": "限量",
        "color": "#8f7fe8",
        "description": "一次触发最多放行几条，多出来的丢弃。放在过滤之后、格式化之前最接近老版行为。",
        "config_schema": [
            field("count", "每次触发最多放行", "int", default=5, required=True),
        ],
    },
    "delay": {
        "label": "延迟",
        "color": "#8f7fe8",
        "description": "收到的内容先压住，等一段时间再继续往下传。与信息源的定时是两回事："
                       "信息源定时决定「什么时候产生新数据」，这里决定「已有的数据什么时候继续走」。",
        "config_schema": [
            field("value", "延迟多久", "string", default="10m", required=True,
                  placeholder="10m / 2h / 600",
                  help="支持 600（秒）、10m、2h、1d；填区间如 5m-15m 表示随机延迟"),
        ],
    },
    "merge": {
        "label": "合流",
        "color": "#8f7fe8",
        "description": "多条分支真正需要汇合时用它。普通节点收到多个上游是「谁先到就处理谁」，"
                       "只有这个节点会等待和聚合。",
        "config_schema": [
            field("strategy", "合流方式", "select", default="append", required=True,
                  options=[{"value": "append", "label": "append —— 到一条放一条，不等待"},
                           {"value": "concat", "label": "concat —— 把正文按到达顺序拼起来"},
                           {"value": "latest", "label": "latest —— 只保留最新的一条"},
                           {"value": "wait_all", "label": "wait-all —— 等齐所有入边再放行"},
                           {"value": "key_based", "label": "key-based —— 按键配对后放行"}]),
            field("key", "配对键", "string", default="",
                  placeholder="留空 / origin / uid / meta.source_chat",
                  help="留空＝所有到达的内容算一组，按到达顺序配对（两个各自定时的源要汇合时用这个）；"
                       "填 origin＝只把同一次采集产出的内容算一组；也可以填字段名或 meta.键名"),
            field("timeout", "最长等待", "string", default="10m",
                  help="wait-all / key-based 专用。等不齐就按下面的方式收尾——"
                       "不允许填 0，否则上游一旦失败，这里会永远挂着"),
            field("on_timeout", "等不齐时", "select", default="emit_partial",
                  options=[{"value": "emit_partial", "label": "把已到的放行"},
                           {"value": "drop", "label": "丢弃并记一条诊断"}]),
        ],
    },
    "router": {
        "label": "分流",
        "color": "#8f7fe8",
        "description": "按条件把内容分到不同出口。命中走「匹配」口，其余走「其它」口。",
        "config_schema": [
            field("expr", "匹配条件", "text", default="", required=True,
                  placeholder='text contains "关键词"',
                  help='字段：text / title / uid / url / author / source_name / tags / meta.键名。'
                       '运算符：contains / not contains / equals / not equals / '
                       'matches / not matches / startswith / endswith。'
                       '多条用 and / or 连接，值要加引号（中文全角引号也行）。'
                       '例：meta.source_chat equals "@abc" and text not contains "广告"'),
        ],
    },
}

ALL_KINDS = {**PLUGIN_KINDS, **BUILTIN_KINDS}

# 多出口的节点在这里声明，其余一律单出口
OUT_PORTS: dict[str, list[dict[str, str]]] = {
    "router": [{"name": "match", "label": "匹配"}, {"name": "else", "label": "其它"}],
}


def out_ports(kind: str) -> list[dict[str, str]]:
    if kind == "output":
        return []                       # 终止节点，不能有下游
    return OUT_PORTS.get(kind) or [{"name": "out", "label": ""}]


def in_ports(kind: str) -> list[dict[str, str]]:
    return [{"name": "in", "label": ""}]


def describe(registry: Any = None) -> dict[str, Any]:
    """给前端的节点面板用：每种节点的标签、端口、配置表单来源。"""
    out: dict[str, Any] = {}
    for kind, meta in ALL_KINDS.items():
        entry = {
            "kind": kind,
            "label": meta["label"],
            "color": meta.get("color", "#888"),
            "description": meta.get("description", ""),
            "builtin": kind in BUILTIN_KINDS,
            "in_ports": in_ports(kind),
            "out_ports": out_ports(kind),
        }
        if kind in BUILTIN_KINDS:
            entry["config_schema"] = meta["config_schema"]
        else:
            entry["plugin_type"] = meta["plugin_type"]
            entry["plugins"] = [
                {"name": p.meta.name, "display_name": p.meta.display_name,
                 "description": p.meta.description, "enabled": p.enabled,
                 "config_schema": p.meta.config_schema}
                for p in (registry.list(meta["plugin_type"]) if registry else [])
            ]
        out[kind] = entry
    return out
