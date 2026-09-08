#!/usr/bin/env python3
"""TeleOps 一键 clean 构建 / 部署 / 运行。

一个文件搞定：清干净 → 装环境 → 自检 → 起服务。跨 Windows / macOS / Linux。

macOS / Linux 用 python3（系统上通常没有 python 这个名字），或者直接 ./deploy.py；
Windows 用 python。下面统一写 python3。

    python3 deploy.py                 # 网页版：clean 重建 .venv 后启动 http://127.0.0.1:8800
    python3 deploy.py web --port 9000 # 指定端口
    python3 deploy.py desktop         # 桌面版：再装一遍 Electron 依赖，开窗口
    python3 deploy.py package         # 打安装包（产物在 desktop/dist/）
    python3 deploy.py doctor          # 只体检，不改动任何东西
    python3 deploy.py stop            # 停掉占着端口的旧实例

    python3 deploy.py --latest        # 不按 requirements.txt 的固定版本，装最新依赖
    python3 deploy.py --cn            # 走清华镜像，国内网速快很多

常用开关：
    --fast            跳过 clean，沿用现有 .venv / node_modules（日常改代码用这个）
    --build-only      只构建部署，不启动
    --python PATH     指定用哪个解释器建虚拟环境
    --mirror URL      pip 镜像源，例如 https://pypi.tuna.tsinghua.edu.cn/simple
    --purge-data      连 data/ 一起删（危险：数据库、登录会话、媒体全没，需二次确认）
    --latest          装依赖最新版；--cn / --mirror 换源；--no-kill 不自动清理端口

装依赖时机器上有 uv 就自动用 uv（并行下载 + 缓存，比 pip 快一个数量级）。
启动前端口被旧实例占着会自动结束它，不是本项目的进程则要 --force-kill 才动。

clean 默认只删可再生成的东西：.venv、__pycache__、desktop/node_modules、
desktop/dist。config.yaml 和 data/ 永远不动，除非显式加 --purge-data。
"""
from __future__ import annotations

import argparse
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DESKTOP = ROOT / "desktop"
VENV = ROOT / ".venv"
REQUIREMENTS = ROOT / "requirements.txt"
CONFIG = ROOT / "config.yaml"
CONFIG_EXAMPLE = ROOT / "config.example.yaml"

IS_WIN = os.name == "nt"
# 依赖里 lxml / greenlet / cryptg 都是二进制 wheel，太新的 Python 往往还没有轮子
PY_MIN = (3, 10)
PY_MAX_TESTED = (3, 13)


# --------------------------------------------------------------------------- 输出

