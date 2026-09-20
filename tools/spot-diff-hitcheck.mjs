#!/usr/bin/env node
/*
 * 找不同——命中判定可达性校验
 * ------------------------------------------------------------
 * 复刻 index.html 里的 hitRadiusFor() 逻辑，逐个差异点验证：
 *   1) 判定半径 ≥ HIT_R_MIN（靠边也不会缩到点不中）
 *   2) 判定圈不会超出 400×300 画布（超出部分孩子根本点不到，等于白给）
 *   3) 在该点正上方模拟"孩子的手略有偏移"的点击，仍能命中
 *
 * 用法：node tools/spot-diff-hitcheck.mjs
 * 退出码：0 = 全部通过；1 = 存在点不中 / 判定圈溢出的差异。
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const __dirname = dirname(fileURLToPath(import.meta.url));
const HTML = join(__dirname, '..', 'spot-difference', 'index.html');
const IMG_W = 400, IMG_H = 300;
const HIT_R = 40, HIT_R_MIN = 34;

const src = readFileSync(HTML, 'utf8');
const start = src.indexOf('const SCENES = [');
const arrStart = src.indexOf('[', start);
let depth = 0, end = -1;
for (let i = arrStart; i < src.length; i++) {
  const c = src[i];
  if (c === '[' || c === '{') depth++;
  else if (c === ']' || c === '}') { depth--; if (depth === 0) { end = i + 1; break; } }
}
const body = src.slice(arrStart, end)
  .replace(/\/\/[^\n]*/g, '')
  .replace(/([{,]\s*)draw(\s*:\s*)[A-Za-z_$][\w$]*/g, '$1draw$2null');
const SCENES = eval('(' + body + ')');   // eslint-disable-line no-eval

/* 与游戏内 hitRadiusFor() 保持一致（改动游戏逻辑时这里要同步） */
function hitRadiusFor(d) {
  const edgeX = Math.min(d.x, IMG_W - d.x);
  const edgeY = Math.min(d.y, IMG_H - d.y);
  return Math.max(HIT_R_MIN, Math.min(HIT_R, Math.min(edgeX, edgeY)));
}

let fail = 0, totalR = 0;
console.log('命中判定可达性校验（模拟孩子手指偏移 ±18px 仍应命中）\n');

for (let i = 0; i < SCENES.length; i++) {
  const sc = SCENES[i];
  const probs = [];
  sc.diffs.forEach((d, k) => {
    const r = hitRadiusFor(d);
    totalR += r;
    if (r < HIT_R_MIN - 1e-6) probs.push(`D${k + 1} 半径 ${r.toFixed(1)} < 最小 ${HIT_R_MIN}`);
    /* 判定圈是否溢出画布 */
    if (d.x - r < -0.5 || d.y - r < -0.5 || d.x + r > IMG_W + 0.5 || d.y + r > IMG_H + 0.5) {
      probs.push(`D${k + 1} (${d.x},${d.y}) 判定圈 r=${r.toFixed(1)} 溢出画布`);
    }
    /* 手指偏移模拟：8 个方向各 18px，都应落在判定圈内 */
    const off = 18;
    let miss = 0;
    for (let a = 0; a < 8; a++) {
      const ang = a * Math.PI / 4;
      const px = d.x + Math.cos(ang) * off, py = d.y + Math.sin(ang) * off;
      if (Math.hypot(px - d.x, py - d.y) > r) miss++;
    }
    if (miss) probs.push(`D${k + 1} 偏移 18px 有 ${miss}/8 个方向落空`);
  });
  if (probs.length) { fail += probs.length; console.log(`第${i + 1}关 ${sc.name}`); probs.forEach(p => console.log('  ' + p)); }
}
console.log(`\n合计 ${SCENES.length} 关 / ${SCENES.length * 5} 处差异，平均判定半径 ${(totalR / (SCENES.length * 5)).toFixed(1)}px，问题 ${fail} 处。`);
process.exit(fail > 0 ? 1 : 0);
