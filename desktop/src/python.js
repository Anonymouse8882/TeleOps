'use strict';
/** Python 解释器的查找、体检和首次安装依赖。 */
const path = require('node:path');
const fs = require('node:fs');
const { spawn } = require('node:child_process');
const P = require('./paths');

const IS_WIN = process.platform === 'win32';
const MIN_VERSION = [3, 10];
/**
 * requirements.txt 里钉死的 lxml / greenlet / cryptg 只到这个版本有预编译轮子。
 * 更新的解释器不是不能用，但要现场编译（greenlet 会直接编译失败、cryptg 还要 Rust），
 * 所以只在实在找不到别的解释器时才拿它装依赖。
 */
const MAX_TESTED = [3, 13];
/** 后台必需的依赖，缺任何一个都要重新安装 requirements.txt。 */
const REQUIRED_MODULES = [
  'fastapi',
  'uvicorn',
  'telethon',
  'sqlalchemy',
  'aiosqlite',
  'apscheduler',
  'yaml',
  'httpx',
  'feedparser',
  'bs4',
];

const PROBE = `
import json, sys
missing = []
for m in ${JSON.stringify(REQUIRED_MODULES)}:
    try:
        __import__(m)
    except Exception:
        missing.append(m)
print("TELEOPS_PROBE" + json.dumps({
    "version": list(sys.version_info[:3]),
    "executable": sys.executable,
    "missing": missing,
}))
`.trim();

/** 执行一条命令，逐行回调输出，返回 {code, output}。 */
function run(cmd, args, opts = {}) {
  const { onLog, cwd, env, timeout } = opts;
  return new Promise((resolve) => {
    let child;
    try {
      child = spawn(cmd, args, {
        cwd,
        env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUNBUFFERED: '1', ...(env || {}) },
        windowsHide: true,
      });
    } catch (err) {
      resolve({ code: -1, output: String(err && err.message ? err.message : err) });
      return;
    }
    let output = '';
    let timer = null;
    const push = (buf) => {
      const text = buf.toString();
      output += text;
      if (onLog) {
        for (const line of text.split(/\r?\n/)) {
          if (line.trim()) onLog(line.trimEnd());
        }
      }
    };
    child.stdout.on('data', push);
    child.stderr.on('data', push);
    child.on('error', (err) => {
      if (timer) clearTimeout(timer);
      resolve({ code: -1, output: output + String(err.message || err) });
    });
    child.on('close', (code) => {
      if (timer) clearTimeout(timer);
      resolve({ code: code == null ? -1 : code, output });
    });
    if (timeout) {
      timer = setTimeout(() => {
        try {
          child.kill();
        } catch {
          /* ignore */
        }
      }, timeout);
    }
  });
}

function venvPython(venvDir) {
  return IS_WIN
    ? path.join(venvDir, 'Scripts', 'python.exe')
    : path.join(venvDir, 'bin', 'python');
}

/** py.exe（Windows 启动器）需要显式指定 -3，其它解释器原样调用。 */
function argsFor(exe, args) {
  return path.basename(exe).toLowerCase() === 'py.exe' ? ['-3', ...args] : args;
}

/** 打包时内置的运行时（scripts/fetch-python.mjs 下载的那份）。 */
function embeddedPython() {
  const exe = IS_WIN
    ? path.join(P.runtimeDir, 'python.exe')
    : path.join(P.runtimeDir, 'bin', 'python3');
  return fs.existsSync(exe) ? exe : null;
}

/** 桌面版自己创建的虚拟环境。 */
function managedVenv() {
  return path.join(P.home, '.venv');
}

/** 按优先级列出所有候选解释器。 */
function candidates() {
  const list = [];
  const push = (exe, kind) => {
    if (exe && !list.some((c) => c.exe === exe)) list.push({ exe, kind });
  };

  if ((process.env.TELEOPS_PYTHON || '').trim()) {
    push(path.resolve(process.env.TELEOPS_PYTHON.trim()), 'env');
  }
  push(embeddedPython(), 'embedded');

  for (const dir of [managedVenv(), path.join(P.repoRoot, '.venv'), path.join(P.pySrc, '.venv')]) {
    const exe = venvPython(dir);
    if (fs.existsSync(exe)) push(exe, 'venv');
  }

  // 带版本号的名字排在裸 python3 前面：裸名可能指向刚发布的版本，编译型依赖还没有轮子。
  // Finder 启动的应用 PATH 里通常没有 homebrew，所以绝对路径也要各列一遍。
  const versioned = [];
  if (!IS_WIN) {
    for (let minor = MAX_TESTED[1]; minor >= MIN_VERSION[1]; minor -= 1) {
      versioned.push(
        `python3.${minor}`,
        `/opt/homebrew/bin/python3.${minor}`,
        `/usr/local/bin/python3.${minor}`
      );
    }
  }
  for (const name of [
    ...versioned,
    ...(IS_WIN
      ? ['python.exe', 'python3.exe', 'py.exe']
      : ['python3', 'python', '/usr/local/bin/python3', '/opt/homebrew/bin/python3']),
  ]) {
    push(name, 'system');
  }
  return list;
}

