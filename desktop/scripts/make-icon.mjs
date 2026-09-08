#!/usr/bin/env node
/**
 * 生成 build/icon.png（1024×1024），electron-builder 会自动转成 .ico / .icns。
 * 纯手写 PNG，不引任何图形库；想换图标直接覆盖 build/icon.png 即可。
 *
 *   node scripts/make-icon.mjs
 */
import { deflateSync } from 'node:zlib';
import { writeFileSync, mkdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const OUT = join(dirname(fileURLToPath(import.meta.url)), '..', 'build', 'icon.png');
const SIZE = 1024;
const SS = 2; // 超采样倍数，用来做抗锯齿
const N = SIZE * SS;

const BG_TOP = [56, 189, 248]; // #38BDF8
const BG_BOTTOM = [3, 105, 161]; // #0369A1
const WING = [255, 255, 255];
const BODY = [214, 240, 255];

// 纸飞机，坐标系 0..100
const wing = [
  [7, 47],
  [95, 9],
  [45, 57],
];
const body = [
  [45, 57],
  [95, 9],
  [58, 92],
  [45, 79],
];

// 往中心缩一点，四周留白，缩略图里不会顶到边
const SHRINK = 0.86;
const fit = (poly) => poly.map(([x, y]) => [50 + (x - 50) * SHRINK, 50 + (y - 50) * SHRINK]);

function inside(poly, x, y) {
  let hit = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const [xi, yi] = poly[i];
    const [xj, yj] = poly[j];
    if (yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) hit = !hit;
  }
  return hit;
}

/** 圆角矩形：角落用圆判断 */
function inRounded(x, y, size, r) {
  const cx = Math.min(Math.max(x, r), size - r);
  const cy = Math.min(Math.max(y, r), size - r);
  const dx = x - cx;
  const dy = y - cy;
  return dx * dx + dy * dy <= r * r;
}

const WING_P = fit(wing);
const BODY_P = fit(body);
const radius = N * 0.22;
const acc = new Float64Array(SIZE * SIZE * 4);

for (let y = 0; y < N; y++) {
  for (let x = 0; x < N; x++) {
    let r = 0;
    let g = 0;
    let b = 0;
    let a = 0;
    if (inRounded(x + 0.5, y + 0.5, N, radius)) {
      const t = (x / N) * 0.35 + (y / N) * 0.65;
      r = BG_TOP[0] + (BG_BOTTOM[0] - BG_TOP[0]) * t;
      g = BG_TOP[1] + (BG_BOTTOM[1] - BG_TOP[1]) * t;
      b = BG_TOP[2] + (BG_BOTTOM[2] - BG_TOP[2]) * t;
      a = 255;

      const px = ((x + 0.5) / N) * 100;
      const py = ((y + 0.5) / N) * 100;
      const mark = inside(WING_P, px, py) ? WING : inside(BODY_P, px, py) ? BODY : null;
      if (mark) [r, g, b] = mark;
    }
    const o = (Math.floor(y / SS) * SIZE + Math.floor(x / SS)) * 4;
    acc[o] += r;
    acc[o + 1] += g;
    acc[o + 2] += b;
    acc[o + 3] += a;
  }
}

// 下采样 + 打包成 PNG 扫描行（每行前面加一个 filter 字节 0）
const samples = SS * SS;
const raw = Buffer.alloc(SIZE * (SIZE * 4 + 1));
let p = 0;
for (let y = 0; y < SIZE; y++) {
  raw[p++] = 0;
  for (let x = 0; x < SIZE; x++) {
    const o = (y * SIZE + x) * 4;
    for (let c = 0; c < 4; c++) raw[p++] = Math.round(acc[o + c] / samples);
  }
}

function chunk(type, data) {
  const len = Buffer.alloc(4);
  len.writeUInt32BE(data.length);
  const payload = Buffer.concat([Buffer.from(type, 'ascii'), data]);
  const crc = Buffer.alloc(4);
  crc.writeUInt32BE(crc32(payload) >>> 0);
  return Buffer.concat([len, payload, crc]);
}

const TABLE = (() => {
  const t = new Int32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    t[n] = c;
  }
  return t;
})();

function crc32(buf) {
  let c = 0xffffffff;
  for (const byte of buf) c = TABLE[(c ^ byte) & 0xff] ^ (c >>> 8);
  return c ^ 0xffffffff;
}

const ihdr = Buffer.alloc(13);
ihdr.writeUInt32BE(SIZE, 0);
ihdr.writeUInt32BE(SIZE, 4);
ihdr[8] = 8; // 位深
ihdr[9] = 6; // RGBA
const png = Buffer.concat([
  Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
  chunk('IHDR', ihdr),
  chunk('IDAT', deflateSync(raw, { level: 9 })),
  chunk('IEND', Buffer.alloc(0)),
]);

mkdirSync(dirname(OUT), { recursive: true });
writeFileSync(OUT, png);
console.log(`图标已生成：${OUT}（${SIZE}×${SIZE}, ${(png.length / 1024).toFixed(1)} KB）`);
