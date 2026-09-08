'use strict';
/** 首次运行时把配置和内置插件铺到用户数据目录。 */
const path = require('node:path');
const fs = require('node:fs');
const crypto = require('node:crypto');
const P = require('./paths');

/** 记录每个内置插件"我们上次写下去的内容"，用来区分"用户改过"和"只是旧版本"。 */
const SEED_MANIFEST = () => path.join(P.dataDir, 'plugin-seed.json');

const sha = (file) => crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');

function readManifest() {
  try {
    return JSON.parse(fs.readFileSync(SEED_MANIFEST(), 'utf8'));
  } catch {
    return null; // 没有清单 = 这台机器还是老版本铺的插件
  }
}

function writeManifest(manifest) {
  try {
    fs.mkdirSync(path.dirname(SEED_MANIFEST()), { recursive: true });
    fs.writeFileSync(SEED_MANIFEST(), JSON.stringify(manifest, null, 2));
  } catch {
    /* 写不下清单不影响主流程，下次再说 */
  }
}

function copyIfAbsent(src, dst) {
  if (!fs.existsSync(src) || fs.existsSync(dst)) return false;
  fs.mkdirSync(path.dirname(dst), { recursive: true });
  fs.copyFileSync(src, dst);
  return true;
}

/**
 * 同步内置插件到用户目录。
 *
 * 只有"我们自己铺下去、用户没动过"的文件才会跟着升级覆盖——比对的是清单里
 * 记着的哈希。用户改过的插件一律原样保留，只在日志里说一声。
 *
 * 首次带清单运行时（老版本升上来）没有历史哈希可比，此时先把现有文件另存为
 * .bak-<时间戳> 再覆盖，这样即便用户当初改过也不会丢。
 */
function syncDir(src, dst, manifest, prev, stats, rel = '', skip = new Set(['__pycache__', '.git'])) {
  if (!fs.existsSync(src)) return;
  for (const entry of fs.readdirSync(src, { withFileTypes: true })) {
    if (skip.has(entry.name) || entry.name.endsWith('.pyc')) continue;
    const from = path.join(src, entry.name);
    const to = path.join(dst, entry.name);
    const key = rel ? `${rel}/${entry.name}` : entry.name;

    if (entry.isDirectory()) {
      syncDir(from, to, manifest, prev, stats, key, skip);
      continue;
    }
    if (!fs.existsSync(to)) {
      fs.mkdirSync(path.dirname(to), { recursive: true });
      fs.copyFileSync(from, to);
      manifest[key] = sha(from);
      stats.added += 1;
      continue;
    }

    const here = sha(to);
    const shipped = sha(from);
    manifest[key] = here;
    if (here === shipped) {
      manifest[key] = shipped;
      continue; // 已经是最新的
    }
    if (prev && prev[key] !== undefined && prev[key] !== here) {
      stats.kept.push(key); // 用户改过，不碰
      continue;
    }
    if (!prev) {
      // 没有清单，分不清"用户改过"还是"旧版本"——先备份再升级
      const bak = `${to}.bak-${Date.now()}`;
      try {
        fs.copyFileSync(to, bak);
        stats.backedUp.push(path.basename(bak));
      } catch {
        stats.kept.push(key);
        continue;
      }
    }
    fs.copyFileSync(from, to);
    manifest[key] = shipped;
    stats.updated += 1;
  }
}

/**
 * 准备用户数据目录。开发模式下 home 就是仓库根，什么都不用做。
 * 返回一段可以显示给用户的说明（没做事就返回 null）。
 */
function prepareHome(onLog) {
  P.ensureDirs();
  if (path.resolve(P.home) === path.resolve(P.pySrc)) return null;

  const created = [];
  if (copyIfAbsent(path.join(P.pySrc, 'config.example.yaml'), P.configFile)) {
    created.push('config.yaml');
  }
  const prev = readManifest();
  const manifest = {};
  const stats = { added: 0, updated: 0, kept: [], backedUp: [] };
  syncDir(path.join(P.pySrc, 'plugins'), path.join(P.home, 'plugins'), manifest, prev, stats);
  writeManifest(manifest);

  if (stats.added) created.push(`${stats.added} 个插件文件`);
  if (stats.updated) created.push(`升级 ${stats.updated} 个内置插件`);
  if (onLog) {
    if (stats.backedUp.length) {
      onLog(`升级前已备份原插件：${stats.backedUp.join('、')}`);
    }
    if (stats.kept.length) {
      onLog(`以下插件你改过，保持原样不升级：${stats.kept.join('、')}`);
    }
  }

  if (created.length && onLog) onLog(`已初始化 ${created.join(' / ')} → ${P.home}`);
  return created.length ? created.join(' / ') : null;
}

/** 从 config.yaml 里读端口（不引第三方 YAML 库，够用就行）。 */
function configuredPort(fallback = 8800) {
  try {
    const text = fs.readFileSync(P.configFile, 'utf8');
    const m = text.match(/^\s*server:\s*$([\s\S]*?)(?=^\S|\Z)/m);
    const block = m ? m[1] : text;
    const p = block.match(/^\s+port:\s*(\d+)/m);
    if (p) return Number(p[1]);
  } catch {
    /* 没有配置文件就用默认端口 */
  }
  return fallback;
}

module.exports = { prepareHome, configuredPort };
