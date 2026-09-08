"""发送到 Telegram 频道/群组 —— 默认输出端。

支持三种媒体来源（由源插件决定）：
  * tg_ref —— 复用源消息的 file reference，服务端直传，不消耗本地带宽
  * path   —— 上传本地文件
  * url    —— 交给 Telegram 服务端去拉取直链
多个媒体自动作为相册发送。
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from telethon.errors import ChatWriteForbiddenError, MessageTooLongError, SlowModeWaitError
from telethon.tl.custom import Button

from teleops.core import Item, SinkPlugin, Target, field
from teleops.tg.client import normalize_peer, with_flood_retry

MAX_TEXT = 4096
MAX_CAPTION = 1024

_TAG_RE = re.compile(r"<[^>]+>")
_TAG_NAME_RE = re.compile(r"</?\s*([A-Za-z0-9]+)")


class TgSendSink(SinkPlugin):
    name = "tg_send"
    display_name = "发送到 TG 频道"
    version = "1.2.0"
    author = "TeleOps"
    description = "把内容发布到目标频道/群组，自动处理相册、长文截断、静音发送与限流退避。"

    config_schema = [
        field("silent", "静默发送", "bool", default=False, help="订阅者不会收到通知提示音。"),
        field("no_webpage", "禁用链接预览", "bool", default=False),
        field("long_text", "超长正文处理", "select", default="split",
              options=[
                  {"value": "split", "label": "拆成多条发送"},
                  {"value": "truncate", "label": "截断"},
              ]),
        field("caption_overflow", "带媒体时正文过长", "select", default="separate",
              options=[
                  {"value": "separate", "label": "媒体单发，正文另发一条"},
                  {"value": "truncate", "label": "截断到 1024 字"},
              ]),
        field("album", "多媒体合并为相册", "bool", default=True,
              help="关闭则逐条单独发送（内容不会丢，只是变成多条消息）。"),
        field("comment_to", "作为评论发送到", "string", default="",
              help="留空为正常发帖。填讨论组 id 时把内容发到评论区。"),
        field("retry", "失败重试次数", "int", default=2),
    ]

    # ------------------------------------------------------------------ 发送
    async def send(self, item: Item, target: Target, ctx) -> dict[str, Any]:
        client = await ctx.client_for(target.account_id)
        peer = normalize_peer(target.peer)
        entity = await client.get_input_entity(peer)

        silent = bool(target.opt("silent", self.get("silent", False)) or item.silent)
        no_preview = bool(target.opt("no_webpage", self.get("no_webpage", False)) or item.no_webpage)
        parse_mode = item.parse_mode
        buttons = _build_buttons(item)

        files = await self._resolve_media(item, ctx)
        if files:
            return await self._send_with_media(
                client, entity, item, files, silent, parse_mode, buttons, ctx
            )
        return await self._send_text(
            client, entity, item.text, silent, no_preview, parse_mode, buttons, ctx
        )

    # ------------------------------------------------------------- 媒体解析
    async def _resolve_media(self, item: Item, ctx) -> list[Any]:
        """把 Item.media 转成 Telethon 能直接吃的对象列表。

        本地文件的清理由 ctx.register_temp 统一负责，这里不删文件——
        同一条内容可能要发到多个频道，删早了后面的目标就发不出去了。
        """
        files: list[Any] = []
        for m in item.media:
            if m.is_empty():
                continue
            if m.path:
                if Path(m.path).exists():
                    files.append(m.path)
                else:
                    ctx.log.warning("媒体文件不存在：%s", m.path)
            elif m.tg_ref:
                ref = m.tg_ref
                src_client = await ctx.client_for(ref.get("account_id"))
                try:
                    msg = await src_client.get_messages(
                        normalize_peer(ref["chat"]), ids=int(ref["msg_id"])
                    )
                except Exception as e:
                    ctx.log.warning("拉取源媒体失败 %s：%s", ref, e)
                    continue
                if msg is None or msg.media is None:
                    continue
                # 同账号内可直接复用 file reference，Telegram 服务端侧完成搬运
                files.append(msg.media)
            elif m.url:
                files.append(m.url)
        return files

    # ------------------------------------------------------------- 具体发送
    async def _send_with_media(
        self, client, entity, item: Item, files: list[Any], silent: bool,
        parse_mode: str | None, buttons, ctx
    ) -> dict[str, Any]:
        text = item.text or ""
        extra_text = ""
        if _visible_len(text, parse_mode) > MAX_CAPTION:
            if self.get("caption_overflow", "separate") == "separate":
                extra_text, text = text, ""
            else:
                text = _shorten(text, MAX_CAPTION, parse_mode)

        # 正文要另发一条时按钮跟着正文走，否则媒体和正文上会各挂一份
        media_buttons = None if extra_text else buttons

        as_album = self.get("album", True) and len(files) > 1

        if as_album:
            batches: list[Any] = [files]
        else:
            # 关掉相册合并时要逐条发出去，不能只发第一个把其余的丢掉
            batches = [f for f in files]

        ids: list[int] = []
        for idx, payload in enumerate(batches):
            first = idx == 0

            async def _do(p=payload, f=first):
                return await client.send_file(
                    entity,
                    p,
                    caption=(text or None) if f else None,  # 说明文字只跟第一条
                    parse_mode=parse_mode,
                    silent=silent,
                    # 相册整体不支持按钮，非相册时挂在最后一条上
                    buttons=None if as_album else (
                        media_buttons if idx == len(batches) - 1 else None
                    ),
                    force_document=False,
                )

            msg = await self._guarded(_do, ctx)
            mid = _first_id(msg)
            if mid:
                ids.append(mid)
            if idx < len(batches) - 1:
                await asyncio.sleep(1)

        result: dict[str, Any] = {"message_id": ids[0] if ids else None, "media": len(files)}
        if len(batches) > 1:
            result["messages"] = len(ids)

        if extra_text:
            await asyncio.sleep(1)
            more = await self._send_text(client, entity, extra_text, silent, True, parse_mode, buttons, ctx)
            result["extra_message_id"] = more.get("message_id")
        return result

    async def _send_text(
        self, client, entity, text: str, silent: bool, no_preview: bool,
        parse_mode: str | None, buttons, ctx
    ) -> dict[str, Any]:
        text = (text or "").strip()
        if not text:
            return {"skipped": "空内容"}

        chunks = [text]
        if _visible_len(text, parse_mode) > MAX_TEXT:
            if self.get("long_text", "split") == "truncate":
                chunks = [_shorten(text, MAX_TEXT, parse_mode)]
            else:
                chunks = _split_text(text, MAX_TEXT, parse_mode)

        ids: list[int] = []
        for i, chunk in enumerate(chunks):
            async def _do(c=chunk, last=(i == len(chunks) - 1)):
                return await client.send_message(
                    entity, c, parse_mode=parse_mode, link_preview=not no_preview,
                    silent=silent, buttons=buttons if last else None,
                )

            msg = await self._guarded(_do, ctx)
            mid = _first_id(msg)
            if mid:
                ids.append(mid)
            if len(chunks) > 1 and i < len(chunks) - 1:
                await asyncio.sleep(1.5)
        return {"message_id": ids[0] if ids else None, "parts": len(chunks)}

    async def _guarded(self, factory, ctx):
        """统一处理限流与常见错误。"""
        attempts = max(1, int(self.get("retry", 2)) + 1)
        try:
            return await with_flood_retry(
                factory, max_wait=ctx.settings.runtime.max_flood_wait,
                attempts=attempts, logger=ctx.log,
            )
        except SlowModeWaitError as e:
            ctx.log.warning("目标开启了慢速模式，需等待 %ss", e.seconds)
            raise
        except ChatWriteForbiddenError:
            raise RuntimeError("没有该频道的发言权限（账号需为管理员或成员）")
        except MessageTooLongError:
            raise RuntimeError("消息超长，请调整「超长正文处理」选项")


def _build_buttons(item: Item):
    if not item.buttons:
        return None
    rows = []
    for row in item.buttons:
        cells = [Button.url(b.get("text", "🔗"), b["url"]) for b in row if b.get("url")]
        if cells:
            rows.append(cells)
    return rows or None


def _first_id(msg: Any) -> int | None:
    if msg is None:
        return None
    if isinstance(msg, list):
        return getattr(msg[0], "id", None) if msg else None
    return getattr(msg, "id", None)


def _is_html(parse_mode: str | None) -> bool:
    return bool(parse_mode) and str(parse_mode).lower().startswith("htm")


def _visible_len(text: str, parse_mode: str | None) -> int:
    """Telegram 的长度上限算的是渲染后的可见文字，HTML 标签本身不计入。"""
    return len(_TAG_RE.sub("", text)) if _is_html(parse_mode) else len(text)


def _scan(text: str, budget: int) -> tuple[int, list[str]]:
    """找到"可见字符数刚好用满 budget"的原始下标，并带回该处仍未闭合的标签。

    返回 (下标, [未闭合的开标签原文…])。下标永远落在标签之外，不会把标签劈开。
    """
    stack: list[str] = []
    visible = 0
    pos = 0
    for m in _TAG_RE.finditer(text):
        gap = m.start() - pos
        if visible + gap >= budget:
            return pos + (budget - visible), list(stack)
        visible += gap
        tag = m.group(0)
        name_m = _TAG_NAME_RE.match(tag)
        if name_m:
            name = name_m.group(1).lower()
            if tag.startswith("</"):
                for i in range(len(stack) - 1, -1, -1):
                    if _TAG_NAME_RE.match(stack[i]).group(1).lower() == name:
                        del stack[i]
                        break
            elif not tag.endswith("/>"):
                stack.append(tag)
        pos = m.end()
    if visible + (len(text) - pos) >= budget:
        return pos + (budget - visible), list(stack)
    return len(text), list(stack)


def _close(open_tags: list[str]) -> str:
    return "".join(f"</{_TAG_NAME_RE.match(t).group(1)}>" for t in reversed(open_tags))


def _shorten(text: str, limit: int, parse_mode: str | None = None) -> str:
    """截断到 limit 个可见字符。HTML 模式下不切开标签，并补齐未闭合的标签。"""
    if _visible_len(text, parse_mode) <= limit:
        return text
    if not _is_html(parse_mode):
        return text[: limit - 1] + "…"
    cut, open_tags = _scan(text, limit - 1)
    return text[:cut].rstrip() + "…" + _close(open_tags)


def _split_text(text: str, limit: int, parse_mode: str | None = None) -> list[str]:
    """优先在段落/换行处切分，避免把 HTML 标签劈开。

    按可见字符计数；被截断处未闭合的标签会在本段末尾补齐、在下段开头重开，
    这样每一段单独拿去解析都是完整的 HTML。
    """
    html = _is_html(parse_mode)
    out: list[str] = []
    rest = text
    carry = ""  # 上一段遗留的开标签，续到下一段开头
    while _visible_len(carry + rest, parse_mode) > limit:
        budget = limit - _visible_len(carry, parse_mode)
        cut, _ = _scan(rest, budget) if html else (min(budget, len(rest)), [])
        window = rest[:cut]
        # 先找空行，找不到再退到普通换行——rfind("\n") 的结果总是 >= rfind("\n\n")，
        # 所以这里必须分两步判断，用 max() 会让"空行优先"永远轮不上。
        brk = window.rfind("\n\n")
        if brk <= 0:
            brk = window.rfind("\n")
        if brk < cut // 2:
            brk = max(brk, window.rfind(" "))
        if 0 < brk < cut:
            cut = brk
        if cut <= 0:
            break
        head = rest[:cut]
        open_tags = _scan(head, _visible_len(head, parse_mode))[1] if html else []
        out.append((carry + head).rstrip() + _close(open_tags))
        carry = "".join(open_tags)
        rest = rest[cut:].lstrip()
    if rest:
        out.append((carry + rest).strip())
    return out
