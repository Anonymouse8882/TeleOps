"""模板格式化：用占位符重排正文，加页眉页脚、标签、按钮。

可用占位符：
    {text} {title} {url} {author} {source} {date} {time} {datetime} {tags}
"""
from __future__ import annotations

import datetime as dt
import re

from teleops.core import FormatterPlugin, Item, field


class TemplateFormatter(FormatterPlugin):
    name = "template"
    display_name = "模板排版"
    version = "1.0.0"
    author = "TeleOps"
    description = "用模板重排正文，支持页眉/页脚/标签/来源链接与阅读原文按钮。"

    config_schema = [
        field("template", "正文模板", "text",
              default="{text}",
              help="可用占位符：{text} {title} {url} {author} {source} {date} {time} {datetime} {tags}",
              group="正文"),
        field("header", "页眉", "text", default="", group="正文"),
        field("footer", "页脚", "text", default="", group="正文",
              placeholder="例如：\n\n📢 <a href=\"https://t.me/yourchannel\">订阅本频道</a>"),
        field("tags", "追加标签", "string", default="",
              help="空格或逗号分隔，会自动加 #。", group="正文"),
        field("parse_mode", "解析模式", "select", default="keep",
              options=[
                  {"value": "keep", "label": "沿用来源"},
                  {"value": "html", "label": "HTML"},
                  {"value": "md", "label": "Markdown"},
                  {"value": "none", "label": "纯文本"},
              ], group="正文"),
        field("button_text", "底部按钮文字", "string", default="", group="按钮"),
        field("button_url", "底部按钮链接", "string", default="",
              help="留空则用原文链接 {url}。", group="按钮"),
        field("skip_if_empty", "正文为空则丢弃", "bool", default=False, group="其他"),
    ]

    async def format(self, item: Item, ctx) -> Item:
        now = dt.datetime.now()
        published = item.published_at or now
        tags_cfg = (self.get("tags", "") or "").replace(",", " ").split()
        all_tags = [t if t.startswith("#") else f"#{t}" for t in (item.tags + tags_cfg) if t]

        values = {
            "text": item.text or "",
            "title": item.title or "",
            "url": item.url or "",
            "author": item.author or "",
            "source": item.source_name or "",
            "date": published.strftime("%Y-%m-%d"),
            "time": published.strftime("%H:%M"),
            "datetime": published.strftime("%Y-%m-%d %H:%M"),
            "tags": " ".join(all_tags),
        }

        tpl = self.get("template", "{text}") or "{text}"
        header_tpl = self.get("header", "") or ""
        footer_tpl = self.get("footer", "") or ""

        parts = [
            _safe_format(header_tpl, values),
            _safe_format(tpl, values),
            _safe_format(footer_tpl, values),
        ]
        # 模板里没写 {tags} 的话，"追加标签"填了也不会出现在正文里，这里补到末尾
        if all_tags and "{tags}" not in (tpl + header_tpl + footer_tpl):
            parts.append(values["tags"])
        item.text = _stitch(parts)

        if self.get("skip_if_empty", False) and not item.text and not item.has_media:
            return item.drop("模板渲染后为空")

        mode = self.get("parse_mode", "keep")
        if mode != "keep":
            item.parse_mode = None if mode == "none" else mode

        item.tags = all_tags

        btn_text = self.get("button_text", "")
        btn_url = self.get("button_url", "") or item.url
        if btn_text and btn_url:
            item.buttons = item.buttons + [[{"text": btn_text, "url": _safe_format(btn_url, values)}]]

        return item


def _stitch(parts: list[str]) -> str:
    """把页眉/正文/页脚/标签接起来，段与段之间隔一个空行。

    用户自己在页脚开头写了换行就照他的来，不再叠加。
    """
    out = ""
    for part in parts:
        if not part:
            continue
        if not out:
            out = part
        elif out.endswith("\n") or part.startswith("\n"):
            out += part
        else:
            out += "\n\n" + part
    return out.strip()


def _safe_format(tpl: str, values: dict[str, str]) -> str:
    """占位符替换；未知占位符原样保留，不抛异常。

    一次扫描完成——按 key 逐个 replace 的话，先替进来的正文里要是恰好带
    "{author}" 这种字样，会被后面几轮当成占位符二次替换掉。
    """
    pattern = re.compile(r"\{(" + "|".join(re.escape(k) for k in values) + r")\}")
    return pattern.sub(lambda m: str(values[m.group(1)]), tpl)
