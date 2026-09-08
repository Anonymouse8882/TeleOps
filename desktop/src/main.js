'use strict';
/** TeleOps 桌面版主进程：拉起 Python 后台，再用窗口显示它的界面。 */
const path = require('node:path');
const fs = require('node:fs');
const { app, BrowserWindow, Menu, dialog, ipcMain, shell } = require('electron');

const P = require('./paths');
const { append } = require('./log');
const { prepareHome, configuredPort } = require('./seed');
const { resolveInterpreter, bootstrap } = require('./python');
const { Backend } = require('./backend');

// Chromium 自己的缓存挪进 browser/，数据目录里只留 config.yaml / data / plugins
if (P.isPackaged) app.setPath('sessionData', path.join(P.home, 'browser'));

const backend = new Backend();
let splash = null;
let win = null;
let python = null;
let booting = false;
let quitting = false;

/* ---------------------------------------------------------------- 启动界面 */

function setState(state) {
  if (splash && !splash.isDestroyed()) splash.webContents.send('state', state);
}

/** 启动过程的日志：进启动窗口，也写一份到 desktop.log。 */
function log(line) {
  append(line);
  setState({ phase: 'log', line });
}

/** 后台自己的输出：只显示，落盘交给 Backend（起来之后就不再刷 desktop.log）。 */
function showOnly(line) {
  setState({ phase: 'log', line });
}

function createSplash() {
  splash = new BrowserWindow({
    width: 580,
    height: 440,
    resizable: false,
    show: false,
    center: true,
    title: 'TeleOps',
    backgroundColor: '#111827',
    webPreferences: { preload: path.join(__dirname, 'preload.js') },
  });
  splash.removeMenu();
  splash.loadFile(path.join(__dirname, 'splash.html'));
  splash.once('ready-to-show', () => splash.show());
  splash.on('closed', () => {
    splash = null;
    // 后台还没起来就关掉启动窗口 = 放弃启动
    if (!win && !quitting) app.quit();
  });
}

/* ---------------------------------------------------------------- 主窗口 */

const boundsFile = path.join(P.dataDir, 'window.json');

function loadBounds() {
  try {
    const b = JSON.parse(fs.readFileSync(boundsFile, 'utf8'));
    if (b && b.width > 400 && b.height > 300) return b;
  } catch {
    /* 第一次运行没有这个文件 */
  }
  return { width: 1280, height: 860 };
}

function saveBounds() {
  if (!win || win.isDestroyed() || win.isMinimized() || win.isFullScreen()) return;
  try {
    fs.writeFileSync(boundsFile, JSON.stringify(win.getNormalBounds()));
  } catch {
    /* 存不下就算了 */
  }
}

function isExternal(target) {
  try {
    const u = new URL(target);
    return !(u.hostname === '127.0.0.1' && u.port === String(backend.port));
  } catch {
    return false;
  }
}

function openMain(url) {
  const bounds = loadBounds();
  win = new BrowserWindow({
    ...bounds,
    minWidth: 900,
    minHeight: 600,
    show: false,
    title: 'TeleOps',
    backgroundColor: '#0f172a',
    autoHideMenuBar: process.platform !== 'darwin',
    webPreferences: { spellcheck: false },
  });
  win.loadURL(url);
  win.once('ready-to-show', () => {
    win.show();
    if (splash && !splash.isDestroyed()) splash.destroy();
  });
  win.on('close', saveBounds);
  win.on('closed', () => {
    win = null;
  });

  // 站外链接交给系统浏览器，不在应用里开新窗口
  win.webContents.setWindowOpenHandler(({ url: target }) => {
    if (isExternal(target)) shell.openExternal(target);
    return { action: 'deny' };
  });
  win.webContents.on('will-navigate', (e, target) => {
    if (isExternal(target)) {
      e.preventDefault();
      shell.openExternal(target);
    }
  });
}

/* ---------------------------------------------------------------- 启动流程 */

async function boot() {
  if (booting) return;
  booting = true;
  try {
    setState({ phase: 'busy', title: '正在准备数据目录…' });
    prepareHome(log);

    setState({ phase: 'busy', title: '正在查找 Python…' });
    const info = await resolveInterpreter(log);
    python = info.exe;

    if (info.missing.length) {
      setState({
        phase: 'busy',
        title: '首次运行：安装运行依赖',
        detail: '缺少 ' + info.missing.join('、') + '，正在安装（只需要这一次，可能几分钟）',
        showLog: true,
      });
      python = await bootstrap(info.base, log);
    }

    setState({ phase: 'busy', title: '正在启动后台服务…' });
    const url = await backend.start({ python, port: configuredPort(), onLog: showOnly });
    openMain(url);
  } catch (err) {
    const noPython = err && err.code === 'NO_PYTHON';
    if (!splash) createSplash();
    append(`启动失败：${err && err.message ? err.message : err}`);
    setState({
      phase: 'error',
      title: noPython ? '没有找到 Python' : '启动失败',
      detail: String(err && err.message ? err.message : err),
      showLog: true,
      showInstall: Boolean(noPython),
    });
  } finally {
    booting = false;
  }
}

