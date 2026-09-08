'use strict';
/** 启动过程的日志（data/desktop.log）——后台自己的运行日志在 data/teleops.log。 */
const fs = require('node:fs');
const P = require('./paths');

const MAX_BYTES = 2 * 1024 * 1024;

function append(line) {
  try {
    const stat = fs.existsSync(P.desktopLog) ? fs.statSync(P.desktopLog) : null;
    if (stat && stat.size > MAX_BYTES) fs.writeFileSync(P.desktopLog, '');
    fs.appendFileSync(P.desktopLog, `${new Date().toISOString()} ${line}\n`);
  } catch {
    /* 日志写不进去不影响运行 */
  }
}

module.exports = { append };
