#!/usr/bin/env python
"""TeleOps 启动入口。

    python run.py                  # 启动后台（默认 http://127.0.0.1:8800）
    python run.py --port 9000
    python run.py --run 3          # 只跑一次 3 号工作流然后退出（适合放进外部定时器）
    python run.py --list           # 列出已加载的插件
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from teleops.config import load_settings, set_settings  # noqa: E402
from teleops.logging_setup import setup_logging  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(prog="teleops", description="Telegram 频道专业运营工具")
    ap.add_argument("-c", "--config", default=None, help="配置文件路径（默认 config.yaml）")
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--run", type=int, metavar="ID", help="立即执行一次指定工作流后退出")
    ap.add_argument("--dry", action="store_true", help="配合 --run，只预览不发送")
    ap.add_argument("--list", action="store_true", help="列出已加载插件后退出")
    args = ap.parse_args()

    settings = load_settings(args.config)
    set_settings(settings)  # -c 指定的配置也要成为全局单例，否则鉴权会去读默认 config.yaml
    if args.host:
        settings.server.host = args.host
    if args.port:
        settings.server.port = args.port

    if args.list:
        setup_logging(settings)
        from teleops.core.registry import PluginRegistry

        reg = PluginRegistry(settings.plugins.dirs)
        reg.scan(force=True)
        by_type: dict[str, list] = {}
        for lp in reg.list():
            by_type.setdefault(lp.meta.plugin_type, []).append(lp)
        labels = {"source": "信息源", "filter": "过滤规则", "formatter": "格式化", "sink": "输出端"}
        for t, items in by_type.items():
            print(f"\n[{labels.get(t, t)}]")
            for lp in items:
                print(f"  {lp.meta.name:<16} {lp.meta.display_name:<16} v{lp.meta.version}  {lp.meta.file}")
        for e in reg.errors:
            print(f"\n[加载失败] {e.file}\n  {e.message}")
        print()
        return

    if args.run is not None:
        asyncio.run(_run_once(settings, args.run, args.dry))
        return

    import uvicorn

    from teleops.web.app import create_app

    app = create_app(settings)
    print(f"\n  TeleOps 已启动 → http://{settings.server.host}:{settings.server.port}\n")
    uvicorn.run(app, host=settings.server.host, port=settings.server.port, log_config=None)


async def _run_once(settings, workflow_id: int, dry: bool) -> None:
    setup_logging(settings)
    from teleops.core.engine import Engine, set_engine

    engine = Engine(settings)
    set_engine(engine)
    await engine.start()
    try:
        result = await engine.run_workflow(workflow_id, trigger="manual", dry_run=dry)
        print(
            f"结果：{result.status} | 采集 {result.fetched} | 保留 {result.kept} | "
            f"发布 {result.sent} | 失败 {result.failed}"
        )
        if result.error:
            print("错误：" + result.error)
    finally:
        await engine.stop()


if __name__ == "__main__":
    main()