backend.onExit = (reason, tail) => {
  if (quitting || !win) return;
  dialog
    .showMessageBox(win, {
      type: 'error',
      title: 'TeleOps',
      message: '后台服务意外退出',
      detail: (reason + '\n\n' + tail).slice(0, 2000),
      buttons: ['重新启动', '查看日志', '退出'],
      defaultId: 0,
      cancelId: 2,
    })
    .then(async ({ response }) => {
      if (response === 0) await restartBackend();
      else if (response === 1) shell.openPath(P.desktopLog);
      else app.quit();
    });
};

async function restartBackend() {
  await backend.stop();
  try {
    const url = await backend.start({ python, port: configuredPort(), onLog: () => {} });
    if (win && !win.isDestroyed()) win.loadURL(url);
    else openMain(url);
  } catch (err) {
    dialog.showErrorBox('TeleOps', String(err.message || err));
  }
}

/* ---------------------------------------------------------------- 菜单 */

function buildMenu() {
  const isMac = process.platform === 'darwin';
  const appItems = [
    {
      label: '在浏览器中打开',
      accelerator: 'CmdOrCtrl+B',
      click: () => backend.url && shell.openExternal(backend.url),
    },
    { label: '打开数据目录', click: () => shell.openPath(P.home) },
    { label: '打开配置文件 config.yaml', click: () => shell.openPath(P.configFile) },
    { type: 'separator' },
    { label: '重启后台服务', click: () => restartBackend() },
    { label: '查看后台日志', click: () => shell.openPath(P.serverLog) },
    { label: '查看启动日志', click: () => shell.openPath(P.desktopLog) },
  ];

  const template = [
    ...(isMac
      ? [
          {
            label: 'TeleOps',
            submenu: [
              { role: 'about', label: '关于 TeleOps' },
              { type: 'separator' },
              ...appItems,
              { type: 'separator' },
              { role: 'hide', label: '隐藏' },
              { role: 'quit', label: '退出 TeleOps' },
            ],
          },
        ]
      : []),
    {
      label: '文件',
      submenu: isMac
        ? [{ role: 'close', label: '关闭窗口' }]
        : [...appItems, { type: 'separator' }, { role: 'quit', label: '退出' }],
    },
    {
      label: '编辑',
      submenu: [
        { role: 'undo', label: '撤销' },
        { role: 'redo', label: '重做' },
        { type: 'separator' },
        { role: 'cut', label: '剪切' },
        { role: 'copy', label: '复制' },
        { role: 'paste', label: '粘贴' },
        { role: 'selectAll', label: '全选' },
      ],
    },
    {
      label: '视图',
      submenu: [
        { role: 'reload', label: '刷新' },
        { role: 'forceReload', label: '强制刷新' },
        { role: 'toggleDevTools', label: '开发者工具' },
        { type: 'separator' },
        { role: 'resetZoom', label: '实际大小' },
        { role: 'zoomIn', label: '放大' },
        { role: 'zoomOut', label: '缩小' },
        { type: 'separator' },
        { role: 'togglefullscreen', label: '全屏' },
      ],
    },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

/* ---------------------------------------------------------------- 生命周期 */

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', () => {
    const target = win || splash;
    if (target && !target.isDestroyed()) {
      if (target.isMinimized()) target.restore();
      target.focus();
    }
  });

  app.whenReady().then(() => {
    P.ensureDirs();
    buildMenu();
    createSplash();
    boot();

    app.on('activate', () => {
      if (win) win.show();
      else if (backend.url) openMain(backend.url);
      else if (!splash) {
        createSplash();
        boot();
      }
    });
  });

  ipcMain.on('splash-action', (_e, action) => {
    if (action === 'retry') boot();
    else if (action === 'quit') app.quit();
    else if (action === 'open-log') shell.openPath(P.desktopLog);
    else if (action === 'install-python') shell.openExternal('https://www.python.org/downloads/');
  });

  // Windows / Linux 关掉窗口就整体退出；macOS 保持后台运行（调度还得继续跑）
  app.on('window-all-closed', () => {
    if (process.platform !== 'darwin') app.quit();
  });

  app.on('before-quit', (e) => {
    if (quitting || !backend.running) return;
    e.preventDefault();
    quitting = true;
    backend.stop().finally(() => app.quit());
  });
}
