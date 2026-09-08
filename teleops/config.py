"""配置加载：config.yaml -> Settings。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

#: 代码所在目录（仓库根 / 打包后的 resources 目录），只读。
CODE_ROOT = Path(__file__).resolve().parent.parent


def _resolve_home() -> Path:
    """运行数据的根目录。

    默认与代码同目录（源码方式运行时的老行为）；桌面版把程序装在只读位置，
    通过环境变量 ``TELEOPS_HOME`` 把 ``config.yaml`` / ``data`` / ``plugins``
    指到用户可写的目录。
    """
    env = os.environ.get("TELEOPS_HOME", "").strip()
    if not env:
        return CODE_ROOT
    home = Path(env).expanduser().resolve()
    home.mkdir(parents=True, exist_ok=True)
    return home


#: 运行数据根目录，配置里的相对路径都基于它
ROOT = _resolve_home()


def _abs(base: Path, p: str | os.PathLike[str]) -> Path:
    path = Path(p)
    return path if path.is_absolute() else (base / path)


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8800
    auth_token: str = ""


@dataclass
class StorageConfig:
    data_dir: Path = ROOT / "data"
    database_url: str = "sqlite+aiosqlite:///data/teleops.db"
    media_dir: Path = ROOT / "data" / "media"
    media_retention_days: int = 3
    # 图模式的消息与逐跳执行记录保留几天。每一跳都会存一份完整正文，
    # 不清理的话跑起来大约每天上千行。
    graph_retain_days: int = 7


@dataclass
class PluginConfig:
    dirs: list[Path] = field(default_factory=lambda: [ROOT / "plugins"])
    auto_reload: bool = True
    reload_interval: int = 5


@dataclass
class RuntimeConfig:
    send_interval: float = 3.0
    max_items_per_run: int = 20
    run_log_keep: int = 200
    max_flood_wait: int = 600


@dataclass
class LoggingConfig:
    level: str = "INFO"
    file: Path | None = ROOT / "data" / "teleops.log"


@dataclass
class Settings:
    root: Path = ROOT
    server: ServerConfig = field(default_factory=ServerConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    plugins: PluginConfig = field(default_factory=PluginConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def sessions_dir(self) -> Path:
        return self.storage.data_dir / "sessions"

    def ensure_dirs(self) -> None:
        for p in (self.storage.data_dir, self.storage.media_dir, self.sessions_dir):
            p.mkdir(parents=True, exist_ok=True)


def load_settings(path: str | os.PathLike[str] | None = None) -> Settings:
    """读取配置文件；文件不存在时回退到默认值。"""
    cfg_path = Path(path) if path else ROOT / "config.yaml"
    raw: dict[str, Any] = {}
    if cfg_path.exists():
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}

    s = Settings(raw=raw)

    srv = raw.get("server") or {}
    s.server = ServerConfig(
        host=srv.get("host", s.server.host),
        port=int(srv.get("port", s.server.port)),
        auth_token=os.environ.get("TELEOPS_TOKEN", srv.get("auth_token", "") or ""),
    )

    st = raw.get("storage") or {}
    data_dir = _abs(ROOT, st.get("data_dir", "data"))
    db_url = st.get("database_url", s.storage.database_url)
    if db_url.startswith("sqlite") and ":///" in db_url:
        prefix, _, rel = db_url.partition(":///")
        if not Path(rel).is_absolute():
            db_url = f"{prefix}:///{(ROOT / rel).as_posix()}"
    s.storage = StorageConfig(
        data_dir=data_dir,
        database_url=db_url,
        media_dir=_abs(ROOT, st.get("media_dir", data_dir / "media")),
        media_retention_days=int(st.get("media_retention_days", 3)),
        graph_retain_days=int(st.get("graph_retain_days", 7)),
    )

    pl = raw.get("plugins") or {}
    s.plugins = PluginConfig(
        dirs=[_abs(ROOT, d) for d in (pl.get("dirs") or ["plugins"])],
        auto_reload=bool(pl.get("auto_reload", True)),
        reload_interval=int(pl.get("reload_interval", 5)),
    )

    rt = raw.get("runtime") or {}
    s.runtime = RuntimeConfig(
        send_interval=float(rt.get("send_interval", 3.0)),
        max_items_per_run=int(rt.get("max_items_per_run", 20)),
        run_log_keep=int(rt.get("run_log_keep", 200)),
        max_flood_wait=int(rt.get("max_flood_wait", 600)),
    )

    lg = raw.get("logging") or {}
    log_file = lg.get("file", "data/teleops.log")
    s.logging = LoggingConfig(
        level=str(lg.get("level", "INFO")).upper(),
        file=_abs(ROOT, log_file) if log_file else None,
    )

    s.ensure_dirs()
    return s


_settings: Settings | None = None


def set_settings(settings: Settings) -> None:
    """把显式加载好的配置装成全局单例。

    run.py 支持 ``-c other.yaml``，而 require_auth / SettingsDep 是通过
    get_settings() 取配置的；不装进来的话它们会去重新读默认 config.yaml，
    自定义配置里的 auth_token 就形同虚设。
    """
    global _settings
    _settings = settings


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings
