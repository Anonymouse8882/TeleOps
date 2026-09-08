"""自检用插件（文件名以 _ 开头，默认不加载；测试时临时改名启用）。

包含一个假数据源和一个空转输出端，用于在没有 Telegram 账号的情况下
验证调度、时间闸门、每日限额等逻辑。
"""
from teleops.core import Item, SinkPlugin, SourcePlugin, field


class ProbeSource(SourcePlugin):
    name = "probe"
    display_name = "自检数据源"
    version = "0.0.1"
    description = "产出几条假数据，用于验证管线是否连通。"

    config_schema = [field("count", "条数", "int", default=3)]

    async def fetch(self, ctx):
        seq = int(await ctx.state.get("seq", 0) or 0)
        await ctx.state.set("seq", seq + 1)
        n = int(self.get("count", 3))
        return [
            Item(uid=f"probe:{seq}:{i}", text=f"第 {seq} 轮 第 {i} 条", url=f"https://example.com/{seq}/{i}")
            for i in range(1, n + 1)
        ]


class NullSink(SinkPlugin):
    name = "null_sink"
    display_name = "空转输出（自检用）"
    version = "0.0.1"
    description = "只记日志不真发，用于在没有账号时验证调度与限额。"
    requires_account = False

    config_schema = []

    async def send(self, item: Item, target, ctx):
        ctx.log.info("[空转] -> %s：%s", target.title, item.preview(60))
        return {"message_id": None, "null": True}
