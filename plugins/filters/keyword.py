"""关键词过滤：黑名单 / 白名单 / 正则。"""
from __future__ import annotations

import re

from teleops.core import FilterPlugin, Item, field


class KeywordFilter(FilterPlugin):
    name = "keyword"
    display_name = "关键词过滤"
    version = "1.0.0"
    author = "TeleOps"
    description = "按关键词或正则决定放行/拦截，可作用于正文、标题或链接。"

    config_schema = [
        field("include", "必须包含（白名单）", "text", default="",
              help="每行一个关键词，命中任意一个才放行；留空表示不限制。"),
        field("exclude", "包含则拦截（黑名单）", "text", default="",
              help="每行一个关键词，命中任意一个即丢弃。"),
        field("regex_include", "白名单正则", "string", default=""),
        field("regex_exclude", "黑名单正则", "string", default=""),
        field("case_sensitive", "区分大小写", "bool", default=False),
        field("scope", "匹配范围", "select", default="all",
              options=[
                  {"value": "all", "label": "正文 + 标题 + 链接"},
                  {"value": "text", "label": "仅正文"},
                  {"value": "title", "label": "仅标题"},
              ]),
        field("min_length", "正文最少字数", "int", default=0),
        field("max_length", "正文最多字数", "int", default=0, help="0 表示不限。"),
    ]

    @classmethod
    def validate_config(cls, config: dict) -> list[str]:
        errors = super().validate_config(config)
        for key, label in (("regex_include", "白名单正则"), ("regex_exclude", "黑名单正则")):
            pattern = (config.get(key) or "").strip()
            if not pattern:
                continue
            try:
                re.compile(pattern)
            except re.error as e:
                errors.append(f"{label}写法有误：{e}")
        return errors

    def _haystack(self, item: Item) -> str:
        scope = self.get("scope", "all")
        text = item.meta.get("plain_text") or item.text or ""
        if scope == "text":
            s = text
        elif scope == "title":
            s = item.title
        else:
            s = "\n".join([item.title, text, item.url, " ".join(item.tags)])
        return s if self.get("case_sensitive", False) else s.lower()

    def _words(self, key: str) -> list[str]:
        raw = self.get(key, "") or ""
        words = [w.strip() for w in re.split(r"[\n,，]", raw) if w.strip()]
        return words if self.get("case_sensitive", False) else [w.lower() for w in words]

    async def keep(self, item: Item, ctx) -> bool:
        hay = self._haystack(item)
        plain = item.meta.get("plain_text") or item.text or ""

        min_len = int(self.get("min_length", 0) or 0)
        max_len = int(self.get("max_length", 0) or 0)
        if min_len and len(plain) < min_len:
            return False
        if max_len and len(plain) > max_len:
            return False

        excl = self._words("exclude")
        if excl and any(w in hay for w in excl):
            return False

        incl = self._words("include")
        if incl and not any(w in hay for w in incl):
            return False

        flags = 0 if self.get("case_sensitive", False) else re.IGNORECASE
        rx_ex = self.get("regex_exclude", "")
        if rx_ex and re.search(rx_ex, hay, flags):
            return False
        rx_in = self.get("regex_include", "")
        if rx_in and not re.search(rx_in, hay, flags):
            return False

        return True
