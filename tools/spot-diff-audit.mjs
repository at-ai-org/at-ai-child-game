#!/usr/bin/env node
/*
 * 找不同关卡差异点审计脚本
 * ------------------------------------------------------------
 * 从 spot-difference/index.html 里抽出 SCENES 各关的 5 个差异点，
 * 检查每点是否落在「可安全命中」的区域内（留出足够的边缘余量）。
 *
 * 用法：node tools/spot-diff-audit.mjs [margin]
 *   margin 默认 34 —— 差异点到画布四边的安全距离（400x300 图内坐标）。
 * 退出码：0 = 全部合格；1 = 存在需要挪动的点。
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const __dirname = dirname(fileURLToPath(import.meta.url));
const HTML = join(__dirname, '..', 'spot-difference', 'index.html');
const IMG_W = 400, IMG_H = 300;
const margin = Number(process.argv[2] || 34);

const src = readFileSync(HTML, 'utf8');

/* --- 抽取 SCENES 数组字面量并求值（只取纯数据部分） --- */
const start = src.indexOf('const SCENES = [');
if (start < 0) { console.error('✗ 未找到 SCENES 定义'); process.exit(2); }
const arrStart = src.indexOf('[', start);
let depth = 0, end = -1;
for (let i = arrStart; i < src.length; i++) {
  const c = src[i];
  if (c === '[' || c === '{') depth++;
  else if (c === ']' || c === '}') {
    depth--;
    if (depth === 0) { end = i + 1; break; }
  }
}
if (end < 0) { console.error('✗ SCENES 数组括号不闭合'); process.exit(2); }

const body = src.slice(arrStart, end)
  .replace(/\/\/[^\n]*/g, '')                            // 去掉行注释
  .replace(/([{,]\s*)draw(\s*:\s*)[A-Za-z_$][\w$]*/g, '$1draw$2null');  // 只换掉 draw 的值
const literal = body;
let SCENES;
try {
  SCENES = eval('(' + literal + ')');                     // eslint-disable-line no-eval
} catch (e) {
  console.error('✗ SCENES 解析失败：' + e.message);
  process.exit(2);
}

/* --- 逐关审计 --- */
let bad = 0, warn = 0;
console.log(`差异点审计：安全边距 ${margin}px（画布 ${IMG_W}×${IMG_H}）\n`);

for (let i = 0; i < SCENES.length; i++) {
  const sc = SCENES[i];
  const lv = String(i + 1).padStart(2, ' ');
  if (!sc.diffs || sc.diffs.length !== 5) {
    console.log(`第${lv}关 ${sc.name}：⚠ 差异点数量为 ${sc.diffs ? sc.diffs.length : 0}，应为 5`);
    bad++;
    continue;
  }
  const issues = [];
  const seen = [];
  sc.diffs.forEach((d, k) => {
    const edge = Math.min(d.x, d.y, IMG_W - d.x, IMG_H - d.y);
    // 与其它差异点的距离（过近会互相抢命中）
    let near = Infinity;
    seen.forEach(p => { near = Math.min(near, Math.hypot(p.x - d.x, p.y - d.y)); });
    seen.push(d);
    const tags = [];
    if (d.x < margin || d.y < margin || d.x > IMG_W - margin || d.y > IMG_H - margin) {
      tags.push(`边距仅 ${edge.toFixed(0)}px`);
    }
    if (d.x < 0 || d.y < 0 || d.x > IMG_W || d.y > IMG_H) tags.push('坐标越界');
    if (near < 56) tags.push(`与上一处仅隔 ${near.toFixed(0)}px`);
    if (tags.length) issues.push(`  D${k + 1} (${d.x},${d.y}) ${d.desc}：${tags.join('；')}`);
  });
  if (issues.length) {
    bad += issues.length;
    console.log(`第${lv}关 ${sc.name}`);

    console.log(issues.join('\n'));
  } else {
    console.log(`第${lv}关 ${sc.name}  ✓`);
  }
  if (!/draw:\s*[A-Za-z_$][\w$]*/.test(src.slice(arrStart, end))) {
    console.log(`      ⚠ 第${lv}关缺少 draw 函数引用`);
    warn++;
  }
}

console.log(`\n合计：${SCENES.length} 关 / ${SCENES.length * 5} 处差异，问题 ${bad} 处${warn ? `，另有 ${warn} 条提醒` : ''}。`);
process.exit(bad > 0 ? 1 : 0);
