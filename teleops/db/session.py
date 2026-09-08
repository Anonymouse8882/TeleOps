"""异步数据库会话管理。"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ..config import Settings
from .models import Base

log = logging.getLogger(__name__)


class Database:
    def __init__(self, url: str) -> None:
        self.engine = create_async_engine(
            url, echo=False, future=True,
            # 默认的 json.dumps 会把中文转成 \uXXXX，每个字从 3 字节涨到 6 字节。
            # 这是个搬运中文频道的应用，消息 payload 几乎全是中文。
            json_serializer=lambda o: json.dumps(o, ensure_ascii=False),
            json_deserializer=json.loads,
        )
        self.sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False)
        if url.startswith("sqlite"):
            @event.listens_for(self.engine.sync_engine, "connect")
            def _sqlite_pragmas(dbapi_conn: Any, _record: Any) -> None:
                cur = dbapi_conn.cursor()
                try:
                    # SQLite 默认不校验外键，ondelete="CASCADE" 会形同虚设，
                    # 导致删掉工作流后运行记录/游标变成孤儿数据。这里显式打开。
                    cur.execute("PRAGMA foreign_keys=ON")
                    # 读写不再互斥：默认的 delete 日志模式下，一个写事务会挡住
                    # 所有读，图运行时「一边发消息一边刷状态」会频繁撞上。
                    cur.execute("PRAGMA journal_mode=WAL")
                    # 撞上写锁时先等 5 秒再报错，而不是立刻抛 database is locked
                    cur.execute("PRAGMA busy_timeout=5000")
                    # 不降到 NORMAL：runner 是「先发 Telegram、再写 seen_items 去重记录」，
                    # 断电丢掉那条 seen_items 事务的后果是同一条内容下轮重发，频道里出现
                    # 重复贴文且不可回滚。这个应用每 10 分钟才写几十次，FULL 的开销无所谓。
                    cur.execute("PRAGMA synchronous=FULL")
                except Exception as e:  # 只读文件系统等极端情况，不该让应用起不来
                    log.warning("设置 SQLite PRAGMA 失败：%s", e)
                finally:
                    cur.close()

    async def create_all(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        # 迁移单独开一个事务，免得它失败把建表一起回滚。但失败要往外抛：
        # 带着缺列的 schema 继续跑，表现是「启动成功、随后每次查询都 no such column」，
        # 比起不来难查一万倍。
        try:
            async with self.engine.begin() as conn:
                await conn.run_sync(self._migrate)
        except Exception:
            log.exception("数据库迁移失败")
            raise
        log.info("数据库已就绪")

    @staticmethod
    def _migrate(conn: Any) -> None:
        """轻量迁移：给已存在的表补上模型里新增的列和索引。

        没有 alembic，这就是全部的迁移能力。三条原则：
        - 列一定要加上。SQLite 只接受"可空或默认值是常量"的新列，不满足的就退化成
          "先加可空无默认的列，再 UPDATE 回填"，而不是跳过——跳过会让整张表从此
          查不了（mapper 里有那一列，库里没有）。
        - 索引也要补。create_all 对已存在的表整表跳过，新加的 Index 不会被建出来，
          结果是「新装的机器有索引、老机器没有」这种最难查的性能差异。
        - 最后收口校验，还缺列就抛出去让应用起不来。
        """
        from sqlalchemy import inspect

        insp = inspect(conn)
        existing_tables = set(insp.get_table_names())
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name not in have:
                    _add_column(conn, table.name, col)
            have_idx = {i["name"] for i in insp.get_indexes(table.name)}
            for idx in table.indexes:
                if idx.name in have_idx:
                    continue
                try:
                    idx.create(conn)
                    log.info("数据库迁移：%s 新增索引 %s", table.name, idx.name)
                except Exception as e:
                    log.error("数据库迁移：%s 新增索引 %s 失败：%s", table.name, idx.name, e)

        # —— 收口：模型里有、库里没有的列，一个都不能剩 ——
        insp = inspect(conn)
        tables = set(insp.get_table_names())
        missing = [
            f"{t.name}.{c.name}"
            for t in Base.metadata.sorted_tables if t.name in tables
            for c in t.columns if c.name not in {x["name"] for x in insp.get_columns(t.name)}
        ]
        if missing:
            raise RuntimeError("数据库迁移后仍缺少列：" + "、".join(missing))

    async def dispose(self) -> None:
        await self.engine.dispose()

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.sessionmaker() as s:
            try:
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise


def _needs_fallback(ddl: str) -> bool:
    """这条 ADD COLUMN 会不会被 SQLite 拒绝（默认值非常量 / 带默认值的外键列）。

    实测：非空表上 `ADD COLUMN x DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP` 抛
    "Cannot add a column with non-constant default"；`ADD COLUMN x INTEGER NOT NULL
    DEFAULT 0 REFERENCES p(id)` 抛 "Cannot add a REFERENCES column with non-NULL
    default value"。两种都退化成"加可空列再回填"。
    """
    up = ddl.upper()
    if any(k in up for k in ("CURRENT_TIMESTAMP", "CURRENT_DATE", "CURRENT_TIME")):
        return True
    return "REFERENCES" in up and re.search(r"\bDEFAULT\b", up) is not None


def _is_empty_table(conn: Any, name: str) -> bool:
    from sqlalchemy import text as _text

    try:
        return conn.execute(_text(f"SELECT 1 FROM {name} LIMIT 1")).first() is None
    except Exception:
        return False


def _add_column(conn: Any, table: str, col: Any) -> None:
    """给已有表加一列。加不成就抛，让上层的收口校验之前就炸出来。"""
    from sqlalchemy import text
    from sqlalchemy.schema import CreateColumn

    ddl = str(CreateColumn(col).compile(dialect=conn.engine.dialect)).strip()
    literal = _default_literal(col)
    if literal is not None and "DEFAULT" not in ddl.upper():
        ddl += f" DEFAULT {literal}"

    if _needs_fallback(ddl) and not _is_empty_table(conn, table):
        # 退化路径：只加"列名 + 类型"（可空、无默认、不带外键约束），再回填。
        # 这里不用正则去剥 DDL，直接照列定义重新拼，省得被引号和子句坑到。
        bare = f"{col.name} {col.type.compile(dialect=conn.engine.dialect)}"
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {bare}"))
        if literal is not None:
            conn.execute(text(f"UPDATE {table} SET {col.name} = {literal} WHERE {col.name} IS NULL"))
        log.info("数据库迁移：%s 新增列 %s（加为可空并回填，SQLite 不接受该默认值）", table, col.name)
        return

    if literal is None and "NOT NULL" in ddl.upper():
        # SQLite 不允许加"非空且无默认值"的列；退一步允许为空。
        ddl = re.sub(r"\s+NOT\s+NULL", "", ddl, flags=re.IGNORECASE)
    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {ddl}"))
    log.info("数据库迁移：%s 新增列 %s", table, col.name)


def _default_literal(col: Any) -> str | None:
    """把列的 Python 侧默认值转成能写进 DDL 的 SQL 字面量；转不了返回 None。"""
    if col.default is None:
        return None
    arg = col.default.arg
    if callable(arg):
        for call in (lambda: arg(), lambda: arg(None)):  # 有的默认值签名带 context
            try:
                arg = call()
                break
            except TypeError:
                continue
            except Exception:
                return None
        else:
            return None
    if isinstance(arg, bool):
        return "1" if arg else "0"
    if isinstance(arg, (int, float)):
        return str(arg)
    if isinstance(arg, str):
        return "'" + arg.replace("'", "''") + "'"
    if isinstance(arg, (list, dict)):
        return "'" + json.dumps(arg, ensure_ascii=False).replace("'", "''") + "'"
    if isinstance(arg, dt.datetime):
        return "CURRENT_TIMESTAMP"
    return None


_db: Database | None = None


async def init_db(settings: Settings) -> Database:
    global _db
    _db = Database(settings.storage.database_url)
    await _db.create_all()
    return _db


def get_db() -> Database:
    if _db is None:
        raise RuntimeError("数据库尚未初始化")
    return _db


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    async with get_db().session() as s:
        yield s
