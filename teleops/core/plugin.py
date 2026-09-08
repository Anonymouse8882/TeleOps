"""插件基类与配置字段声明。

写一个插件 = 在 plugins/<类型>/ 下放一个 .py，里面定义一个继承自
SourcePlugin / FilterPlugin / FormatterPlugin / SinkPlugin 的类。
注册表会自动发现它，支持热重载，不需要改动主程序任何代码。

    from teleops.core import SourcePlugin, Item, field

    class MySource(SourcePlugin):
        name = "my_source"
        display_name = "我的采集器"
        config_schema = [field("url", "地址", "string", required=True)]

        async def fetch(self, ctx):
            html = await ctx.http_get(self.config["url"])
            return [Item(uid=..., text=...)]
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Sequence

from .item import Item

if TYPE_CHECKING:  # 避免循环导入
    from .context import RunContext

FieldType = str  # string|text|int|float|bool|select|multiselect|json|channel|account|password


def field(
    name: str,
    label: str,
    type: FieldType = "string",
    *,
    default: Any = None,
    required: bool = False,
    help: str = "",
    options: Sequence[Any] | None = None,
    placeholder: str = "",
    group: str = "",
) -> dict[str, Any]:
    """声明一个配置项，后台会据此自动生成表单。

    options 支持 ["a", "b"] 或 [{"value": "a", "label": "甲"}]。
    """
    opts: list[dict[str, Any]] = []
    for o in options or []:
        opts.append(o if isinstance(o, dict) else {"value": o, "label": str(o)})
    return {
        "name": name,
        "label": label,
        "type": type,
        "default": default,
        "required": required,
        "help": help,
        "options": opts,
        "placeholder": placeholder,
        "group": group,
    }


@dataclass
class PluginMeta:
    name: str
    display_name: str
    plugin_type: str
    version: str
    author: str
    description: str
    config_schema: list[dict[str, Any]]
    module: str
    file: str
    builtin: bool = False


class Plugin:
    """所有插件的公共基类。"""

    # —— 元信息（子类覆盖）——
    name: str = ""
    display_name: str = ""
    version: str = "1.0.0"
    author: str = ""
    description: str = ""
    plugin_type: str = ""          # source / filter / formatter / sink
    config_schema: list[dict[str, Any]] = []
    # 是否需要一个 Telegram 账号才能工作
    requires_account: bool = False

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = self.merge_defaults(config or {})
        self.log = logging.getLogger(f"plugin.{self.name or self.__class__.__name__}")

    # —— 生命周期（可选覆盖）——
    async def setup(self, ctx: "RunContext") -> None:
        """每次运行前调用。"""

    async def teardown(self, ctx: "RunContext") -> None:
        """每次运行后调用（无论成功失败）。"""

    # —— 工具 ——
    @classmethod
    def merge_defaults(cls, config: dict[str, Any]) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        for f in cls.config_schema:
            merged[f["name"]] = f.get("default")
        merged.update({k: v for k, v in (config or {}).items()})
        return merged

    @classmethod
    def validate_config(cls, config: dict[str, Any]) -> list[str]:
        """返回错误信息列表，空列表表示通过。"""
        errors: list[str] = []
        for f in cls.config_schema:
            if not f.get("required"):
                continue
            v = config.get(f["name"], f.get("default"))
            if v is None or (isinstance(v, str) and not v.strip()) or v == []:
                errors.append(f"缺少必填项：{f['label']}（{f['name']}）")
        return errors

    def get(self, key: str, default: Any = None) -> Any:
        v = self.config.get(key, default)
        return default if v is None else v

    @classmethod
    def meta(cls, module: str = "", file: str = "", builtin: bool = False) -> PluginMeta:
        return PluginMeta(
            name=cls.name,
            display_name=cls.display_name or cls.name,
            plugin_type=cls.plugin_type,
            version=cls.version,
            author=cls.author,
            description=(cls.description or (cls.__doc__ or "")).strip(),
            config_schema=list(cls.config_schema),
            module=module,
            file=file,
            builtin=builtin,
        )


class SourcePlugin(Plugin):
    """信息源：从外部世界拉取内容。"""

    plugin_type = "source"

    async def fetch(self, ctx: "RunContext") -> list[Item]:
        raise NotImplementedError

    async def probe(self, ctx: "RunContext") -> dict[str, Any]:
        """连通性自检，后台"测试"按钮会调用。"""
        items = await self.fetch(ctx)
        return {"ok": True, "count": len(items), "sample": [i.preview() for i in items[:3]]}


class FilterPlugin(Plugin):
    """过滤规则：决定哪些条目继续往下走。"""

    plugin_type = "filter"

    async def apply(self, items: list[Item], ctx: "RunContext") -> list[Item]:
        """默认逐条调用 keep()；需要跨条目判断（如批内去重）时覆盖本方法。"""
        out: list[Item] = []
        for it in items:
            try:
                if await self.keep(it, ctx):
                    out.append(it)
                else:
                    ctx.trace(it, f"被 {self.name} 过滤")
            except Exception as e:
                # 算不出结果时绝不能放行：对黑名单来说，放行等于把用户
                # 明确要拦的内容发出去。宁可让这一轮报错停下来。
                self.log.error("过滤器 %s 处理 %s 出错：%s", self.name, it.uid, e)
                raise
        return out

    async def keep(self, item: Item, ctx: "RunContext") -> bool:
        return True


class FormatterPlugin(Plugin):
    """格式化：改写正文、加尾巴、加按钮、处理媒体等。"""

    plugin_type = "formatter"

    async def format(self, item: Item, ctx: "RunContext") -> Item:
        return item


class SinkPlugin(Plugin):
    """输出端：把 Item 投递到目标（频道/群/Webhook…）。"""

    plugin_type = "sink"
    requires_account = True

    async def send(self, item: Item, target: "Target", ctx: "RunContext") -> dict[str, Any]:
        raise NotImplementedError


@dataclass
class Target:
    """一个输出目标。"""

    channel_id: int
    title: str
    peer: str
    account_id: int | None = None
    overrides: dict[str, Any] | None = None

    def opt(self, key: str, default: Any = None) -> Any:
        return (self.overrides or {}).get(key, default)
