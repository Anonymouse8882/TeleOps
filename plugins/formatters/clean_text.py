"""正文清洗：去水印、去链接、正则替换、表情与空行整理。

搬运场景里最常用的一步——把原频道的推广尾巴、@用户名、邀请链接洗掉。
"""
from __future__ import annotations

import re

from teleops.core import FormatterPlugin, Item, field

_URL_RE = re.compile(r"https?://[^\s<>\"']+")
_TME_RE = re.compile(r"(?:https?://)?(?:t\.me|telegram\.me)/[^\s<>\"']+", re.IGNORECASE)
#: HTML 模式下用来把 <a> 拆成纯文字，保住可见内容
_ANCHOR_RE = re.compile(r"<a\b[^>]*href=[\"']([^\"']*)[\"'][^>]*>(.*?)</a>", re.IGNORECASE | re.DOTALL)
_MENTION_RE = re.compile(r"(?<![\w>])@[A-Za-z0-9_]{4,}")
_HASHTAG_RE = re.compile(r"(?<![\w>])#[^\s#]+")
_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF⬀-⯿]+"
)
_TAG_RE = re.compile(r"<[^>]+>")


class CleanTextFormatter(FormatterPlugin):
    name = "clean_text"
    display_name = "正文清洗"
    version = "1.0.0"
    author = "TeleOps"
    description = "去除原频道的推广链接、@用户名、话题标签和多余空行，支持自定义正则替换。"

    config_schema = [
        field("strip_urls", "删除所有链接", "bool", default=False, group="清理"),
        field("strip_tme_links", "删除 t.me 链接", "bool", default=True, group="清理"),
        field("strip_mentions", "删除 @用户名", "bool", default=True, group="清理"),
        field("strip_hashtags", "删除 #话题标签", "bool", default=False, group="清理"),
        field("strip_emoji", "删除表情符号", "bool", default=False, group="清理"),
        field("strip_html", "清除 HTML 标签（转纯文本）", "bool", default=False, group="清理"),

        field("remove_lines", "整行删除（关键词）", "text", default="",
              help="每行一个关键词，包含该词的整行会被删掉。常用于洗掉固定推广尾巴。",
              group="替换"),
        field("replacements", "替换规则", "text", default="",
              help="每行一条，格式 原文=>新文；以 re: 开头则按正则处理，例如 re:\\d{11}=>[已隐藏]",
              group="替换"),

        field("collapse_blank", "折叠多余空行", "bool", default=True, group="整理"),
        field("trim", "去除首尾空白", "bool", default=True, group="整理"),
        field("max_length", "正文截断长度", "int", default=0, help="0 表示不截断。", group="整理"),
        field("drop_if_empty", "清洗后为空则丢弃", "bool", default=True, group="整理"),
    ]

    async def format(self, item: Item, ctx) -> Item:
        text = item.text or ""

        if self.get("strip_html", False):
            text = _TAG_RE.sub("", text)
            item.parse_mode = None

        is_html = item.parse_mode == "html"
        for key, rx in (
            ("strip_urls", _URL_RE),
            ("strip_tme_links", _TME_RE),
            ("strip_mentions", _MENTION_RE),
            ("strip_hashtags", _HASHTAG_RE),
            ("strip_emoji", _EMOJI_RE),
        ):
            if not self.get(key, False):
                continue
            # HTML 正文里链接藏在 href 里，直接删会连标签带可见文字一起没掉；
            # 先把命中的 <a> 拆成纯文字，再删剩下的裸链接。
            if is_html and rx in (_URL_RE, _TME_RE):
                text = _ANCHOR_RE.sub(
                    lambda m, _rx=rx: m.group(2) if _rx.search(m.group(1)) else m.group(0), text
                )
            text = rx.sub("", text)

        drop_words = [w.strip() for w in (self.get("remove_lines", "") or "").splitlines() if w.strip()]
        if drop_words:
            text = "\n".join(
                line for line in text.splitlines()
                if not any(w in line for w in drop_words)
            )

        for rule in (self.get("replacements", "") or "").splitlines():
            if "=>" not in rule:
                continue
            src, _, dst = rule.partition("=>")
            src, dst = src.strip(), dst.strip()
            if not src:
                continue
            if src.startswith("re:"):
                try:
                    text = re.sub(src[3:], dst, text)
                except re.error as e:
                    ctx.log.warning("正则替换规则无效 %s：%s", src, e)
            else:
                text = text.replace(src, dst)

        if self.get("collapse_blank", True):
            text = re.sub(r"\n{3,}", "\n\n", text)
            text = re.sub(r"[ \t]{2,}", " ", text)
        if self.get("trim", True):
            text = "\n".join(line.rstrip() for line in text.splitlines()).strip()

        limit = int(self.get("max_length", 0) or 0)
        if limit and len(text) > limit:
            text = text[: limit - 1].rstrip() + "…"

        item.text = text
        item.meta["plain_text"] = _TAG_RE.sub("", text)

        if self.get("drop_if_empty", True) and not text.strip() and not item.has_media:
            return item.drop("清洗后内容为空")
        return item
