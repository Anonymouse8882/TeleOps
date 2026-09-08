"""插件注册表：扫描目录、动态导入、热重载。

约定：
  plugins/sources/*.py     -> 信息源
  plugins/filters/*.py     -> 过滤规则
  plugins/formatters/*.py  -> 格式化
  plugins/sinks/*.py       -> 输出端
子目录只是为了整理，真正决定类型的是插件类继承自哪个基类。
以 `_` 开头的文件会被忽略。
"""
from __future__ import annotations

import importlib.util
import inspect
import logging
import sys
import traceback
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Any, Iterable, Type

from .plugin import (
    FilterPlugin,
    FormatterPlugin,
    Plugin,
    PluginMeta,
    SinkPlugin,
    SourcePlugin,
)

log = logging.getLogger(__name__)

_BASES: tuple[type[Plugin], ...] = (SourcePlugin, FilterPlugin, FormatterPlugin, SinkPlugin)


@dataclass
class LoadedPlugin:
    cls: Type[Plugin]
    meta: PluginMeta
    mtime: float
    enabled: bool = True
    error: str = ""


@dataclass
class LoadError:
    file: str
    message: str
    traceback: str = ""


@dataclass
class ScanResult:
    added: list[str] = dc_field(default_factory=list)
    updated: list[str] = dc_field(default_factory=list)
    removed: list[str] = dc_field(default_factory=list)
    errors: list[LoadError] = dc_field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.added or self.updated or self.removed)


class PluginRegistry:
    def __init__(self, dirs: Iterable[Path]) -> None:
        self.dirs = [Path(d) for d in dirs]
        self.plugins: dict[str, LoadedPlugin] = {}
        self.errors: list[LoadError] = []
        self._file_index: dict[str, str] = {}  # 文件路径 -> 插件名

    # ------------------------------------------------------------------ 扫描
    def _iter_files(self) -> list[Path]:
        files: list[Path] = []
        for d in self.dirs:
            if not d.exists():
                continue
            for p in sorted(d.rglob("*.py")):
                if p.name.startswith("_") or "__pycache__" in p.parts:
                    continue
                files.append(p)
        return files

    def scan(self, force: bool = False) -> ScanResult:
        """扫描插件目录，按文件 mtime 增量加载。"""
        result = ScanResult()
        self.errors = []
        seen_files: set[str] = set()

        for path in self._iter_files():
            key = str(path.resolve())
            seen_files.add(key)
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue

            existing_name = self._file_index.get(key)
            if (
                not force
                and existing_name
                and existing_name in self.plugins
                and self.plugins[existing_name].mtime == mtime
            ):
                continue

            try:
                loaded = self._load_file(path, mtime)
            except Exception as e:
                msg = f"{type(e).__name__}: {e}"
                log.error("加载插件失败 %s —— %s", path.name, msg)
                self.errors.append(LoadError(file=str(path), message=msg, traceback=traceback.format_exc()))
                result.errors.append(self.errors[-1])
                continue

            if not loaded:
                continue

            for lp in loaded:
                if existing_name and existing_name != lp.meta.name:
                    self.plugins.pop(existing_name, None)
                if lp.meta.name in self.plugins:
                    lp.enabled = self.plugins[lp.meta.name].enabled
                    result.updated.append(lp.meta.name)
                else:
                    result.added.append(lp.meta.name)
                self.plugins[lp.meta.name] = lp
                self._file_index[key] = lp.meta.name

        # 文件被删除 -> 卸载插件
        for key, pname in list(self._file_index.items()):
            if key not in seen_files:
                self._file_index.pop(key, None)
                if self.plugins.pop(pname, None) is not None:
                    result.removed.append(pname)

        if result.changed:
            log.info(
                "插件扫描：新增 %d，更新 %d，移除 %d，错误 %d",
                len(result.added), len(result.updated), len(result.removed), len(result.errors),
            )
        return result

    def _load_file(self, path: Path, mtime: float) -> list[LoadedPlugin]:
        mod_name = "teleops_plugins." + path.stem + "_" + str(abs(hash(str(path.resolve()))) % 10**8)
        spec = importlib.util.spec_from_file_location(mod_name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"无法为 {path} 创建模块规格")
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(mod_name, None)
            raise

        out: list[LoadedPlugin] = []
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if obj in _BASES or obj is Plugin:
                continue
            if not issubclass(obj, Plugin):
                continue
            if obj.__module__ != mod_name:  # 只收本文件定义的类，忽略 import 进来的
                continue
            if inspect.isabstract(obj) or not obj.name:
                continue
            ptype = next((b.plugin_type for b in _BASES if issubclass(obj, b)), "")
            if not ptype:
                continue
            obj.plugin_type = ptype
            rel = str(path)
            try:
                rel = str(path.relative_to(Path.cwd()))
            except ValueError:
                pass
            out.append(
                LoadedPlugin(
                    cls=obj,
                    meta=obj.meta(module=mod_name, file=rel, builtin=path.parent.parent.name == "plugins"),
                    mtime=mtime,
                )
            )
        if not out:
            log.debug("%s 中没有发现插件类", path.name)
        return out

    # ------------------------------------------------------------------ 查询
    def get(self, name: str) -> LoadedPlugin | None:
        return self.plugins.get(name)

    def cls(self, name: str) -> Type[Plugin]:
        lp = self.plugins.get(name)
        if lp is None:
            raise KeyError(f"插件 {name!r} 不存在或未加载")
        if not lp.enabled:
            raise RuntimeError(f"插件 {name!r} 已被禁用")
        return lp.cls

    def create(self, name: str, config: dict[str, Any] | None = None) -> Plugin:
        return self.cls(name)(config or {})

    def list(self, plugin_type: str | None = None) -> list[LoadedPlugin]:
        items = [p for p in self.plugins.values() if not plugin_type or p.meta.plugin_type == plugin_type]
        return sorted(items, key=lambda p: (p.meta.plugin_type, p.meta.name))

    def set_enabled(self, name: str, enabled: bool) -> bool:
        lp = self.plugins.get(name)
        if not lp:
            return False
        lp.enabled = enabled
        return True

    def reload(self, name: str | None = None) -> ScanResult:
        """name 为空则整体强制重载。"""
        if name is None:
            self.plugins.clear()
            self._file_index.clear()
            return self.scan(force=True)
        lp = self.plugins.get(name)
        if lp is None:
            return self.scan(force=True)
        target = Path(lp.meta.file)
        if not target.is_absolute():
            target = Path.cwd() / target
        key = str(target.resolve())
        self._file_index.pop(key, None)
        self.plugins.pop(name, None)
        return self.scan()

    def stats(self) -> dict[str, int]:
        s = {"source": 0, "filter": 0, "formatter": 0, "sink": 0, "disabled": 0, "error": len(self.errors)}
        for p in self.plugins.values():
            s[p.meta.plugin_type] = s.get(p.meta.plugin_type, 0) + 1
            if not p.enabled:
                s["disabled"] += 1
        return s