/** 体检一个解释器：版本 + 缺哪些依赖。 */
async function probe(exe) {
  const { code, output } = await run(exe, argsFor(exe, ['-c', PROBE]), { timeout: 30000 });
  if (code !== 0) return { ok: false, error: output.trim() || `退出码 ${code}` };
  const marker = output.lastIndexOf('TELEOPS_PROBE');
  if (marker < 0) return { ok: false, error: output.trim() || '无法识别的 Python 输出' };
  let info;
  try {
    info = JSON.parse(output.slice(marker + 'TELEOPS_PROBE'.length).trim());
  } catch (err) {
    return { ok: false, error: `解析 Python 输出失败：${err.message}` };
  }
  const [major, minor] = info.version;
  if (major < MIN_VERSION[0] || (major === MIN_VERSION[0] && minor < MIN_VERSION[1])) {
    return { ok: false, error: `需要 Python ${MIN_VERSION.join('.')} 及以上，当前是 ${info.version.join('.')}` };
  }
  return { ok: true, exe, version: info.version.join('.'), parts: [major, minor], missing: info.missing };
}

/**
 * 找一个可用的解释器。
 * 返回 {exe, version, missing, base} —— missing 非空表示还要装依赖，
 * base 是可以用来建虚拟环境的解释器（可能就是 exe 本身）。
 */
function tooNewToBuild([major, minor]) {
  return major > MAX_TESTED[0] || (major === MAX_TESTED[0] && minor > MAX_TESTED[1]);
}

async function resolveInterpreter(onLog) {
  let fallback = null;
  let lastResort = null;
  const problems = [];
  for (const cand of candidates()) {
    const info = await probe(cand.exe);
    if (!info.ok) {
      problems.push(`${cand.exe}: ${info.error.split('\n')[0]}`);
      if (onLog) onLog(`跳过 ${cand.exe}（${info.error.split('\n')[0]}）`);
      continue;
    }
    if (onLog) onLog(`发现 Python ${info.version} → ${cand.exe}`);
    const found = { ...info, kind: cand.kind, base: cand.exe };
    // 依赖齐全就直接用——能跑起来就说明这个版本没问题，不用管它多新。
    if (info.missing.length === 0) return found;
    if (tooNewToBuild(info.parts)) {
      if (onLog) onLog(`  ↳ 比 Python ${MAX_TESTED.join('.')} 新，装依赖要现场编译，先跳过`);
      if (!lastResort) lastResort = found;
      continue;
    }
    if (!fallback) fallback = found;
  }
  if (fallback) return fallback;
  if (lastResort) return lastResort;
  const err = new Error(
    '没有找到可用的 Python 3.10+。\n\n' +
      '请先安装 Python（勾选 “Add python.exe to PATH”），再重新打开 TeleOps。\n\n' +
      (problems.length ? '已尝试：\n' + problems.join('\n') : '')
  );
  err.code = 'NO_PYTHON';
  throw err;
}

/** 首次运行：建虚拟环境并安装 requirements.txt。返回可用的解释器路径。 */
async function bootstrap(base, onLog) {
  const req = path.join(P.pySrc, 'requirements.txt');
  if (!fs.existsSync(req)) throw new Error(`找不到依赖清单：${req}`);

  // 内置运行时是可写的独立目录，直接往里装，不用再套一层 venv。
  const embedded = embeddedPython();
  let target = base;
  if (!embedded || base !== embedded) {
    const venv = managedVenv();
    const exe = venvPython(venv);
    if (!fs.existsSync(exe)) {
      if (onLog) onLog(`创建虚拟环境：${venv}`);
      const r = await run(base, argsFor(base, ['-m', 'venv', venv]), { onLog });
      if (r.code !== 0) throw new Error(`创建虚拟环境失败：\n${r.output.trim()}`);
    }
    target = exe;
  }

  if (onLog) onLog('安装依赖，第一次会久一点（几分钟）…');
  const args = ['-m', 'pip', 'install', '--disable-pip-version-check', '-r', req];
  const r = await run(target, argsFor(target, args), { onLog, cwd: P.pySrc });
  if (r.code !== 0) throw new Error(`依赖安装失败：\n${r.output.trim().slice(-4000)}`);

  const info = await probe(target);
  if (!info.ok) throw new Error(info.error);
  if (info.missing.length) throw new Error(`依赖装完后仍然缺少：${info.missing.join(', ')}`);
  return target;
}

module.exports = {
  run,
  probe,
  argsFor,
  resolveInterpreter,
  bootstrap,
  managedVenv,
  venvPython,
  embeddedPython,
};
