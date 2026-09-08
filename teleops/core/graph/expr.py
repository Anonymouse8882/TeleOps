"""分流条件的求值器。

故意做得很小：字段 + 运算符 + 字面量，用 and / or 连起来，没有括号、没有函数、
没有 eval。用户在画布上填的是一行文本，能写错的地方越少越好，而且这东西要在
消息路径上跑，绝不能因为一条奇怪的表达式把整个分支炸掉。

    text contains "关键词"
    title startswith "【公告】" or meta.source_chat equals "@abc"
    text not contains "广告" and text matches "\\d{4}年"
"""
from __future__ import annotations

import re
from typing import Any

from ..item import Item

# 顺序即匹配优先级：长的必须排在短的前面，否则 "text not matches x" 会被
# "matches" 先切开，字段名变成 "text not"，这个运算符就等于不存在。
OPS = ("not contains", "not equals", "not matches",
       "contains", "equals", "matches", "startswith", "endswith")

# 成对的引号。中文输入法默认打出来的是全角，不认的话条件永远不命中，
# 而且保存时一声不吭——这是最难自查的那种故障。
QUOTES = {'"': '"', "'": "'", "\u201c": "\u201d", "\u2018": "\u2019", "\u300c": "\u300d"}
# 被匹配的正文截断长度：正则回溯的代价随长度暴涨，先砍一刀
MAX_VALUE = 20000


class ExprError(ValueError):
    """表达式写错了，消息直接给用户看。"""


def _field(item: Item, name: str) -> str:
    name = name.strip()
    if name.startswith("meta."):
        return str((item.meta or {}).get(name[5:], ""))
    if name == "tags":
        return " ".join(item.tags or [])
    if name in ("text", "title", "uid", "url", "author", "source_name", "drop_reason"):
        return str(getattr(item, name, "") or "")
    raise ExprError(f"不认识的字段：{name}（可用：text/title/uid/url/author/source_name/tags/meta.xxx）")


def _split_top(s: str, word: str) -> list[str]:
    """按 and / or 切开，引号里的不算。"""
    out, buf, quote, i = [], [], "", 0
    pat = f" {word} "
    while i < len(s):
        c = s[i]
        if quote:
            buf.append(c)
            if c == quote:
                quote = ""
            i += 1
            continue
        if c in QUOTES:
            quote = QUOTES[c]
            buf.append(c)
            i += 1
            continue
        if s[i:i + len(pat)].lower() == pat:
            out.append("".join(buf))
            buf = []
            i += len(pat)
            continue
        buf.append(c)
        i += 1
    out.append("".join(buf))
    return out


def _unquote(v: str) -> str:
    v = v.strip()
    if not v:
        return v
    close = QUOTES.get(v[0])
    if close is None:
        return v                      # 没加引号，原样当字面量
    if len(v) >= 2 and v.endswith(close):
        return v[1:-1]
    raise ExprError(f"引号没配对：{v}（中文输入法的全角引号也可以用，但要成对）")


def _eval_one(item: Item, clause: str) -> bool:
    clause = clause.strip()
    if not clause:
        raise ExprError("条件是空的")
    low = clause.lower()
    for op in OPS:                      # 长的先匹配，"not contains" 要排在 "contains" 前面
        idx = low.find(f" {op} ")
        if idx < 0:
            continue
        left = clause[:idx]
        right = _unquote(clause[idx + len(op) + 2:])
        value = _field(item, left)[:MAX_VALUE]
        if op == "contains":
            return right in value
        if op == "not contains":
            return right not in value
        if op == "equals":
            return value.strip() == right
        if op == "not equals":
            return value.strip() != right
        if op == "startswith":
            return value.startswith(right)
        if op == "endswith":
            return value.endswith(right)
        if op in ("matches", "not matches"):
            try:
                hit = re.search(right, value) is not None
            except re.error as e:
                raise ExprError(f"正则写错了：{e}") from e
            return hit if op == "matches" else not hit
    raise ExprError(f"看不懂这个条件：{clause}（格式是「字段 运算符 \"值\"」）")


def evaluate(expr: str, item: Item) -> bool:
    """空表达式一律算命中——用户还没填条件时，分流节点等于直通。"""
    expr = (expr or "").strip()
    if not expr:
        return True
    for or_part in _split_top(expr, "or"):
        if all(_eval_one(item, c) for c in _split_top(or_part, "and")):
            return True
    return False


def check(expr: str) -> None:
    """保存图时的静态校验：每一段都要查，不能靠求值——求值会短路，
    `a contains "x" or 写错的东西` 里右边那半永远轮不到检查。"""
    expr = (expr or "").strip()
    if not expr:
        return
    probe = Item(text="", title="")
    for or_part in _split_top(expr, "or"):
        for clause in _split_top(or_part, "and"):
            _eval_one(probe, clause)      # 字段名、运算符、引号、正则都在这里校验


def describe_fields() -> list[dict[str, Any]]:
    return [{"name": n} for n in
            ("text", "title", "uid", "url", "author", "source_name", "tags", "meta.<键名>")]
