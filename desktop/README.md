# TeleOps 桌面版（Electron）

把 TeleOps 包成 Windows / macOS 上双击就能用的桌面应用：Electron 负责拉起
Python 后台（uvicorn），再用一个窗口显示原来那套网页界面。功能和 `python run.py`
完全一样，只是不用开终端、不用记端口。

```
TeleOps.exe / TeleOps.app
   └─ 找 Python ─▶ 缺依赖就自动装 ─▶ 起 uvicorn ─▶ 等 /api/ping 通 ─▶ 开窗口
```

## 开发时运行

```bash
cd desktop
```

```bash
npm install
```

```bash
npm start
```

开发模式下**数据目录就是仓库根目录**，用的还是仓库里的 `config.yaml`、`data/`、
`plugins/` 和 `.venv`，跟直接 `python run.py` 没有区别，可以随时来回切。

## 打包

Windows 包要在 Windows 上打，macOS 包要在 Mac 上打（里面的 Python 依赖带平台
标记，交叉打包装不了 `cryptg` / `lxml` 这类二进制包）。

```bash
npm run build:win
```

```bash
npm run build:mac
```

产物在 `desktop/dist/`：

| 平台 | 产物 |
| --- | --- |
| Windows | `TeleOps-0.1.0-win-x64.exe`（安装版，免管理员）、`TeleOps-0.1.0-portable.exe`（免安装，双击即用） |
| macOS | `TeleOps-0.1.0-mac-arm64.dmg` / `-x64.dmg`，以及对应的 `.zip` |

只想验证能不能跑、不生成安装包：`npm run pack`，结果在 `dist/win-unpacked/`
或 `dist/mac/`。

### 要不要把 Python 一起打进去

默认**不打**：安装包只有几十 MB，第一次启动时去找机器上的 Python 3.10+，
没有依赖就自己在用户目录里建虚拟环境装一遍（界面上有进度，几分钟）。
适合自己用、或者能确定对方装了 Python 的场景。

想让对方机器上什么都不用装：

```bash
npm run fetch-python
```

它会从 [python-build-standalone](https://github.com/astral-sh/python-build-standalone)
下载一份独立 CPython 到 `desktop/runtime/python`，并把 `requirements.txt` 装进去；
之后 `npm run build` 打出来的包自带 Python（体积涨到 200 MB 左右），
用户双击就能用，完全不碰系统环境。

在 Mac 上给 Intel 机器准备一份：`node scripts/fetch-python.mjs --arch=x64`。

### 图标

`build/icon.png`（1024×1024）由 `npm run icon` 生成，electron-builder 会自动
转成 `.ico` / `.icns`。想换成自己的图，直接覆盖这个文件即可。

### 打包时可能碰到的两个坑

1. **`Cannot create symbolic link ... libcrypto.dylib`**
   electron-builder 解压签名工具包时要创建符号链接，Windows 默认账户没这个权限。
   打开「设置 → 系统 → 开发者选项 → 开发人员模式」，或者用管理员身份的终端再打一次。

2. **macOS 上打开提示「已损坏 / 无法验证开发者」**
   包没有签名（`electron-builder.yml` 里 `identity: null`）。自己用的话：
   ```bash
   xattr -dr com.apple.quarantine /Applications/TeleOps.app
   ```
   有开发者证书就把 `identity: null` 删掉，改成正常签名 + 公证。

## 数据存在哪

| 模式 | 目录 |
| --- | --- |
| 开发（`npm start`） | 仓库根目录，和源码方式完全一致 |
| Windows 安装版 | `%APPDATA%\TeleOps\` |
| macOS | `~/Library/Application Support/TeleOps/` |

里面是 `config.yaml`、`data/`（数据库、登录会话、媒体、日志）、`plugins/`、
`.venv`（自动建的虚拟环境）、`browser/`（Electron 自己的缓存）。
首次启动会把内置插件和 `config.example.yaml` 铺进去，**升级不会覆盖你改过的文件**。

菜单里的「打开数据目录」「打开配置文件」直接跳到这里。想换个位置，
启动前设环境变量 `TELEOPS_HOME=D:\TeleOpsData` 即可。

## 几个行为说明

- **端口**：优先用 `config.yaml` 里的 `server.port`（默认 8800）。被别的程序占了就
  自动换一个空闲端口；如果占用它的正好是另一个 TeleOps（比如 `start-background.vbs`
  起的那个），桌面版不会再起一份，而是直接接管界面。
- **关窗口**：Windows / Linux 关窗口 = 退出，后台一起结束；macOS 沿用系统习惯，
  关窗口后应用还在（调度继续跑），点 Dock 图标回来，⌘Q 才真正退出。
- **想让它一直在后台跑**（关掉界面也继续搬运）：桌面版不适合，用仓库里的
  `start-background.vbs` + 开机自启，见主 README。
- **后台崩了**会弹窗提示，可以一键重启或看日志。
- **启动日志**在 `data/desktop.log`（找 Python、装依赖、起服务这一段），
  后台自己的运行日志还是 `data/teleops.log`。

## 换掉自动找到的 Python

启动前设 `TELEOPS_PYTHON` 指到某个解释器即可，例如：

```bash
TELEOPS_PYTHON=/opt/homebrew/bin/python3.12 npm start
```

查找顺序：`TELEOPS_PYTHON` → 内置运行时 → 数据目录里的 `.venv` → 仓库 `.venv`
→ 系统 `python3` / `python` / `py`。

## 目录

```
desktop/
  src/main.js       主进程：启动流程、窗口、菜单、退出时收尾
  src/paths.js      代码目录 vs 数据目录
  src/python.js     找解释器、体检、建虚拟环境装依赖
  src/backend.js    起/停 uvicorn、健康检查、端口选择
  src/seed.js       首次运行铺配置和插件、读端口
  src/log.js        启动日志
  src/splash.html   启动/安装/报错界面
  scripts/          图标生成、下载内置 Python、打包前准备
  electron-builder.yml
```
