"""流经管线的统一数据模型。

所有插件都读写 Item，这样源/过滤/格式化/输出可以任意组合。
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Literal

log = logging.getLogger(__name__)

MediaKind = Literal["photo", "video", "audio", "voice", "document", "animation", "sticker", "webpage"]


def _jsonable(v: Any) -> Any:
    """把任意值压成能落库的 JSON，永不抛。

    插件的 raw/meta/extra 是自由字典，第三方插件完全可能往里塞 datetime、
    元组键甚至循环引用。json 的 default= 只对**值**生效，对键不生效，所以整块
    dumps 失败是很现实的；此时不能把整个字典丢掉——tg_channel 的游标结算依赖
    raw 里的 chat_raw/group_ids，raw 变成空字典的后果是游标永久停住、每轮抓回
    同一批又被去重全拦下，表现为「运行成功、0 条产出」的静默断供。
    所以失败时逐键降级，只丢真正救不回来的那个键，并且把键名打进日志。
    """
    try:
        return json.loads(json.dumps(v, ensure_ascii=False, allow_nan=False, default=str))
    except Exception:
        pass
    if isinstance(v, dict):
        out: dict[str, Any] = {}
        for k, val in v.items():
            key = k if isinstance(k, str) else str(k)
            try:
                out[key] = json.loads(json.dumps(val, ensure_ascii=False, allow_nan=False, default=str))
            except Exception as e:
                out[key] = repr(val)[:500]
                log.warning("字段 %s 无法序列化，已降级成字符串：%s", key, e)
        return out
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    log.warning("值无法序列化，已降级成字符串：%r", type(v))
    return repr(v)[:500]


def _iso(v: Any) -> str | None:
    """时间转 ISO 字符串。插件可能直接塞了字符串，原样放行，别在这里抛。"""
    if not v:
        return None
    if isinstance(v, (dt.datetime, dt.date)):
        return v.isoformat()
    return str(v)


def _dt(v: Any) -> dt.datetime | None:
    """ISO 字符串转时间，统一成 aware UTC。

    不统一的话，多源合流时只要混进一个 naive 值，按时间排序就抛 TypeError，
    而且是数据依赖的偶发故障。
    """
    if not v:
        return None
    if isinstance(v, dt.datetime):
        d = v
    else:
        try:
            d = dt.datetime.fromisoformat(str(v))
        except ValueError:
            return None
    return d.replace(tzinfo=dt.timezone.utc) if d.tzinfo is None else d.astimezone(dt.timezone.utc)


@dataclass
class Media:
    """一份媒体。三种来源方式，优先级：path > url > tg_ref。

    * path   —— 已下载到本地的文件（"下载模式"）
    * url    —— 直链，交给 Telegram 服务端拉取
    * tg_ref —— 指向某条 TG 消息的媒体，发送时直接复用 file reference（"复制模式"，不落盘、最快）
    """

    kind: MediaKind = "document"
    path: str | None = None
    url: str | None = None
    tg_ref: dict[str, Any] | None = None  # {"account_id": 1, "chat": "@x", "msg_id": 123}
    filename: str | None = None
    mime: str | None = None
    size: int | None = None
    spoiler: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def is_empty(self) -> bool:
        return not (self.path or self.url or self.tg_ref)

    def to_wire(self) -> dict[str, Any]:
        """落库用的完整快照（to_dict 是给界面看的，会丢字段，别拿它做持久化）。"""
        return _jsonable({
            "kind": self.kind, "path": self.path, "url": self.url, "tg_ref": self.tg_ref,
            "filename": self.filename, "mime": self.mime, "size": self.size,
            "spoiler": self.spoiler, "extra": self.extra,
        })

    @classmethod
    def from_wire(cls, d: Any) -> "Media":
        d = d if isinstance(d, dict) else {}
        return cls(
            kind=d.get("kind") or "document", path=d.get("path"), url=d.get("url"),
            tg_ref=copy.deepcopy(d.get("tg_ref")), filename=d.get("filename"),
            mime=d.get("mime"), size=d.get("size"), spoiler=bool(d.get("spoiler")),
            extra=copy.deepcopy(d.get("extra")) or {},
        )


@dataclass
class Item:
    """一条待发布的内容。"""

    # 源内唯一标识，用于去重（例如 "tg:@channel:12345"）
    uid: str = ""
    title: str = ""
    text: str = ""                     # 正文（纯文本或 HTML，取决于 parse_mode）
    parse_mode: str | None = "html"    # html / md / None
    url: str = ""                      # 原文链接
    author: str = ""
    source_name: str = ""              # 来源展示名，格式化插件常用
    published_at: dt.datetime | None = None
    media: list[Media] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    buttons: list[list[dict[str, str]]] = field(default_factory=list)  # [[{"text":..,"url":..}]]
    raw: dict[str, Any] = field(default_factory=dict)   # 插件私有原始数据
    meta: dict[str, Any] = field(default_factory=dict)  # 管线中传递的附加信息
    # 发送控制（格式化插件可修改）
    silent: bool = False
    no_webpage: bool = False
    schedule_at: dt.datetime | None = None
    dropped: bool = False              # 被过滤器标记丢弃
    drop_reason: str = ""

    def fingerprint(self) -> str:
        """去重指纹：有 uid 用 uid，否则对正文+媒体取哈希。"""
        if self.uid:
            return self.uid[:250]
        base = "\n".join(
            [self.text.strip(), self.url] + [m.url or m.path or "" for m in self.media]
        )
        return "h:" + hashlib.sha1(base.encode("utf-8", "ignore")).hexdigest()

    def content_hash(self) -> str:
        """只看内容的指纹，忽略 uid。

        去重节点选「正文 + 媒体哈希」时用它：有些源每次抓回来 uid 都不一样
        （比如带时间戳的链接），只有内容是稳定的。
        """
        base = "\n".join(
            [self.text.strip(), self.title.strip()] + [m.url or m.path or "" for m in self.media]
        )
        return "h:" + hashlib.sha1(base.encode("utf-8", "ignore")).hexdigest()

    def drop(self, reason: str = "") -> "Item":
        self.dropped = True
        self.drop_reason = reason
        return self

    @property
    def has_media(self) -> bool:
        return any(not m.is_empty() for m in self.media)

    def preview(self, limit: int = 160) -> str:
        t = (self.text or self.title or "").replace("\n", " ").strip()
        if len(t) > limit:
            t = t[: limit - 1] + "…"
        if not t and self.has_media:
            t = f"[{len(self.media)} 个媒体]"
        return t

    def to_dict(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "title": self.title,
            "text": self.text,
            "parse_mode": self.parse_mode,
            "url": self.url,
            "author": self.author,
            "source_name": self.source_name,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "media": [
                {"kind": m.kind, "path": m.path, "url": m.url, "tg_ref": m.tg_ref,
                 "filename": m.filename, "spoiler": m.spoiler}
                for m in self.media
            ],
            "tags": self.tags,
            "buttons": self.buttons,
            "silent": self.silent,
            "no_webpage": self.no_webpage,
            "dropped": self.dropped,
            "drop_reason": self.drop_reason,
            "meta": {k: v for k, v in self.meta.items() if isinstance(v, (str, int, float, bool, list, dict, type(None)))},
        }

    # ---------------------------------------------------------------- 持久化
    # to_dict 是给界面的（刻意丢掉 raw 这类内部数据），落库必须用 to_wire：
    # tg_channel 的游标结算完全依赖 raw["chat_raw"] 与 raw["group_ids"]，
    # 用 to_dict 存一遍再读回来，游标就再也算不对了。
    def to_wire(self) -> dict[str, Any]:
        d = {
            "uid": self.uid, "title": self.title, "text": self.text,
            "parse_mode": self.parse_mode, "url": self.url, "author": self.author,
            "source_name": self.source_name,
            "published_at": _iso(self.published_at),
            "media": [m.to_wire() for m in self.media],
            "tags": list(self.tags), "buttons": self.buttons,
            "raw": self.raw, "meta": self.meta,
            "silent": self.silent, "no_webpage": self.no_webpage,
            "schedule_at": _iso(self.schedule_at),
            "dropped": self.dropped, "drop_reason": self.drop_reason,
        }
        # 整体过一遍：buttons/tags/tg_ref 这些也可能被插件塞进非 JSON 值，
        # 只护住 raw/meta 的话，爆炸点只是从这里挪到 INSERT，消息照样进不了队列。
        return _jsonable(d)

    @classmethod
    def from_wire(cls, d: Any) -> "Item":
        """从落库的字典还原，**永不抛异常**，坏字段一律降级成默认值。

        队列里抛异常有两种结局都不好：抛在 worker 主循环外整张图停摆，抛在里面
        就是一条毒消息反复重试烧 CPU。而且返回的 Item 与入参字典不共享任何可变
        对象——扇出时两条分支各自 from_wire，共享 meta 会让先跑的分支被后跑的
        覆盖（clean_text 就是原地写 item.meta['plain_text'] 的），过滤结果随调度
        顺序抖动，直接打穿分支隔离。
        """
        d = d if isinstance(d, dict) else {}
        media = [Media.from_wire(m) for m in (d.get("media") or []) if isinstance(m, dict)]
        tags = d.get("tags")
        buttons = d.get("buttons")
        return cls(
            uid=str(d.get("uid") or ""), title=str(d.get("title") or ""),
            text=str(d.get("text") or ""),
            # 键在就照抄（含显式 None），键不在退回 dataclass 默认值 "html"——
            # 画布上手搓 payload 的节点漏写这个键的话，正文会以纯文本发出去，
            # 订阅者看到的是裸的 <b> 标签，而 dry-run 预览还看不出来。
            parse_mode=d["parse_mode"] if "parse_mode" in d else "html",
            url=str(d.get("url") or ""), author=str(d.get("author") or ""),
            source_name=str(d.get("source_name") or ""),
            published_at=_dt(d.get("published_at")), media=media,
            tags=list(tags) if isinstance(tags, list) else [],
            buttons=copy.deepcopy(buttons) if isinstance(buttons, list) else [],
            raw=copy.deepcopy(d.get("raw")) if isinstance(d.get("raw"), dict) else {},
            meta=copy.deepcopy(d.get("meta")) if isinstance(d.get("meta"), dict) else {},
            silent=bool(d.get("silent")), no_webpage=bool(d.get("no_webpage")),
            schedule_at=_dt(d.get("schedule_at")),
            dropped=bool(d.get("dropped")), drop_reason=str(d.get("drop_reason") or ""),
        )
