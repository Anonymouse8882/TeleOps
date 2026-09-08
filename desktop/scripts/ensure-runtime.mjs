#!/usr/bin/env node
/** 保证 runtime/ 目录存在——没有内置 Python 时 electron-builder 也不会因为找不到目录而报错。 */
import { existsSync, mkdirSync, readdirSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const RUNTIME = join(dirname(fileURLToPath(import.meta.url)), '..', 'runtime');
mkdirSync(RUNTIME, { recursive: true });

if (!existsSync(join(RUNTIME, 'python'))) {
  writeFileSync(
    join(RUNTIME, 'README.txt'),
    '这里可以放一份独立的 Python（目录名 python），运行 npm run fetch-python 自动下载。\n' +
      '没有的话，TeleOps 启动时会去找系统 Python，并在用户目录里自建虚拟环境。\n'
  );
  console.log('runtime/ 里没有内置 Python：安装包会依赖用户机器上的 Python 3.10+。');
} else {
  console.log(`runtime/python 已就绪（${readdirSync(join(RUNTIME, 'python')).length} 个条目），会打进安装包。`);
}
