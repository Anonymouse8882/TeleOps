# 插件目录

把 `.py` 文件丢进对应子目录即可生效，程序会在几秒内自动热加载，**不需要重启**。
以 `_` 开头的文件会被忽略（适合放模板/草稿）。

| 目录 | 类型 | 基类 | 职责 |
| --- | --- | --- | --- |
| `sources/` | 信息源 | `SourcePlugin` | 从外部抓内容，产出 `Item` 列表 |
| `filters/` | 过滤规则 | `FilterPlugin` | 决定哪些 `Item` 继续往下走 |
| `formatters/` | 格式化 | `FormatterPlugin` | 改写正文、加尾巴、加按钮 |
| `sinks/` | 输出端 | `SinkPlugin` | 把 `Item` 投递到目标 |

> 决定插件类型的是**继承的基类**，不是目录；目录只是为了整理。

## 最小示例

```python
from teleops.core import Item, SourcePlugin, field

class HelloSource(SourcePlugin):
    name = "hello"                 # 唯一标识，必填
    display_name = "示例采集器"
    version = "1.0.0"
    description = "演示用"

    # 后台会按这份声明自动生成配置表单
    config_schema = [
        field("keyword", "关键词", "string", required=True),
        field("limit", "条数", "int", default=3),
    ]

    async def fetch(self, ctx):
        data = await ctx.http_json("https://example.com/api?q=" + self.config["keyword"])
        return [
            Item(uid=f"hello:{d['id']}", text=d["title"], url=d["url"])
            for d in data[: self.config["limit"]]
        ]
```

## 配置字段类型

`field(name, label, type, *, default, required, help, options, placeholder, group)`

`type` 可选：`string` `text` `int` `float` `bool` `select` `multiselect` `json`
`password` `channel`（频道选择器）`account`（账号选择器）。

`group` 用于在表单里分组显示。

## 上下文 `ctx` 能做什么

| 用法 | 说明 |
| --- | --- |
| `await ctx.http_get(url)` / `ctx.http_json(url)` | 带 UA、自动跟随跳转的 HTTP 请求 |
| `ctx.http` | 原始 `httpx.AsyncClient` |
| `await ctx.client()` | 当前工作流账号的 Telethon 客户端 |
| `await ctx.client_for(account_id)` | 指定账号的客户端 |
| `await ctx.state.get(k)` / `set(k, v)` | 工作流级持久化状态（游标就存这儿） |
| `ctx.media_dir()` | 本工作流的媒体下载目录 |
| `ctx.register_temp(path)` | 登记临时文件，运行结束自动删 |
| `await ctx.is_seen(key)` / `mark_seen(key)` | 去重记录 |
| `ctx.add_commit_hook(fn)` | 注册"发送成功后"回调，用来安全推进游标 |
| `ctx.log` | 该工作流的 logger |
| `ctx.dry_run` | 当前是否为试运行 |

## 生命周期

```
setup() → fetch()/apply()/format()/send() → commit hooks → teardown()
```

`teardown()` 无论成功失败都会调用；`commit hooks` 只在内容真的发出去之后调用，
所以推进游标请放在 commit hook 里，避免发送失败导致内容丢失。

## 内置插件

- `tg_channel` —— TG 频道搬运（增量 / 从头开始 / 随机，媒体复制或下载）
- `rss` —— RSS/Atom 订阅
- `web_scraper` —— CSS 选择器网页采集
- `keyword` —— 关键词/正则过滤
- `content_rules` —— 媒体、时间、广告、相似度过滤
- `template` —— 模板排版、页眉页脚、按钮
- `clean_text` —— 正文清洗、去水印、正则替换
- `tg_send` —— 发送到 TG 频道
