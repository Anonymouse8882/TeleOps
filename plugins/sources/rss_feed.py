"""RSS / Atom 订阅源。"""
from __future__ import annotations

import datetime as dt
import html as html_lib
import re
from time import mktime

import feedparser

from teleops.core import Item, Media, SourcePlugin, field

_TAG_RE = re.compile(r"<[^>]+>")
_IMG_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)


class RssSource(SourcePlugin):
    name = "rss"
    display_name = "RSS 订阅"
    version = "1.0.0"
    author = "TeleOps"
    description = "抓取 RSS/Atom 订阅，自动提取标题、摘要与首图。"

    config_schema = [
        field("url", "订阅地址", "string", required=True, placeholder="https://example.com/feed.xml"),
        field("limit", "每轮最多取", "int", default=5),
        field("content_field", "正文取自", "select", default="summary",
              options=[
                  {"value": "summary", "label": "摘要"},
                  {"value": "content", "label": "全文"},
                  {"value": "title", "label": "仅标题"},
              ]),
        field("strip_html", "转为纯文本", "bool", default=True),
        field("max_length", "正文截断", "int", default=800),
        field("extract_image", "提取首图作为媒体", "bool", default=True),
        field("newest_first", "按最新优先", "bool", default=True),
    ]

    async def fetch(self, ctx) -> list[Item]:
        raw = await ctx.http_get(self.config["url"])
        feed = feedparser.parse(raw)
        if getattr(feed, "bozo", 0) and not feed.entries:
            raise RuntimeError(f"订阅解析失败：{getattr(feed, 'bozo_exception', '未知错误')}")

        entries = list(feed.entries)
        if not self.get("newest_first", True):
            entries.reverse()
        entries = entries[: max(1, int(self.get("limit", 5)))]

        feed_title = getattr(feed.feed, "title", "") if hasattr(feed, "feed") else ""
        items: list[Item] = []
        for e in entries:
            body = self._body(e)
            image = None
            if self.get("extract_image", True):
                image = self._image(e)
            text = body
            if self.get("strip_html", True):
                text = html_lib.unescape(_TAG_RE.sub("", body)).strip()
            limit = int(self.get("max_length", 800) or 0)
            if limit and len(text) > limit:
                text = text[: limit - 1].rstrip() + "…"

            items.append(
                Item(
                    uid=f"rss:{e.get('id') or e.get('link') or e.get('title')}",
                    title=e.get("title", ""),
                    text=text,
                    parse_mode=None if self.get("strip_html", True) else "html",
                    url=e.get("link", ""),
                    author=e.get("author", ""),
                    source_name=feed_title,
                    published_at=_published(e),
                    media=[Media(kind="photo", url=image)] if image else [],
                    meta={"plain_text": text},
                )
            )
        return items

    def _body(self, entry) -> str:
        mode = self.get("content_field", "summary")
        if mode == "title":
            return entry.get("title", "")
        if mode == "content":
            content = entry.get("content")
            if content:
                return content[0].get("value", "")
        return entry.get("summary", "") or entry.get("description", "")

    @staticmethod
    def _image(entry) -> str | None:
        for enc in entry.get("enclosures", []) or []:
            if str(enc.get("type", "")).startswith("image/"):
                return enc.get("href")
        for key in ("content", "summary", "description"):
            val = entry.get(key)
            blob = val[0].get("value", "") if isinstance(val, list) and val else (val or "")
            m = _IMG_RE.search(blob if isinstance(blob, str) else "")
            if m:
                return m.group(1)
        media = entry.get("media_content") or []
        if media and media[0].get("url"):
            return media[0]["url"]
        return None


def _published(entry) -> dt.datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return dt.datetime.fromtimestamp(mktime(t), dt.timezone.utc)
    return None
