"""内容规则过滤：媒体要求、广告特征、批内相似去重、时间窗口。"""
from __future__ import annotations

import datetime as dt
import re
from difflib import SequenceMatcher

from teleops.core import FilterPlugin, Item, field

_AD_HINTS = [
    r"@[A-Za-z0-9_]{4,}\s*(?:私聊|联系|咨询)",
    r"(?:加|进)群",
    r"(?:代理|招商|返利|下单|开户|USDT|usdt)",
    r"(?:t\.me/joinchat|t\.me/\+)",
]


class ContentRuleFilter(FilterPlugin):
    name = "content_rules"
    display_name = "内容规则"
    version = "1.0.0"
    author = "TeleOps"
    description = "按媒体类型、发布时间、广告特征和文本相似度做精细筛选。"

    config_schema = [
        field("require_media", "必须带媒体", "bool", default=False, group="媒体"),
        field("forbid_media", "必须无媒体", "bool", default=False, group="媒体"),
        field("allow_kinds", "仅允许的媒体类型", "multiselect", default=[],
              options=["photo", "video", "animation", "audio", "voice", "document", "sticker"],
              help="留空表示全部允许。", group="媒体"),
        field("max_media", "最多媒体个数", "int", default=0, help="0 表示不限。", group="媒体"),

        field("max_age_hours", "只要 N 小时内的内容", "int", default=0,
              help="0 表示不限制发布时间。", group="时间"),

        field("drop_ads", "拦截疑似广告", "bool", default=False, group="质量"),
        field("ad_patterns", "自定义广告正则", "text", default="",
              help="每行一个正则，命中即判为广告。", group="质量"),
        field("similarity", "批内相似度去重阈值", "float", default=0.0,
              help="0 表示关闭；0.9 表示相似度 ≥90% 的后一条会被丢弃。", group="质量"),
    ]

    async def apply(self, items: list[Item], ctx) -> list[Item]:
        out: list[Item] = []
        threshold = float(self.get("similarity", 0) or 0)
        for it in items:
            if not self._check(it, ctx):
                continue
            if threshold > 0 and self._too_similar(it, out, threshold):
                ctx.trace(it, "与本批内已有内容高度相似")
                continue
            out.append(it)
        return out

    def _check(self, item: Item, ctx) -> bool:
        media = [m for m in item.media if not m.is_empty()]
        if self.get("require_media", False) and not media:
            return False
        if self.get("forbid_media", False) and media:
            return False

        allow = self.get("allow_kinds", []) or []
        if allow and media and any(m.kind not in allow for m in media):
            return False

        max_media = int(self.get("max_media", 0) or 0)
        if max_media and len(media) > max_media:
            return False

        hours = int(self.get("max_age_hours", 0) or 0)
        if hours and item.published_at:
            published = item.published_at
            if published.tzinfo is None:
                published = published.replace(tzinfo=dt.timezone.utc)
            if dt.datetime.now(dt.timezone.utc) - published > dt.timedelta(hours=hours):
                return False

        if self.get("drop_ads", False) and self._looks_like_ad(item):
            ctx.trace(item, "疑似广告")
            return False
        return True

    def _looks_like_ad(self, item: Item) -> bool:
        text = item.meta.get("plain_text") or item.text or ""
        patterns = list(_AD_HINTS)
        custom = (self.get("ad_patterns", "") or "").splitlines()
        patterns += [p.strip() for p in custom if p.strip()]
        hits = 0
        for p in patterns:
            try:
                if re.search(p, text, re.IGNORECASE):
                    hits += 1
            except re.error:
                continue
        return hits >= 2

    @staticmethod
    def _too_similar(item: Item, seen: list[Item], threshold: float) -> bool:
        a = (item.meta.get("plain_text") or item.text or "").strip()
        if len(a) < 20:
            return False
        for other in seen[-30:]:
            b = (other.meta.get("plain_text") or other.text or "").strip()
            if not b:
                continue
            if SequenceMatcher(None, a, b).quick_ratio() < threshold:
                continue
            if SequenceMatcher(None, a, b).ratio() >= threshold:
                return True
        return False
