#!/usr/bin/env node
/**
 * 可选步骤：下载一份独立的 CPython 到 desktop/runtime/python，并把
 * requirements.txt 装进去。这样打出来的安装包自带 Python，
 * 用户机器上不需要装任何东西。
 *
 *   node scripts/fetch-python.mjs                # 当前平台/架构
 *   node scripts/fetch-python.mjs --arch=x64     # macOS 上给 Intel 机器准备
 *   node scripts/fetch-python.mjs --version=3.12
 *
 * 注意：里面装的是带平台标记的二进制 wheel（cryptg / lxml 等），
 * 所以 Windows 包要在 Windows 上跑这个脚本，mac 包要在 mac 上跑。
 */
import { existsSync, mkdirSync, rmSync, writeFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const DESKTOP = join(HERE, '..');
const REPO = join(DESKTOP, '..');
const RUNTIME = join(DESKTOP, 'runtime');
const PY_DIR = join(RUNTIME, 'python');
const RELEASES = 'https://api.github.com/repos/astral-sh/python-build-standalone/releases';

const argv = Object.fromEntries(
  process.argv.slice(2).map((a) => {
    const [k, v = 'true'] = a.replace(/^--/, '').split('=');
    return [k, v];
  })
);
const arch = argv.arch || process.arch;
const version = argv.version || '3.12';

const TRIPLES = {
  'win32-x64': 'x86_64-pc-windows-msvc',
  'win32-arm64': 'aarch64-pc-windows-msvc',
  'darwin-arm64': 'aarch64-apple-darwin',
  'darwin-x64': 'x86_64-apple-darwin',
  'linux-x64': 'x86_64-unknown-linux-gnu',
  'linux-arm64': 'aarch64-unknown-linux-gnu',
};
const triple = TRIPLES[`${process.platform}-${arch}`];
if (!triple) {
  console.error(`不支持的平台组合：${process.platform}-${arch}`);
  process.exit(1);
}

const headers = { 'User-Agent': 'teleops-desktop' };
if (process.env.GITHUB_TOKEN) headers.Authorization = `Bearer ${process.env.GITHUB_TOKEN}`;

async function json(url) {
  const res = await fetch(url, { headers });
  if (!res.ok) throw new Error(`${url} → HTTP ${res.status}`);
  return res.json();
}

/** 最新 release 的资产可能有好几百个，要翻页取全。 */
async function listAssets(release) {
  const all = [];
  for (let page = 1; page <= 20; page++) {
    const batch = await json(`${RELEASES}/${release.id}/assets?per_page=100&page=${page}`);
    all.push(...batch);
    if (batch.length < 100) break;
  }
  return all;
}

function sh(cmd, args, opts = {}) {
  console.log(`$ ${cmd} ${args.join(' ')}`);
  const r = spawnSync(cmd, args, { stdio: 'inherit', ...opts });
  if (r.status !== 0) {
    console.error(`命令失败（退出码 ${r.status}）：${cmd}`);
    process.exit(1);
  }
}

const pattern = new RegExp(
  `^cpython-${version.replace('.', '\\.')}\\.\\d+\\+\\d+-${triple}-install_only\\.tar\\.gz$`
);

console.log(`平台 ${process.platform}-${arch} → ${triple}，目标 Python ${version}`);
const release = await json(`${RELEASES}/latest`);
const assets = await listAssets(release);
const asset = assets
  .filter((a) => pattern.test(a.name))
  .sort((a, b) => b.name.localeCompare(a.name, undefined, { numeric: true }))[0];

if (!asset) {
  console.error(`release ${release.tag_name} 里没有匹配 ${pattern} 的文件`);
  process.exit(1);
}

mkdirSync(RUNTIME, { recursive: true });
if (existsSync(PY_DIR)) {
  console.log('清掉旧的 runtime/python');
  rmSync(PY_DIR, { recursive: true, force: true });
}

const archive = join(RUNTIME, asset.name);
console.log(`下载 ${asset.name}（${(asset.size / 1048576).toFixed(1)} MB）…`);
const res = await fetch(asset.browser_download_url, { headers, redirect: 'follow' });
if (!res.ok) {
  console.error(`下载失败：HTTP ${res.status}`);
  process.exit(1);
}
writeFileSync(archive, Buffer.from(await res.arrayBuffer()));

console.log('解压…');
sh('tar', ['-xzf', archive, '-C', RUNTIME]);
rmSync(archive, { force: true });

const exe =
  process.platform === 'win32' ? join(PY_DIR, 'python.exe') : join(PY_DIR, 'bin', 'python3');
if (!existsSync(exe)) {
  console.error(`解压后没找到解释器：${exe}`);
  process.exit(1);
}

console.log('安装 requirements.txt …');
sh(exe, ['-m', 'pip', 'install', '--disable-pip-version-check', '-r', join(REPO, 'requirements.txt')]);

console.log(`\n完成：${PY_DIR}`);
console.log('现在 npm run build 打出来的包会自带这份 Python。');
