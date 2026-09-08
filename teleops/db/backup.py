"""数据库备份。

WAL 模式下 teleops.db 单文件不是完整数据库——一半以上的新数据可能还在
teleops.db-wal 里，只拷主文件会得到一个能正常打开、但缺表缺数据的库，
恢复时不报错、只是安静地少东西。所以一律走 SQLite 的 backup API。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import sqlite3
from pathlib import Path

from ..config import Settings

log = logging.getLogger(__name__)


def db_path(settings: Settings) -> Path:
    url = settings.storage.database_url
    return Path(url.split("///", 1)[-1]) if "///" in url else Path(url)


def _do_backup(src: Path, dest: Path) -> None:
    """先写临时文件、校验通过再改名。

    备份是迁移流程唯一的安全网，它「假成功」比不做更危险：中途失败（磁盘满）
    会留下一个能正常打开、但一张表都没有的文件，名字和成功的备份一模一样，
    用户几周后回滚时挑最新的那个，拿到的是空库。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    try:
        # 只读打开源库：路径写错时不会顺手建一个空库再"备份"出 4KB 空文件
        con = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
        try:
            out = sqlite3.connect(str(tmp))
            try:
                con.backup(out)
            finally:
                out.close()
        finally:
            con.close()

        chk = sqlite3.connect(str(tmp))
        try:
            if chk.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("备份文件完整性校验未通过")
            if chk.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] == 0:
                raise RuntimeError("备份文件里一张表都没有")
        finally:
            chk.close()
        tmp.replace(dest)
    except Exception:
        for leftover in (tmp, Path(str(tmp) + "-journal"), Path(str(tmp) + "-wal")):
            try:
                leftover.unlink(missing_ok=True)
            except OSError:
                pass
        raise


async def backup_database(settings: Settings, tag: str = "") -> Path:
    """快照一份到 data/backups/，返回文件路径。"""
    src = db_path(settings)
    if not src.is_file():
        raise RuntimeError(f"数据库文件不存在，无法备份：{src}")
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    name = f"teleops-{stamp}{'-' + tag if tag else ''}.db"
    dest = settings.storage.data_dir / "backups" / name
    await asyncio.to_thread(_do_backup, src, dest)
    size = dest.stat().st_size if dest.exists() else 0
    log.info("已备份数据库到 %s（%.1f KB）", dest, size / 1024)
    return dest