class C:
    """终端着色；不是 tty 或设了 NO_COLOR 就自动退化成纯文本。"""

    enabled = sys.stdout.isatty() and not os.environ.get("NO_COLOR")

    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[36m"

    @classmethod
    def p(cls, code: str, text: str) -> str:
        return f"{code}{text}{cls.RESET}" if cls.enabled else text


if IS_WIN and C.enabled:
    os.system("")  # 让老版本 Windows 终端认 ANSI 转义

_step_no = 0


def step(title: str) -> None:
    global _step_no
    _step_no += 1
    print()
    print(C.p(C.BOLD + C.BLUE, f"▶ [{_step_no}] {title}"))


def info(msg: str) -> None:
    print(f"  {msg}")


def dim(msg: str) -> None:
    print(C.p(C.DIM, f"  {msg}"))


def ok(msg: str) -> None:
    print(C.p(C.GREEN, f"  ✓ {msg}"))


def warn(msg: str) -> None:
    print(C.p(C.YELLOW, f"  ! {msg}"))


def die(msg: str, hint: str = "") -> "None":
    print()
    print(C.p(C.BOLD + C.RED, f"✗ {msg}"))
    if hint:
        for line in hint.strip().splitlines():
            print(C.p(C.YELLOW, f"  {line}"))
    print()
    sys.exit(1)


# --------------------------------------------------------------------------- 执行

def run(cmd: list[str], cwd: Path | None = None, what: str = "", env: dict | None = None) -> None:
    """跑一条命令，输出直通终端；失败就带上下文退出。"""
    dim("$ " + " ".join(str(c) for c in cmd))
    # 网慢时给 uv 宽一点的超时，默认 30s 在国内经常还没连上就断了
    full_env = {"UV_HTTP_TIMEOUT": os.environ.get("UV_HTTP_TIMEOUT", "120"),
                **os.environ, **(env or {})}
    try:
        code = subprocess.call([str(c) for c in cmd], cwd=str(cwd or ROOT), env=full_env)
    except FileNotFoundError:
        die(f"找不到命令：{cmd[0]}", f"{what or ''}\n请确认它已安装并在 PATH 里。")
        return
    if code != 0:
        die(f"{what or ' '.join(str(c) for c in cmd)} 失败（退出码 {code}）")


def capture(cmd: list[str], cwd: Path | None = None) -> tuple[int, str]:
    try:
        r = subprocess.run(
            [str(c) for c in cmd], cwd=str(cwd or ROOT),
            capture_output=True, text=True, timeout=60,
        )
        return r.returncode, (r.stdout + r.stderr).strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 127, ""


def which(name: str) -> str | None:
    return shutil.which(name)


# --------------------------------------------------------------------------- 环境

def venv_bin(name: str) -> Path:
    d = VENV / ("Scripts" if IS_WIN else "bin")
    return d / (f"{name}.exe" if IS_WIN else name)


def venv_python() -> Path:
    return venv_bin("python")


def py_version(exe: str | Path) -> tuple[int, int, int] | None:
    code, out = capture([exe, "-c", "import sys;print('%d.%d.%d' % sys.version_info[:3])"])
    if code != 0 or not out:
        return None
    try:
        return tuple(int(x) for x in out.strip().splitlines()[-1].split("."))  # type: ignore[return-value]
    except ValueError:
        return None


def pick_python(explicit: str | None) -> tuple[str, tuple[int, int, int]]:
    """挑一个能装上依赖的解释器：显式指定 > 环境变量 > 3.13..3.10 > 当前 > python3。"""
    candidates: list[str] = []
    if explicit:
        candidates.append(explicit)
    if os.environ.get("TELEOPS_PYTHON"):
        candidates.append(os.environ["TELEOPS_PYTHON"])
    uv = which("uv")
    for minor in range(PY_MAX_TESTED[1], PY_MIN[1] - 1, -1):
        exe = which(f"python3.{minor}")
        if exe:
            candidates.append(exe)
        if uv:
            # 机器上装了 uv 的话，它管的那几个解释器也算数（本仓库的 .venv 就是这么来的）
            code, out = capture([uv, "python", "find", "--system", f"3.{minor}"])
            if code == 0 and out:
                candidates.append(out.splitlines()[-1].strip())
        if IS_WIN and which("py"):
            # Windows 上没有 python3.12 这种名字，问一下 py 启动器要真实路径
            code, out = capture(["py", f"-3.{minor}", "-c", "import sys;print(sys.executable)"])
            if code == 0 and out:
                candidates.append(out.splitlines()[-1].strip())
    candidates.append(sys.executable)
    for name in ("python3", "python"):
        exe = which(name)
        if exe:
            candidates.append(exe)

    fallback: tuple[str, tuple[int, int, int]] | None = None
    seen: set[str] = set()
    for exe in candidates:
        real = os.path.realpath(exe)
        if real in seen:
            continue
        seen.add(real)
        v = py_version(exe)
        if not v:
            continue
        if v[:2] < PY_MIN:
            continue
        if v[:2] <= PY_MAX_TESTED:
            return exe, v
        fallback = fallback or (exe, v)  # 比测试过的更新，先记着

    if fallback:
        exe, v = fallback
        warn(f"只找到 Python {v[0]}.{v[1]}.{v[2]}，比依赖清单验证过的 3.{PY_MAX_TESTED[1]} 新，"
             "lxml / greenlet 这类包可能没有预编译轮子。")
        warn("装不上的话：装一个 3.12，再用 --python 指过去。")
        return exe, v

    die("没找到可用的 Python 3.10+",
        "macOS: brew install python@3.12\n"
        "Windows: https://www.python.org/downloads/ （勾上 Add to PATH）\n"
        "Linux: sudo apt install python3.12 python3.12-venv")
    raise SystemExit(1)


def port_busy(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex((("127.0.0.1" if host in ("0.0.0.0", "") else host), port)) == 0


def port_listeners(port: int) -> list[tuple[int, str]]:
    """谁在监听这个端口 → [(pid, 命令行)]。查不到就返回空表。"""
    pids: list[int] = []
    if IS_WIN:
        _, out = capture(["netstat", "-ano", "-p", "TCP"])
        for line in out.splitlines():
            parts = line.split()
            if (len(parts) >= 5 and parts[0].upper() == "TCP"
                    and parts[1].rsplit(":", 1)[-1] == str(port)
                    and parts[3].upper() == "LISTENING" and parts[4].isdigit()):
                pids.append(int(parts[4]))
    else:
        code, out = capture(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"])
        if code == 0:
            pids = [int(x) for x in out.split() if x.isdigit()]

    owners: list[tuple[int, str]] = []
    for pid in dict.fromkeys(pids):
        if pid in (os.getpid(), os.getppid()):
            continue
        owners.append((pid, process_cmdline(pid)))
    return owners


def process_cmdline(pid: int) -> str:
    if IS_WIN:
        code, out = capture(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"])
        if code == 0 and out.strip():
            return out.strip().splitlines()[-1].replace('"', " ").strip()
        return f"pid {pid}"
    code, out = capture(["ps", "-p", str(pid), "-o", "command="])
    return out.strip() if code == 0 and out.strip() else f"pid {pid}"


def looks_like_teleops(cmd: str) -> bool:
    low = cmd.lower()
    if "deploy.py" in low:  # 别把自己或另一个 deploy.py 认成后台
        return False
    return any(k in low for k in ("run.py", "teleops", "uvicorn"))


def kill_pid(pid: int, hard: bool) -> None:
    if IS_WIN:
        capture(["taskkill", "/PID", str(pid), "/T"] + (["/F"] if hard else []))
        return
    try:
        os.kill(pid, signal.SIGKILL if hard else signal.SIGTERM)
    except ProcessLookupError:
        pass
    except PermissionError:
        warn(f"没权限结束 pid {pid}（可能是别的用户起的）")


def free_port(host: str, port: int, allow_kill: bool, force: bool) -> None:
    """端口被旧进程占着就清掉；不是自家进程则要 --force-kill 才动。"""
    if not port_busy(host, port):
        return

    owners = port_listeners(port)
    warn(f"端口 {port} 已被占用：")
    for pid, cmd in owners:
        info(f"pid {pid}  {cmd[:110]}")
    if not owners:
        die(f"端口 {port} 被占用，而且查不出是哪个进程",
            f"换个端口：./deploy.py web --port {port + 1}")
    if not allow_kill:
        die(f"端口 {port} 被占用（--no-kill 禁止了自动清理）",
            f"换个端口：./deploy.py web --port {port + 1}")

    foreign = [o for o in owners if not looks_like_teleops(o[1])]
    if foreign and not force:
        die(f"端口 {port} 被别的程序占着，没敢动",
            "确认要结束它就加 --force-kill；或者换端口 --port %d" % (port + 1))

    info("先发终止信号 …")
    for pid, _ in owners:
        kill_pid(pid, hard=False)
    for _ in range(20):  # 最多等 5 秒
        if not port_busy(host, port):
            ok(f"旧进程已退出，端口 {port} 已释放")
            return
        time.sleep(0.25)

    warn("没退干净，强制结束 …")
    for pid, _ in owners:
        kill_pid(pid, hard=True)
    for _ in range(12):  # 再等 3 秒
        if not port_busy(host, port):
            ok(f"端口 {port} 已释放")
            return
        time.sleep(0.25)

    die(f"端口 {port} 还是被占着",
        f"手动看看：{'netstat -ano | findstr :%d' % port if IS_WIN else 'lsof -nP -iTCP:%d -sTCP:LISTEN' % port}")


def read_configured_port() -> tuple[str, int]:
    host, port = "127.0.0.1", 8800
    src = CONFIG if CONFIG.exists() else CONFIG_EXAMPLE
    if not src.exists():
        return host, port
    in_server = False
    for raw in src.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        if not line[0].isspace():
            in_server = line.startswith("server:")
            continue
        if not in_server:
            continue
        key, _, val = line.strip().partition(":")
        val = val.split("#")[0].strip().strip('"').strip("'")
        if key == "host" and val:
            host = val
        elif key == "port" and val.isdigit():
            port = int(val)
    return host, port


# --------------------------------------------------------------------------- 步骤

def do_clean(purge_data: bool, desktop: bool) -> None:
    step("清理旧产物")
    targets: list[Path] = [VENV]
    if desktop:
        targets += [DESKTOP / "node_modules", DESKTOP / "dist", DESKTOP / "build" / "cache"]

    for t in targets:
        if t.exists():
            info(f"删除 {t.relative_to(ROOT)} …")
            rmtree(t)
    # __pycache__ 到处都是，单独扫一遍（别进 .venv / node_modules）
    n = 0
    skip = {".venv", ".venv.windows.bak", "node_modules", ".git", "dist", "runtime"}
    for base, dirs, _ in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in skip]  # 剪枝，别去翻 node_modules
        if "__pycache__" in dirs:
            dirs.remove("__pycache__")
            rmtree(Path(base) / "__pycache__")
            n += 1
    if n:
        info(f"删除 {n} 个 __pycache__")

    if purge_data:
        data = ROOT / "data"
        if data.exists():
            print()
            warn(f"即将删除 {data}：数据库、Telegram 登录会话、媒体、日志全部丢失，不可恢复。")
            if input(C.p(C.YELLOW, "  确认请输入 DELETE：")).strip() != "DELETE":
                die("已取消")
            rmtree(data)
            info("data/ 已删除")
    ok("清理完成（config.yaml 与 data/ 未受影响）" if not purge_data else "清理完成")


def _force_rm(func, path, _exc) -> None:
    """Windows 上只读文件删不掉，去掉只读位再删一次。"""
    try:
        os.chmod(path, 0o700)
        func(path)
    except OSError:
        pass


def rmtree(path: Path) -> None:
    # 3.12 起 onerror 换成了 onexc，两个回调签名一致，按版本选一个
    kw = {"onexc": _force_rm} if sys.version_info >= (3, 12) else {"onerror": _force_rm}
    shutil.rmtree(path, **kw)  # type: ignore[arg-type]


def ensure_config() -> None:
    step("准备配置文件")
    if CONFIG.exists():
        ok(f"沿用现有 {CONFIG.name}")
        return
    if not CONFIG_EXAMPLE.exists():
        die("config.example.yaml 缺失，无法生成配置")
    shutil.copyfile(CONFIG_EXAMPLE, CONFIG)
    ok(f"已从 config.example.yaml 生成 {CONFIG.name}")
    warn("首次使用记得填 server.auth_token，并在网页里登录 Telegram。")


def ensure_venv(py_exe: str, version: tuple[int, int, int], mirror: str | None,
                reuse: bool, latest: bool = False) -> None:
    step("构建 Python 环境")
    if reuse and venv_python().exists():
        v = py_version(venv_python())
        if v and v[:2] >= PY_MIN:
            ok(f"沿用现有 .venv（Python {v[0]}.{v[1]}.{v[2]}）")
        else:
            warn("现有 .venv 不可用，重新创建")
            rmtree(VENV)
    if not venv_python().exists():
        info(f"用 {py_exe} (Python {version[0]}.{version[1]}.{version[2]}) 创建 .venv …")
        uv = which("uv")
        if uv:
            # uv venv 不用 bootstrap pip，比 python -m venv 快得多（依赖也走 uv 装）
            run([uv, "venv", "--python", py_exe, str(VENV)], what="创建虚拟环境")
        else:
            run([py_exe, "-m", "venv", str(VENV)], what="创建虚拟环境")
    if not venv_python().exists():
        die("虚拟环境创建后找不到解释器",
            "Debian/Ubuntu 需要先装 python3-venv：sudo apt install python3-venv")

    kind, base = resolve_installer()
    # 同一个镜像地址，uv 和 pip 的参数名不一样
    idx: list[str] = []
    if mirror:
        idx = ["--default-index", mirror] if kind == "uv" else ["--index-url", mirror]
        info(f"镜像源：{mirror}")
    if kind == "pip":
        # 网慢的时候别动不动就超时失败
        idx += ["--timeout", "120", "--retries", "5"]

    if kind == "pip":
        info("升级 pip …")
        run([str(venv_python()), "-m", "pip", "install", "--quiet", "--upgrade",
             "pip", "setuptools", "wheel"] + idx, what="升级 pip")

    if latest:
        pkgs = requirement_names()
        info(f"安装最新版依赖（忽略 requirements.txt 里的 == 固定版本，共 {len(pkgs)} 个包）…")
        warn("最新版可能和代码里的用法不兼容；出问题就去掉 --latest 回到锁定版本。")
        run(base + ["--upgrade"] + pkgs + idx, what="安装依赖")
    else:
        info("安装 requirements.txt …")
        run(base + ["-r", str(REQUIREMENTS)] + idx, what="安装依赖")
    ok("Python 依赖就绪")


def requirement_names() -> list[str]:
    """把 requirements.txt 里的版本号剥掉，只留包名（含 extras），用于 --latest。"""
    names: list[str] = []
    for raw in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = raw.split("#")[0].strip()
        if not line or line.startswith("-"):
            continue
        name = re.split(r"[<>=!~;\s]", line, maxsplit=1)[0].strip()
        if name:
            names.append(name)
    return names


def resolve_installer() -> tuple[str, list[str]]:
    """返回 (类型, install 命令前缀)。

    有 uv 就优先用 uv：并行下载 + 全局缓存，比 pip 快一个数量级，卡在
    "Collecting xxx" 上的等待基本消失。没有 uv 才退回 venv 自带的 pip。
    """
    vpy = str(venv_python())
    uv = which("uv")
    if uv:
        dim("检测到 uv，用它装依赖（比 pip 快很多）")
        return "uv", [uv, "pip", "install", "--python", vpy]

    if capture([vpy, "-m", "pip", "--version"])[0] == 0:
        dim("没装 uv，用 pip 装依赖；想快很多可以先装 uv："
            + ("powershell -c \"irm https://astral.sh/uv/install.ps1 | iex\"" if IS_WIN
               else "curl -LsSf https://astral.sh/uv/install.sh | sh"))
        return "pip", [vpy, "-m", "pip", "install"]

    dim("这个 .venv 里没有 pip，尝试 ensurepip 补上 …")
    capture([vpy, "-m", "ensurepip", "--upgrade"])
    if capture([vpy, "-m", "pip", "--version"])[0] == 0:
        ok("已补装 pip")
        return "pip", [vpy, "-m", "pip", "install"]

    die("虚拟环境里既没有 pip 也没找到 uv",
        "删掉 .venv 重来：不加 --fast 再跑一次")
    raise SystemExit(1)


def verify_backend() -> None:
    step("自检后台")
    code, out = capture([str(venv_python()), "-c",
                         "import fastapi, uvicorn, telethon, sqlalchemy, apscheduler; print('ok')"])
    if code != 0:
        die("核心依赖导入失败", out[-1500:])
    ok("依赖导入正常")

    code, out = capture([str(venv_python()), str(ROOT / "run.py"), "--list"])
    if code != 0:
        die("插件扫描失败（python run.py --list）", out[-1500:])
    # --list 的插件行长这样：两空格缩进 + 名字 + 显示名 + vX.Y.Z + 文件路径
    loaded = re.findall(r"^ {2}\S+\s+.*\sv\d+\.\d+", out, re.M)
    ok(f"插件加载正常（{len(loaded)} 个）")
    if "[加载失败]" in out:
        warn("有插件加载失败，详见 python run.py --list")


def ensure_node() -> tuple[str, str]:
    step("检查 Node 工具链")
    node, npm = which("node"), which("npm")
    if not node or not npm:
        die("桌面版需要 Node.js 18+",
            "https://nodejs.org/ 装完重开一个终端再跑。")
    _, nv = capture([node, "-v"])
    _, npv = capture([npm, "-v"])
    major = int(nv.lstrip("v").split(".")[0] or 0)
    if major < 18:
        die(f"Node 版本太低（{nv}），electron-builder 需要 18+")
    ok(f"node {nv} / npm {npv}")
    return node, npm  # type: ignore[return-value]


def ensure_node_modules(npm: str, reuse: bool) -> None:
    step("构建 Electron 环境")
    mods = DESKTOP / "node_modules"
    if reuse and (mods / "electron").exists():
        ok("沿用现有 node_modules")
        return
    info("安装 desktop 依赖（要下载 Electron 二进制，第一次比较久）…")
    cmd = "ci" if (DESKTOP / "package-lock.json").exists() else "install"
    run([npm, cmd], cwd=DESKTOP, what=f"npm {cmd}")
    ok("Electron 依赖就绪")


# --------------------------------------------------------------------------- 目标

def target_web(args, host: str, port: int) -> None:
    if args.build_only:
        print()
        ok("构建完成。启动命令：")
        info(f"  {venv_python()} run.py")
        return

    step("启动网页版")
    free_port(host, port, allow_kill=not args.no_kill, force=args.force_kill)

    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '') else host}:{port}"
    print()
    print(C.p(C.BOLD + C.GREEN, f"  TeleOps → {url}"))
    print(C.p(C.DIM, "  Ctrl+C 停止；日志见 data/teleops.log"))
    print()
    cmd = [str(venv_python()), str(ROOT / "run.py"), "--host", host, "--port", str(port)]
    try:
        subprocess.call(cmd, cwd=str(ROOT))
    except KeyboardInterrupt:
        pass
    print()
    ok("已停止")


def target_desktop(args, npm: str) -> None:
    if args.build_only:
        print()
        ok("构建完成。启动命令：")
        info("  cd desktop && npm start")
        return
    step("启动桌面版")
    dim("开发模式下用的就是仓库里的 config.yaml / data/ / .venv，和网页版一份数据。")
    print()
    try:
        subprocess.call([npm, "start"], cwd=str(DESKTOP))
    except KeyboardInterrupt:
        pass
    print()
    ok("已退出")


def target_package(args, npm: str) -> None:
    if args.bundle_python:
        step("下载内置 Python（打进安装包）")
        info("装完的包体积会涨到 200MB 左右，但用户机器上不用装 Python。")
        run([npm, "run", "fetch-python"], cwd=DESKTOP, what="fetch-python")

    step("打包桌面安装包")
    system = platform.system()
    script = {"Darwin": "build:mac", "Windows": "build:win"}.get(system, "build")
    warn("安装包不能交叉编译：Windows 包要在 Windows 上打，mac 包要在 Mac 上打。")
    run([npm, "run", script], cwd=DESKTOP, what=f"npm run {script}")

    dist = DESKTOP / "dist"
    arts = sorted(
        (p for p in dist.iterdir() if p.is_file() and p.suffix in {".exe", ".dmg", ".zip", ".AppImage"}),
        key=lambda p: p.stat().st_mtime, reverse=True,
    ) if dist.exists() else []
    print()
    ok(f"打包完成 → {dist}")
    for p in arts:
        info(f"{p.name}  ({p.stat().st_size / 1024 / 1024:.1f} MB)")
    if system == "Darwin":
        dim("未签名的包在别人机器上会提示「已损坏」，让对方执行：")
        dim("  xattr -dr com.apple.quarantine /Applications/TeleOps.app")


def target_stop(args, host: str, port: int) -> None:
    step(f"停止占用 {host}:{port} 的 TeleOps")
    if not port_busy(host, port):
        ok(f"端口 {port} 本来就是空的，没有在跑的实例")
        return
    free_port(host, port, allow_kill=True, force=args.force_kill)


def target_doctor(args) -> None:
    host, port = read_configured_port()
    step("环境体检")
    info(f"系统        {platform.system()} {platform.release()} / {platform.machine()}")
    info(f"仓库        {ROOT}")

    exe, v = pick_python(args.python)
    info(f"构建解释器  {exe}  (Python {v[0]}.{v[1]}.{v[2]})")

    if venv_python().exists():
        vv = py_version(venv_python())
        code, _ = capture([str(venv_python()), "-c", "import fastapi, telethon"])
        state = "依赖完整" if code == 0 else C.p(C.YELLOW, "依赖缺失")
        info(f".venv       Python {'.'.join(map(str, vv or ()))}  {state}")
    else:
        info(f".venv       {C.p(C.YELLOW, '未创建')}")

    node, npm = which("node"), which("npm")
    if node:
        _, nv = capture([node, "-v"])
        mods = "node_modules 就绪" if (DESKTOP / "node_modules" / "electron").exists() else C.p(C.YELLOW, "node_modules 未安装")
        info(f"Node        {nv}  {mods}")
    else:
        info(f"Node        {C.p(C.YELLOW, '未安装（只影响桌面版）')}")

    info(f"config.yaml {'存在' if CONFIG.exists() else C.p(C.YELLOW, '缺失，会从示例生成')}")
    db = ROOT / "data" / "teleops.db"
    info(f"数据库      {'存在（%.1f MB）' % (db.stat().st_size / 1024 / 1024) if db.exists() else '尚未创建'}")
    busy = port_busy(host, port)
    info(f"监听地址    {host}:{port}  {C.p(C.YELLOW, '已被占用') if busy else '空闲'}")
    if busy:
        for pid, cmd in port_listeners(port):
            tag = "本项目" if looks_like_teleops(cmd) else C.p(C.YELLOW, "外部程序")
            info(f"            ├ pid {pid} [{tag}] {cmd[:80]}")
        dim("            清掉它：./deploy.py stop")
    print()
    ok("体检结束，未改动任何文件")


# --------------------------------------------------------------------------- 入口

def main() -> None:
    ap = argparse.ArgumentParser(
        prog="deploy.py",
        description="TeleOps 一键 clean 构建 / 部署 / 运行",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例（macOS / Linux 用 python3 或 ./deploy.py，Windows 用 python）：
  python3 deploy.py                  clean 重建后启动网页版
  python3 deploy.py desktop --fast   沿用现有依赖，直接开桌面版
  python3 deploy.py package          打安装包
  python3 deploy.py doctor           只看环境状态
  python3 deploy.py stop             停掉占着端口的旧实例
  python3 deploy.py --latest --cn    装最新版依赖，走清华镜像
""",
    )
    ap.add_argument("target", nargs="?", default="web",
                    choices=["web", "desktop", "package", "doctor", "stop"],
                    help="web=网页版(默认) desktop=桌面版 package=打安装包 "
                         "doctor=体检 stop=停掉占用端口的旧实例")
    ap.add_argument("--fast", "--no-clean", dest="fast", action="store_true",
                    help="跳过 clean，沿用现有 .venv / node_modules")
    ap.add_argument("--build-only", action="store_true", help="只构建部署，不启动")
    ap.add_argument("--purge-data", action="store_true", help="连 data/ 一起删（需输入 DELETE 确认）")
    ap.add_argument("--python", metavar="PATH", help="指定建 .venv 用的解释器")
    ap.add_argument("--mirror", metavar="URL", help="pip 镜像源")
    ap.add_argument("--cn", action="store_true",
                    help="用清华镜像（等价于 --mirror https://pypi.tuna.tsinghua.edu.cn/simple）")
    ap.add_argument("--latest", action="store_true",
                    help="忽略 requirements.txt 里的固定版本，装各依赖的最新版")
    ap.add_argument("--no-kill", action="store_true",
                    help="端口被占时不自动清理旧进程，直接报错退出")
    ap.add_argument("--force-kill", action="store_true",
                    help="端口被非 TeleOps 程序占用时也照杀不误")
    ap.add_argument("--bundle-python", action="store_true",
                    help="配合 package：把独立 Python 打进安装包")
    ap.add_argument("--host", help="覆盖 config.yaml 里的 server.host")
    ap.add_argument("--port", type=int, help="覆盖 config.yaml 里的 server.port")
    args = ap.parse_args()

    if not REQUIREMENTS.exists():
        die(f"这里不像 TeleOps 仓库根目录（缺 requirements.txt）：{ROOT}")

    t0 = time.time()
    print()
    print(C.p(C.BOLD, f"TeleOps 一键部署 · {args.target}") +
          C.p(C.DIM, "  （--fast 跳过 clean，-h 看全部开关）"))

    if args.cn and not args.mirror:
        args.mirror = "https://pypi.tuna.tsinghua.edu.cn/simple"

    if args.target == "doctor":
        target_doctor(args)
        return

    if args.target == "stop":
        cfg_host, cfg_port = read_configured_port()
        target_stop(args, args.host or cfg_host, args.port or cfg_port)
        return

    wants_desktop = args.target in ("desktop", "package")
    if not args.fast:
        do_clean(args.purge_data, wants_desktop)
    else:
        step("跳过清理（--fast）")
        ok("沿用现有依赖")

    ensure_config()
    py_exe, version = pick_python(args.python)
    ensure_venv(py_exe, version, args.mirror, reuse=args.fast, latest=args.latest)
    verify_backend()

    npm = ""
    if wants_desktop:
        _, npm = ensure_node()
        ensure_node_modules(npm, reuse=args.fast)

    print()
    ok(f"构建部署完成，用时 {time.time() - t0:.0f}s")

    host, port = read_configured_port()
    host = args.host or host
    port = args.port or port

    if args.target == "web":
        target_web(args, host, port)
    elif args.target == "desktop":
        target_desktop(args, npm)
    else:
        target_package(args, npm)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        die("已中断")
