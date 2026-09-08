'use strict';
/** 目录解析：区分「代码目录」和「用户数据目录」。 */
const path = require('node:path');
const fs = require('node:fs');
const { app } = require('electron');

const isPackaged = app.isPackaged;

/** 仓库根目录（开发时就是 TeleOps 根目录）。 */
const repoRoot = path.resolve(__dirname, '..', '..');

/** Python 源码所在目录：打包后是 resources/app_py，开发时就是仓库根。 */
const pySrc = isPackaged ? path.join(process.resourcesPath, 'app_py') : repoRoot;

/** 内置 Python 运行时（可选，见 scripts/fetch-python.mjs）。 */
const runtimeDir = isPackaged
  ? path.join(process.resourcesPath, 'runtime', 'python')
  : path.join(__dirname, '..', 'runtime', 'python');

/**
 * 运行数据目录：config.yaml / data / plugins 都放这里。
 * 开发时沿用仓库根（和 `python run.py` 完全一致），打包后放到用户目录。
 * 可用环境变量 TELEOPS_HOME 覆盖。
 */
const home = (process.env.TELEOPS_HOME || '').trim()
  ? path.resolve(process.env.TELEOPS_HOME.trim())
  : isPackaged
    ? app.getPath('userData')
    : repoRoot;

const dataDir = path.join(home, 'data');
const configFile = path.join(home, 'config.yaml');
const desktopLog = path.join(dataDir, 'desktop.log');
const serverLog = path.join(dataDir, 'teleops.log');

function ensureDirs() {
  fs.mkdirSync(dataDir, { recursive: true });
}

module.exports = {
  isPackaged,
  repoRoot,
  pySrc,
  runtimeDir,
  home,
  dataDir,
  configFile,
  desktopLog,
  serverLog,
  ensureDirs,
};
