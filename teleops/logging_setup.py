"""日志初始化：控制台 + 滚动文件 + 内存环形缓冲（供后台"概览"实时查看）。"""
from __future__ import annotations

import logging
import logging.handlers
from collections import deque
from typing import Any

from .config import Settings

_RING: deque[dict[str, Any]] = deque(maxlen=1000)

_FMT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
_DATE = "%Y-%m-%d %H:%M:%S"


class RingHandler(logging.Handler):
    """把日志同时塞进内存环形缓冲，前端轮询即可看到实时日志。"""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            _RING.append(
                {
                    "ts": self.formatter.formatTime(record, _DATE) if self.formatter else "",
                    "level": record.levelname,
                    "name": record.name,
                    "msg": record.getMessage(),
                }
            )
        except Exception:  # 日志系统本身绝不能抛
            pass


def recent_logs(limit: int = 200, level: str | None = None) -> list[dict[str, Any]]:
    items = list(_RING)
    if level:
        want = level.upper()
        items = [i for i in items if i["level"] == want]
    return items[-limit:][::-1]


def setup_logging(settings: Settings) -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, settings.logging.level, logging.INFO))
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = logging.Formatter(_FMT, _DATE)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    if settings.logging.file:
        settings.logging.file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            settings.logging.file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        fh.setFormatter(fmt)
        root.addHandler(fh)

    ring = RingHandler()
    ring.setFormatter(fmt)
    root.addHandler(ring)

    # 第三方库降噪
    for noisy in ("telethon", "apscheduler", "httpx", "httpcore", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("telethon.client.updates").setLevel(logging.ERROR)
