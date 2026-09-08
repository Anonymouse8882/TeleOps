"""TG 频道搬运源 —— 内置示例插件。

可以一次填多个源频道（一行一个），每个频道的进度游标彼此独立，
互不干扰；多源之间的取数策略见「多源策略」配置项。

从 Telegram 频道/群组抓取历史或增量消息，支持三种抓取顺序：

  * incremental —— 增量：只搬运本工作流启动之后的新消息（默认）
  * from_start  —— 从头开始：从频道第 1 条消息按顺序往后搬，进度持久化在游标里
  * random      —— 随机：在指定 id 区间内随机取样，适合"老号翻新"式的循环发布

媒体有三种处理方式：

  * copy     —— 直接复用源消息的 file reference 转发内容（不落盘、最快、最省流量）
  * download —— 先下载到本地再上传（可配合格式化插件二次加工，能去掉原始来源痕迹）
  * none     —— 丢弃媒体，只保留文字

相册（同一 grouped_id 的多条消息）会自动合并成一条。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import random
import re
from typing import Any

from telethon.extensions import html as tg_html
from telethon.tl.types import (
    Message,
    MessageMediaDocument,
    MessageMediaPhoto,
    MessageMediaWebPage,
    MessageService,
)

from teleops.core import Item, Media, SourcePlugin, field
from teleops.tg.client import normalize_peer

CURSOR_KEY = "tg_channel_cursor"            # 旧版单频道游标（int），保留用于自动迁移
CURSORS_KEY = "tg_channel_cursors"          # {chat: last_id}
RANDOM_USED_KEY = "tg_channel_random_used"  # 旧版单频道随机池
RANDOM_USED_MAP = "tg_channel_random_pool"  # {chat: [id, ...]}
RR_KEY = "tg_channel_rr_index"              # 轮流策略的指针
ALBUM_MAX = 10                              # Telegram 单个相册最多 10 条，且 id 连续


def split_chats(raw: str) -> list[str]:
    """把多行/逗号分隔的频道列表拆开并去重（保持填写顺序）。"""
    parts = re.split(r"[\n,，;；\s]+", str(raw or ""))
    out: list[str] = []
    for p in parts:
        p = p.strip()
        if p and p not in out:
            out.append(p)
    return out


class TgChannelSource(SourcePlugin):
    name = "tg_channel"
    display_name = "TG 频道搬运"
    version = "1.3.0"
    author = "TeleOps"
    description = ("抓取一个或多个 Telegram 频道的消息，支持增量 / 从头开始 / 随机三种模式，"
                   "媒体可复制或下载后重发。每个源频道的进度独立记录。")
    requires_account = True

    config_schema = [
        field("source_chat", "源频道（一行一个）", "text", required=True,
              placeholder="@channel_one\n@channel_two\n-1001234567890\nt.me/channel_three",
              help="可以填多个，一行一个（也支持逗号分隔）。私有频道需要当前账号已加入。"
                   "每个频道的进度各自独立记录，删掉某行不影响其他频道。", group="来源"),
        field("multi_strategy", "多源策略", "select", default="spread",
              options=[
                  {"value": "spread", "label": "均分 —— 每轮从每个频道各取一些"},
                  {"value": "round_robin", "label": "轮流 —— 每轮只取一个频道，下轮换下一个"},
                  {"value": "merge", "label": "合并 —— 全部取来后按发布时间排序取前 N 条"},
              ],
              help="只填一个源频道时该项无影响。", group="来源"),
        field("account_id", "使用账号", "account",
              help="留空则使用工作流绑定的账号。", group="来源"),
        field("mode", "抓取模式", "select", default="incremental",
              options=[
                  {"value": "incremental", "label": "增量 —— 只搬新消息"},
                  {"value": "from_start", "label": "从头开始 —— 按顺序补历史"},
                  {"value": "random", "label": "随机 —— 在区间内随机取样"},
              ], group="来源"),
        field("batch_size", "每轮抓取条数", "int", default=5,
              help="一次运行最多产出多少条（相册算 1 条）。", group="来源"),
        field("min_id", "起始消息 ID", "int", default=0,
              help="从头开始/随机模式的下界。0 表示不限。", group="来源"),
        field("max_id", "结束消息 ID", "int", default=0,
              help="随机模式的上界；0 表示自动取频道当前最新一条。", group="来源"),
        field("random_recent", "随机范围（最近 N 条）", "int", default=0,
              help="仅随机模式：只在最近 N 条里随机；0 表示整个频道。", group="来源"),

        field("media_mode", "媒体处理", "select", default="copy",
              options=[
                  {"value": "copy", "label": "复制 —— 复用原文件，不落盘"},
                  {"value": "download", "label": "下载 —— 存到本地再上传"},
                  {"value": "none", "label": "忽略 —— 只要文字"},
              ], group="内容"),
        field("keep_format", "保留原文排版", "bool", default=True,
              help="保留加粗/链接等实体格式（以 HTML 形式传递给后续插件）。", group="内容"),
        field("group_album", "合并相册", "bool", default=True, group="内容"),

        field("require_media", "只要带媒体的消息", "bool", default=False, group="筛选"),
        field("require_text", "只要带文字的消息", "bool", default=False, group="筛选"),
        field("skip_forwards", "跳过转发消息", "bool", default=False, group="筛选"),
        field("skip_service", "跳过系统消息", "bool", default=True, group="筛选"),
        field("skip_webpage_only", "跳过纯链接预览", "bool", default=False, group="筛选"),
        field("min_length", "正文最少字数", "int", default=0, group="筛选"),
    ]

    # ------------------------------------------------------------------ 主流程
    async def setup(self, ctx) -> None:
        # 每个源频道各记一份"本轮扫描到的最大 id"，提交时分别推进
        self._scan_max: dict[str, int] = {}
        self._cursors: dict[str, int] = {}
        # 每个源频道本轮产出的条目（取相册里最大的消息 id），升序结算
        self._produced_ids: dict[str, list[int]] = {}
        ctx.add_commit_hook(self._commit_cursors)

    async def fetch(self, ctx) -> list[Item]:
        chats = split_chats(self.config.get("source_chat"))
        if not chats:
            raise ValueError("没有填写任何源频道")

        client = await ctx.client_for(self.config.get("account_id") or None)
        account_id = getattr(ctx.account, "id", None)
        if self.config.get("account_id"):
            account_id = int(self.config["account_id"])

        mode = self.get("mode", "incremental")
        batch = max(1, int(self.get("batch_size", 5)))
        strategy = self.get("multi_strategy", "spread") if len(chats) > 1 else "spread"

        await self._load_cursors(ctx, chats)

        if strategy == "round_robin":
            chats = [await self._pick_round_robin(ctx, chats)]
            per_chat = batch
        elif strategy == "merge":
            per_chat = batch  # 先各自多取一些，最后统一排序截断
        else:  # spread：尽量平均分配名额，除不尽的前几个频道多担一条
            per_chat = max(1, -(-batch // len(chats)))

        items: list[Item] = []
        for chat_raw in chats:
            try:
                chat = normalize_peer(chat_raw)
            except ValueError as e:
                ctx.log.warning("源频道 %r 格式无效，已跳过：%s", chat_raw, e)
                continue
            try:
                got = await self._fetch_one(client, chat_raw, chat, per_chat, mode, account_id, ctx)
            except Exception as e:
                # 单个频道出问题（没权限/被删）不能拖垮整轮
                ctx.log.error("从 %s 采集失败：%s: %s", chat_raw, type(e).__name__, e)
                continue
            items.extend(got)
            if strategy == "spread" and len(items) >= batch:
                break

        if strategy == "merge":
            items.sort(key=lambda i: i.published_at or _EPOCH, reverse=False)
        items = items[:batch]

        ctx.log.info(
            "tg_channel[%s/%s] 从 %d 个频道取到 %d 条", mode, strategy, len(chats), len(items)
        )
        return items

    async def _fetch_one(
        self, client, chat_raw: str, chat, batch: int, mode: str, account_id: int | None, ctx
    ) -> list[Item]:
        if mode == "random":
            messages = await self._fetch_random(client, chat_raw, chat, batch, ctx)
            # 随机取样是按 id 点抽的，抽中相册里的一条时另外几张不会跟着来，
            # 必须往两边补齐，否则多图消息会退化成单图。
            messages = await self._complete_albums(client, chat_raw, chat, messages, ctx, backward=True)
            await self._mark_random_used(ctx, chat_raw, [m.id for m in messages])
        else:
            messages = await self._fetch_sequential(
                client, chat_raw, chat, batch, ctx, from_start=(mode == "from_start")
            )
            # 顺序抓有条数窗口，相册可能正好被窗口边界切断，向后补齐即可
            # （游标之前的消息已经处理过，不能向前补，否则会重发）。
            messages = await self._complete_albums(client, chat_raw, chat, messages, ctx, backward=False)
            if messages:
                self._scan_max[chat_raw] = max(m.id for m in messages)

        out: list[Item] = []
        for group in self._group(messages):
            item = await self._build_item(group, chat_raw, chat, account_id, client, ctx)
            if item is not None:
                out.append(item)
            if len(out) >= batch:
                break
        self._produced_ids.setdefault(chat_raw, []).extend(
            max(i.raw.get("group_ids") or [0]) for i in out
        )
        return out

    # ------------------------------------------------------------- 游标存取
    async def _load_cursors(self, ctx, chats: list[str]) -> None:
        raw = await ctx.state.get(CURSORS_KEY)
        self._cursors = dict(raw) if isinstance(raw, dict) else {}
        # 从旧版单频道游标平滑迁移
        if not self._cursors:
            legacy = await ctx.state.get(CURSOR_KEY)
            if isinstance(legacy, int) and legacy > 0 and len(chats) == 1:
                self._cursors[chats[0]] = legacy
                ctx.log.info("已把旧版游标 %s 迁移到频道 %s", legacy, chats[0])

    async def _pick_round_robin(self, ctx, chats: list[str]) -> str:
        idx = int(await ctx.state.get(RR_KEY, 0) or 0)
        chat = chats[idx % len(chats)]
        await ctx.state.set(RR_KEY, (idx + 1) % len(chats))
        return chat

    # ------------------------------------------------------------------ 顺序抓
    async def _fetch_sequential(
        self, client, chat_raw: str, chat, batch: int, ctx, *, from_start: bool
    ) -> list[Message]:
        cursor = self._cursors.get(chat_raw)
        min_id = int(self.get("min_id", 0) or 0)

        if cursor is None:
            if from_start:
                cursor = max(0, min_id - 1) if min_id else 0
                self._cursors[chat_raw] = cursor
            else:
                # 增量模式：首轮只记住该频道当前最新 id，不搬历史
                latest = await client.get_messages(chat, limit=1)
                cursor = latest[0].id if latest else 0
                self._cursors[chat_raw] = cursor
                await ctx.state.set(CURSORS_KEY, self._cursors)
                ctx.log.info("增量模式初始化：%s 游标定位到最新消息 id=%s", chat_raw, cursor)
                return []

        # 多抓一些，给筛选/合并留余量
        limit = min(100, max(batch * 4, 10))
        raw = await client.get_messages(chat, limit=limit, min_id=int(cursor), reverse=True)
        if not raw:
            ctx.log.debug("%s 游标 %s 之后没有新消息", chat_raw, cursor)
            return []
        self._scan_max[chat_raw] = max(m.id for m in raw)
        if self.get("skip_service", True):
            raw = [m for m in raw if not isinstance(m, MessageService)]
        return list(raw)

    # ------------------------------------------------------------------ 随机抓
    async def _fetch_random(self, client, chat_raw: str, chat, batch: int, ctx) -> list[Message]:
        latest = await client.get_messages(chat, limit=1)
        if not latest:
            return []
        top_id = latest[0].id

        lower = int(self.get("min_id", 0) or 1)
        upper = int(self.get("max_id", 0) or 0) or top_id
        recent = int(self.get("random_recent", 0) or 0)
        if recent > 0:
            lower = max(lower, upper - recent + 1)
        if upper < lower:
            lower, upper = upper, lower
        if upper <= 0:
            return []

        pool: dict[str, list[int]] = dict(await ctx.state.get(RANDOM_USED_MAP, {}) or {})
        if chat_raw not in pool:  # 从旧版单频道随机池迁移
            legacy = await ctx.state.get(RANDOM_USED_KEY, [])
            pool[chat_raw] = list(legacy or [])
        used: list[int] = list(pool.get(chat_raw) or [])
        used_set = set(used)
        span = upper - lower + 1
        # 用完整个区间就重新开始一轮
        if len(used_set) >= span * 0.9:
            ctx.log.info("%s 的随机池已基本用尽，重置取样记录", chat_raw)
            used, used_set = [], set()

        picked: list[Message] = []
        # 随机取 id 可能命中空洞（消息被删/是服务消息），多试几轮
        for _ in range(6):
            if len(picked) >= batch:
                break
            need = (batch - len(picked)) * 3
            candidates = [
                i for i in {random.randint(lower, upper) for _ in range(need * 2)}
                if i not in used_set
            ][:need]
            if not candidates:
                break
            got = await client.get_messages(chat, ids=candidates)
            for m in got:
                if m is None:
                    continue
                if m.id in used_set:
                    continue
                used_set.add(m.id)
                picked.append(m)
            await asyncio.sleep(0)

        pool[chat_raw] = (used + [m.id for m in picked])[-5000:]
        await ctx.state.set(RANDOM_USED_MAP, pool)
        random.shuffle(picked)
        return picked[: batch * 2]

    async def _mark_random_used(self, ctx, chat_raw: str, ids: list[int]) -> None:
        """把补齐相册时额外捞到的成员也记进取样池，避免下轮重复命中同一个相册。"""
        if not ids:
            return
        pool: dict[str, list[int]] = dict(await ctx.state.get(RANDOM_USED_MAP, {}) or {})
        used = list(pool.get(chat_raw) or [])
        known = set(used)
        fresh = [i for i in ids if i not in known]
        if not fresh:
            return
        pool[chat_raw] = (used + fresh)[-5000:]
        await ctx.state.set(RANDOM_USED_MAP, pool)

    # -------------------------------------------------------------- 相册补齐
    async def _complete_albums(
        self, client, chat_raw: str, chat, messages: list[Message], ctx, *, backward: bool
    ) -> list[Message]:
        """把只抓到一半的相册补全。

        Telegram 的相册最多 10 条，且成员的消息 id 是连续的，所以只要围绕已知成员
        向外探一个小窗口，把 grouped_id 相同的捞回来即可。
        """
        if not self.get("group_album", True) or not messages:
            return messages

        members: dict[int, list[Message]] = {}
        for m in messages:
            gid = getattr(m, "grouped_id", None)
            if gid:
                members.setdefault(gid, []).append(m)
        if not members:
            return messages

        have = {m.id for m in messages}
        extra: list[Message] = []
        for gid, found in members.items():
            lo, hi = min(m.id for m in found), max(m.id for m in found)
            span = range(lo - (ALBUM_MAX - 1) if backward else hi + 1, hi + ALBUM_MAX)
            probe = [i for i in span if i > 0 and i not in have]
            if not probe:
                continue
            try:
                got = await client.get_messages(chat, ids=probe)
            except Exception as e:
                ctx.log.warning("补齐相册失败（%s）：%s", chat_raw, e)
                continue
            for m in got or []:
                if m is None or m.id in have:
                    continue
                if getattr(m, "grouped_id", None) == gid:
                    extra.append(m)
                    have.add(m.id)

        if extra:
            ctx.log.info("%s 补齐相册缺失的 %d 条", chat_raw, len(extra))
            messages = sorted(list(messages) + extra, key=lambda m: m.id)
        return messages

    # ------------------------------------------------------------------ 组装
    def _group(self, messages: list[Message]) -> list[list[Message]]:
        """把同一相册（grouped_id）的消息合并为一组。"""
        if not self.get("group_album", True):
            return [[m] for m in messages]
        groups: list[list[Message]] = []
        index: dict[int, list[Message]] = {}
        for m in messages:
            gid = getattr(m, "grouped_id", None)
            if gid:
                if gid in index:
                    index[gid].append(m)
                    continue
                index[gid] = [m]
                groups.append(index[gid])
            else:
                groups.append([m])
        # 组内按 id 升序：相册的"首条"必须稳定，去重指纹和正文都取自它
        for g in groups:
            g.sort(key=lambda m: m.id)
        return groups

    async def _build_item(
        self, group: list[Message], chat_raw: str, chat, account_id: int | None, client, ctx
    ) -> Item | None:
        head = group[0]
        if isinstance(head, MessageService):
            return None  # 系统消息（进群/改名之类）没有搬运价值
        if self.get("skip_forwards", False) and getattr(head, "fwd_from", None):
            return None

        # 取组内第一条有文字的作为正文
        text_msg = next((m for m in group if getattr(m, "message", "")), head)
        raw_text = getattr(text_msg, "message", "") or ""
        if self.get("keep_format", True) and getattr(text_msg, "entities", None):
            text = tg_html.unparse(raw_text, text_msg.entities)
            parse_mode: str | None = "html"
        else:
            text = raw_text
            parse_mode = None

        media_mode = self.get("media_mode", "copy")
        media: list[Media] = []
        if media_mode != "none":
            for m in group:
                mm = getattr(m, "media", None)
                if mm is None:
                    continue
                if isinstance(mm, MessageMediaWebPage):
                    continue  # 链接预览不算媒体
                kind = _media_kind(m)
                if media_mode == "copy":
                    media.append(
                        Media(
                            kind=kind,
                            tg_ref={"account_id": account_id, "chat": str(chat), "msg_id": m.id},
                            spoiler=bool(getattr(mm, "spoiler", False)),
                        )
                    )
                else:  # download
                    path = await client.download_media(m, file=str(ctx.media_dir()))
                    if path:
                        media.append(Media(kind=kind, path=str(path),
                                           spoiler=bool(getattr(mm, "spoiler", False))))
                        if ctx.settings.storage.media_retention_days == 0:
                            ctx.register_temp(path)

        # 筛选
        plain = raw_text.strip()
        if self.get("require_media", False) and not media:
            return None
        if self.get("require_text", False) and not plain:
            return None
        if self.get("skip_webpage_only", False) and not media and isinstance(
            getattr(head, "media", None), MessageMediaWebPage
        ):
            return None
        if not plain and not media:
            return None
        min_len = int(self.get("min_length", 0) or 0)
        if min_len and len(plain) < min_len:
            return None

        chat_name = str(chat)
        item = Item(
            uid=f"tg:{chat_name}:{head.id}",
            text=text,
            parse_mode=parse_mode,
            url=_message_link(chat_name, head.id),
            source_name=chat_name.lstrip("@"),
            published_at=getattr(head, "date", None),
            media=media,
            raw={
                "chat": chat_name,
                "chat_raw": chat_raw,  # 用户填写的原始写法，游标按它归档
                "msg_id": head.id,
                "group_ids": [m.id for m in group],
                "views": getattr(head, "views", None),
                "forwarded": bool(getattr(head, "fwd_from", None)),
            },
            meta={"plain_text": plain, "media_mode": media_mode, "source_chat": chat_raw},
        )
        return item

    # ------------------------------------------------------------- 游标推进
    async def _commit_cursors(self, ctx) -> None:
        """只在内容确实有结论之后才推进游标，每个源频道单独结算。

        对本轮产出的条目按 id 升序逐个看，两种情况算"有结论"：
        - 成功发送
        - 被过滤链/格式化明确丢弃（不合规的内容，重来多少次也还是丢）

        游标停在第一个"没结论"的条目之前——发送失败或被本轮限量截断的，
        下轮还要重新处理。整批都有结论就直接跳到本轮扫描到的最大 id。

        注意不能只看"有没有发出去"：条目被过滤链全部拦下时一条也发不出去，
        当成发送失败处理的话游标永远不动，下轮又抓同一批、又全被拦，卡死。
        """
        if self.get("mode", "incremental") == "random":
            return

        def _ids_by_chat(items: list) -> dict[str, set[int]]:
            out: dict[str, set[int]] = {}
            for it in items:
                chat_raw = it.raw.get("chat_raw")
                ids = it.raw.get("group_ids") or []
                if chat_raw and ids:
                    out.setdefault(chat_raw, set()).add(max(ids))
            return out

        settled = _ids_by_chat(list(ctx.sent_items) + list(getattr(ctx, "dropped_items", [])))

        changed = False
        for chat_raw, scanned in self._scan_max.items():
            produced = sorted(self._produced_ids.get(chat_raw) or [])
            done = settled.get(chat_raw, set())
            target = 0
            for mid in produced:
                if mid not in done:
                    break          # 这条没结论，游标就停在它前面
                target = mid
            else:
                # 本轮产出的全部有结论（含一条都没产出的情况），整个扫描窗口可以跳过
                target = max(target, scanned)
            if not target:
                continue
            if target > int(self._cursors.get(chat_raw) or 0):
                self._cursors[chat_raw] = target
                changed = True
                ctx.log.debug("%s 游标推进到 %s", chat_raw, target)

        if changed:
            # 合并写而不是整份覆盖：图模式下结算发生在采集很久之后，
            # self._cursors 是 fetch 那一刻的快照。整份写回会把这期间发生的
            # 一切游标变更抹掉——同频道被回拨（重复搬运），别的频道的条目
            # 整条消失（那个频道会从头再搬一遍）。
            live = dict(await ctx.state.get(CURSORS_KEY, {}) or {})
            for chat, val in self._cursors.items():
                if int(val or 0) > int(live.get(chat) or 0):
                    live[chat] = val
            await ctx.state.set(CURSORS_KEY, live)
            self._cursors = live


_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)


def _media_kind(m: Message) -> str:
    if isinstance(getattr(m, "media", None), MessageMediaPhoto):
        return "photo"
    if isinstance(getattr(m, "media", None), MessageMediaDocument):
        if getattr(m, "video", None):
            return "video"
        if getattr(m, "voice", None):
            return "voice"
        if getattr(m, "audio", None):
            return "audio"
        if getattr(m, "gif", None):
            return "animation"
        if getattr(m, "sticker", None):
            return "sticker"
    return "document"


def _message_link(chat: str, msg_id: int) -> str:
    name = chat.lstrip("@")
    if name.startswith("-100"):
        return f"https://t.me/c/{name[4:]}/{msg_id}"
    if name.lstrip("-").isdigit():
        return ""
    return f"https://t.me/{name}/{msg_id}"
