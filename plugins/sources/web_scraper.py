"""通用网页采集：用 CSS 选择器从任意网站抓列表。

例：抓某站首页文章
    列表选择器  .article-list .item
    标题选择器  h2 a
    链接选择器  h2 a
    正文选择器  .summary
"""
from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from teleops.core import Item, Media, SourcePlugin, field


class WebScraperSource(SourcePlugin):
    name = "web_scraper"
    display_name = "网页采集"
    version = "1.0.0"
    author = "TeleOps"
    description = "用 CSS 选择器从任意网页抓取列表内容，可选进入详情页取全文。"

    config_schema = [
        field("url", "页面地址", "string", required=True, group="页面"),
        field("list_selector", "列表项选择器", "string", required=True,
              placeholder=".post-list .item", group="页面"),
        field("title_selector", "标题选择器", "string", default="", group="页面"),
        field("link_selector", "链接选择器", "string", default="a", group="页面"),
        field("text_selector", "正文选择器", "string", default="", group="页面"),
        field("image_selector", "图片选择器", "string", default="", group="页面"),
        field("limit", "每轮最多取", "int", default=5, group="页面"),

        field("detail_selector", "详情页正文选择器", "string", default="",
              help="填了就会打开链接抓详情页正文（会变慢）。", group="详情"),

        field("strip_html", "转为纯文本", "bool", default=True, group="处理"),
        field("max_length", "正文截断", "int", default=800, group="处理"),
        field("headers", "自定义请求头", "json", default={},
              help='例如 {"Referer": "https://example.com"}', group="处理"),
    ]

    async def fetch(self, ctx) -> list[Item]:
        url = self.config["url"]
        headers = self.get("headers", {}) or {}
        html = await ctx.http_get(url, headers=headers)
        soup = BeautifulSoup(html, "lxml")

        nodes = soup.select(self.config["list_selector"])[: max(1, int(self.get("limit", 5)))]
        if not nodes:
            ctx.log.warning("列表选择器没有匹配到任何节点：%s", self.config["list_selector"])

        items: list[Item] = []
        for node in nodes:
            title = _text(node, self.get("title_selector", "")) or _clean(node.get_text(" "))[:120]
            link = _attr(node, self.get("link_selector", "a"), "href")
            if link:
                link = urljoin(url, link)
            body = _text(node, self.get("text_selector", ""))
            image = _attr(node, self.get("image_selector", ""), "src")
            if image:
                image = urljoin(url, image)

            detail_sel = self.get("detail_selector", "")
            if detail_sel and link:
                try:
                    dhtml = await ctx.http_get(link, headers=headers)
                    dsoup = BeautifulSoup(dhtml, "lxml")
                    found = dsoup.select_one(detail_sel)
                    if found:
                        body = _clean(found.get_text("\n"))
                except Exception as e:
                    ctx.log.warning("抓取详情页失败 %s：%s", link, e)

            text = body or title
            limit = int(self.get("max_length", 800) or 0)
            if limit and len(text) > limit:
                text = text[: limit - 1].rstrip() + "…"

            if not text.strip():
                continue

            items.append(
                Item(
                    uid=f"web:{link or title}",
                    title=title,
                    text=text,
                    parse_mode=None if self.get("strip_html", True) else "html",
                    url=link or url,
                    source_name=_domain(url),
                    media=[Media(kind="photo", url=image)] if image else [],
                    meta={"plain_text": text},
                )
            )
        return items


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _text(node, selector: str) -> str:
    if not selector:
        return ""
    found = node.select_one(selector)
    return _clean(found.get_text(" ")) if found else ""


def _attr(node, selector: str, attr: str) -> str:
    if not selector:
        return ""
    found = node.select_one(selector)
    if not found:
        return ""
    return found.get(attr) or found.get("data-" + attr) or ""


def _domain(url: str) -> str:
    m = re.match(r"https?://([^/]+)", url)
    return m.group(1) if m else url
