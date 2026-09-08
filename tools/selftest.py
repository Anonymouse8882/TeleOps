#!/usr/bin/env python
"""自检：跑一遍容易静默出错的地方，不碰生产库、不发任何消息。

    python tools/selftest.py

覆盖的都是踩过或差点踩到的坑：消息序列化的边界、JSON 列原地修改、
数据库补列迁移、时间戳时区。改完 teleops/core/item.py 或 teleops/db/ 之后跑一下。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import Column, DateTime, Index, create_engine, select, text  # noqa: E402

from teleops.core.item import Item, Media, _jsonable  # noqa: E402
from teleops.db import models as M  # noqa: E402
from teleops.db.session import Database  # noqa: E402

_ok = _fail = 0


def check(name: str, cond: bool, extra: object = "") -> None:
    global _ok, _fail
    if cond:
        _ok += 1
        print(f"  ok   {name}")
    else:
        _fail += 1
        print(f"  FAIL {name} {extra}")


class _Weird:
    pass


def test_serialization() -> None:
    print("消息序列化")
    # 落库前必须保证产物一定能 json.dumps，否则爆炸点会挪到 INSERT，整条消息进不了队列
    it = Item(uid="u", text="正文", buttons=[[{"text": "go", "url": _Weird()}]],
              media=[Media(kind="photo", tg_ref={"account_id": 1, "obj": _Weird()})])
    try:
        json.dumps(it.to_wire())
        check("含非法对象时 to_wire 产物仍可 dumps", True)
    except Exception as e:
        check("含非法对象时 to_wire 产物仍可 dumps", False, repr(e))

    # 插件把时间写成字符串是常见写法，不能因此抛
    check("字符串 published_at 不抛",
          Item(uid="u", published_at="2026-09-05T10:00:00").to_wire()["published_at"]
          == "2026-09-05T10:00:00")

    # 一个坏键不能吞掉整个 raw：raw 里的 chat_raw/group_ids 是游标结算的依据，
    # 丢了会让游标永久停住，表现为「运行成功、0 条产出」的静默断供
    r = _jsonable({"chat_raw": "@x", "group_ids": [1, 2], dt.date(2026, 1, 1): "坏键"})
    check("坏键不影响其它键", r.get("chat_raw") == "@x" and r.get("group_ids") == [1, 2], r)
    circ: dict = {"chat_raw": "@y"}
    circ["loop"] = circ
    check("循环引用只丢那一个键", _jsonable(circ).get("chat_raw") == "@y")
    check("NaN 不写出非法 JSON", json.dumps(_jsonable({"nan": float("nan")})) is not None)

    # from_wire 在队列里绝不能抛：抛在主循环外整张图停摆，抛在里面就是毒消息反复重试
    for bad in [None, "abc", {"media": "abc"}, {"media": [None]}, {"text": None},
                {"uid": 123}, {"tags": "abc"}, {"raw": "x"}, {"buttons": "x"}]:
        try:
            i = Item.from_wire(bad)
            i.fingerprint()
            i.preview()
            check(f"from_wire({str(bad)[:20]}) 不抛", True)
        except Exception as e:
            check(f"from_wire({str(bad)[:20]}) 不抛", False, repr(e))

    # 扇出时两条分支各自还原，共享 meta 会让后跑的覆盖先跑的（clean_text 就是原地写 meta）
    payload = {"uid": "u", "meta": {"plain_text": "原文"}, "raw": {"a": 1}}
    a, b = Item.from_wire(payload), Item.from_wire(payload)
    a.meta["plain_text"] = "被改了"
    check("还原出的 Item 之间不共享 meta", b.meta["plain_text"] == "原文")
    check("raw 也不共享", a.raw is not b.raw)

    check("parse_mode 键不在时退回 html", Item.from_wire({}).parse_mode == "html")
    check("parse_mode 显式 None 保真", Item.from_wire({"parse_mode": None}).parse_mode is None)

    real = Item(uid="tg:@c:1", text="中文", parse_mode="html",
                published_at=dt.datetime(2026, 9, 5, 10, tzinfo=dt.timezone.utc),
                media=[Media(kind="photo", tg_ref={"account_id": 2, "chat": "@c", "msg_id": 1})],
                raw={"chat_raw": "@c", "group_ids": [1]})
    w1 = real.to_wire()
    back = Item.from_wire(w1)
    check("真实形态往返无损", w1 == back.to_wire() and back.raw == real.raw)


async def test_db(tmp: Path) -> None:
    print("数据库")
    db = Database(f"sqlite+aiosqlite:///{tmp / 'a.db'}")
    await db.create_all()
    async with db.session() as s:
        s.add(M.Workflow(id=1, name="t", source_plugin="x"))
        await s.flush()
        s.add(M.GraphMessage(message_id="m1", workflow_id=1, node_id="n1",
                             payload={"text": "原"}, path=["n0"]))
    # JSON 列不包 Mutable 的话，原地修改 SQLAlchemy 检测不到、静默不落库，
    # 而 path.append(node_id) 正是忙循环检测判「真环」的依据
    async with db.session() as s:
        m = (await s.execute(select(M.GraphMessage))).scalar_one()
        m.payload["text"] = "改过"
        m.path.append("n1")
    async with db.session() as s:
        m = (await s.execute(select(M.GraphMessage))).scalar_one()
        check("payload 原地修改落库", m.payload["text"] == "改过", m.payload)
        check("path.append 落库", m.path == ["n0", "n1"], m.path)
        # 队列的出队条件是 visible_at <= 现在，两边时区不一致会直接抛 TypeError
        check("visible_at 可直接与 utcnow() 比较", m.visible_at <= M.utcnow())

    async with db.session() as s:
        s.add(M.GraphMessage(message_id="m2", workflow_id=1, node_id="n1", payload={"t": "中文"}))
    eng = create_engine(f"sqlite:///{tmp / 'a.db'}")
    with eng.connect() as c:
        raw = c.execute(text("SELECT payload FROM graph_messages WHERE message_id='m2'")).scalar()
        check("中文不被转义成 \\uXXXX", "中文" in raw, raw)
    eng.dispose()
    await db.dispose()


async def test_migration(tmp: Path) -> None:
    print("补列迁移")
    src = ROOT / "data" / "teleops.db"
    target = tmp / "b.db"
    if src.exists():
        shutil.copy(src, target)          # 拿真实库的副本试，形状最接近
    else:
        seed = Database(f"sqlite+aiosqlite:///{target}")
        await seed.create_all()
        async with seed.session() as s:
            s.add(M.Workflow(id=1, name="t", source_plugin="x"))
        await seed.dispose()

    # 模拟"模型里新增了一列 + 一个索引"，这两样都是 create_all 自己补不上的
    col = Column("selftest_at", DateTime, nullable=False, default=M.utcnow)
    M.Workflow.__table__.append_column(col)
    idx = Index("ix_selftest_tmp", M.Workflow.__table__.c.enabled)
    try:
        db = Database(f"sqlite+aiosqlite:///{target}")
        await db.create_all()
        await db.dispose()
        eng = create_engine(f"sqlite:///{target}")
        with eng.connect() as c:
            cols = {r[1] for r in c.execute(text("PRAGMA table_info(workflows)"))}
            check("新列真的加上了（不是跳过）", "selftest_at" in cols, sorted(cols))
            check("已有行被回填",
                  c.execute(text("SELECT selftest_at FROM workflows")).scalar() is not None)
            idxs = {r[1] for r in c.execute(text("PRAGMA index_list(workflows)"))}
            check("新索引被补建", "ix_selftest_tmp" in idxs, idxs)
        eng.dispose()
    finally:
        M.Workflow.__table__._columns.remove(col)
        M.Workflow.__table__.indexes.discard(idx)


def main() -> int:
    test_serialization()
    with tempfile.TemporaryDirectory() as d:
        asyncio.run(test_db(Path(d)))
        asyncio.run(test_migration(Path(d)))
    print(f"\n通过 {_ok}，失败 {_fail}")
    return 1 if _fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
