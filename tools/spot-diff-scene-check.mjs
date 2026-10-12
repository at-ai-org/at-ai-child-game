#!/usr/bin/env node
/*
 * 找不同——关卡质量校验（像素比对 + 差异聚类）
 * ----------------------------------------------------------------------------
 * 比 spot-diff-verify.mjs 更严格：不只检查「每个声明点位都压在差异上」，
 * 还反向检查「画面上的每一处差异都被某个声明点位覆盖」。
 *
 * 检查项：
 *   1) 场景绘制函数 A/B 两版都能渲染，不抛错
 *   2) 【正向】每个声明点位中心 CORE_R(12px) 内 >= CORE_MIN(25) 个差异像素
 *      —— 点位必须真的压在差异本身上（否则孩子照着点必然判错）
 *   3) 【反向】每一块差异区域（8 连通聚类）都必须有声明点位覆盖
 *      —— 抓「多出来的差异」：画漏了 v 判断、用了 Math.random、
 *         或某个装饰在 A/B 两版画得不一样，都会让画面上出现第 6、第 7 处不同。
 *         孩子找到它们却点不亮，会觉得游戏坏了。只有真渲染才能发现。
 *   4) 5 个差异点在 400×300 图内两两间距 >= 56px、离四边 >= 34px
 *
 * 依赖：node-canvas 或 @napi-rs/canvas（后者有预编译包，无需本地编译）
 *   mkdir -p /tmp/spotverify && cd /tmp/spotverify && npm init -y
 *   npm install @napi-rs/canvas --cache /tmp/npmcache-spot
 *
 * 用法：
 *   node tools/spot-diff-scene-check.mjs                       # 校验 index.html 里的正式关卡
 *   node tools/spot-diff-scene-check.mjs path/to/scenes.js     # 校验独立场景文件（草稿）
 *     独立场景文件在同一 vm 上下文里执行，因此可以直接调用游戏里的
 *     bgFill / cloudAt / groundAt / basketAt / ballAt / drawStar / scatterStars
 *     等 helper，只需把场景数组赋值给 EXTRA_SCENES（或 globalThis.__SCENES__）。
 *   node tools/spot-diff-scene-check.mjs [场景文件] [canvas 模块路径]
 * 退出码：0 = 全部通过；1 = 有问题；2 = 环境问题。
 */
import { readFileSync, existsSync } from 'node:fs';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { dirname, join } from 'node:path';
import vm from 'node:vm';

const __dirname = dirname(fileURLToPath(import.meta.url));
const HTML = join(__dirname, '..', 'spot-difference', 'index.html');

/* 点位中心必须真有差异像素的半径 / 最少像素数 */
const CORE_R = 12;
const CORE_MIN = 25;
/* 反向检查：差异簇面积达到这个值才算“一处可见的不同”，必须被点位覆盖 */
const STRAY_MIN_AREA = 15;
/* 差异簇被视作“已覆盖”的容差：点位落在簇包围盒外扩多少像素内 */
const COVER_PAD = 26;

const argScene = process.argv[2] && !/^-/.test(process.argv[2]) ? process.argv[2] : null;
const argCanvas = process.argv[3] || null;

const CANVAS_CANDIDATES = [
  argCanvas,
  process.env.SPOT_CANVAS,
  '/tmp/spotverify/node_modules/canvas/index.js',
  '/tmp/spotverify/node_modules/@napi-rs/canvas/index.js',
  join(__dirname, '..', 'node_modules', 'canvas', 'index.js'),
  join(__dirname, '..', 'node_modules', '@napi-rs', 'canvas', 'index.js'),
].filter(p => p && existsSync(p));

if (!CANVAS_CANDIDATES.length) {
  console.error('✗ 找不到可用的 canvas 模块（node-canvas 或 @napi-rs/canvas）');
  console.error('  请先安装：mkdir -p /tmp/spotverify && cd /tmp/spotverify && npm init -y');
  console.error('           npm install @napi-rs/canvas --cache /tmp/npmcache-spot');
  console.error('  或用环境变量指定：SPOT_CANVAS=/path/to/canvas/index.js');
  process.exit(2);
}

let createCanvas = null, usedCanvas = '';
for (const p of CANVAS_CANDIDATES) {
  try {
    ({ createCanvas } = await import(pathToFileURL(p).href));
    if (createCanvas) { usedCanvas = p; break; }
  } catch (e) { /* 试下一个 */ }
}
if (!createCanvas) {
  console.error(`✗ canvas 模块都加载失败，最后尝试：${CANVAS_CANDIDATES.join(', ')}`);
  process.exit(2);
}

/* ---- 用 DOM stub 在 vm 沙箱里跑游戏脚本（拿到 helper / SCENES / 常量） ---- */
const html = readFileSync(HTML, 'utf8');
const m = html.match(/<script>\n\/\* 屏蔽右键菜单[\s\S]*?<\/script>/);
if (!m) { console.error('✗ 未找到主游戏脚本'); process.exit(2); }
const js = m[0].replace(/^<script>/, '').replace(/<\/script>$/, '');

const elStub = () => ({
  textContent: '', innerHTML: '', style: {},
  classList: { add() {}, remove() {} },
  addEventListener() {},
  getContext: () => createCanvas(400, 300).getContext('2d'),
  getBoundingClientRect: () => ({ left: 0, top: 0 }),
  width: 400, height: 300,
});
const sandbox = {
  document: { getElementById: () => elStub() },
  window: { innerWidth: 1280, innerHeight: 800, addEventListener() {}, performance: { now: () => 0 }, speechSynthesis: null },
  requestAnimationFrame() {},
  performance: { now: () => 0 },
  console,
};
sandbox.globalThis = sandbox;
const ctxObj = vm.createContext(sandbox);

let scenes, IMG_W, IMG_H;
try {
  vm.runInContext(js, ctxObj);
  if (argScene) {
    const extraSrc = readFileSync(argScene, 'utf8');
    vm.runInContext(extraSrc, ctxObj, { filename: argScene });
    scenes = vm.runInContext(
      'typeof EXTRA_SCENES !== "undefined" ? EXTRA_SCENES : globalThis.__SCENES__', ctxObj);
  } else {
    scenes = vm.runInContext('SCENES', ctxObj);
  }
  ({ IMG_W, IMG_H } = vm.runInContext('({ IMG_W, IMG_H })', ctxObj));
} catch (e) {
  console.error('✗ 脚本执行失败:', e && e.stack ? e.stack.split('\n').slice(0, 4).join('\n') : e);
  process.exit(1);
}
if (!Array.isArray(scenes)) { console.error('✗ 没拿到场景数组'); process.exit(2); }

console.log(`关卡质量校验（canvas: ${usedCanvas}）`);
console.log(`来源：${argScene || HTML}`);
console.log(`共 ${scenes.length} 关 · 正向：点位中心 ${CORE_R}px 内 >= ${CORE_MIN} 差异像素 · ` +
            `反向：>= ${STRAY_MIN_AREA} 像素的差异块都必须被点位覆盖\n`);

/* 8 连通聚类：返回 {area, minX, minY, maxX, maxY, cx, cy} 列表 */
function clusters(mask, w, h) {
  const seen = new Uint8Array(w * h);
  const out = [];
  const stack = [];
  for (let i = 0; i < mask.length; i++) {
    if (!mask[i] || seen[i]) continue;
    seen[i] = 1; stack.length = 0; stack.push(i);
    let area = 0, minX = w, minY = h, maxX = -1, maxY = -1, sx = 0, sy = 0;
    while (stack.length) {
      const p = stack.pop();
      const x = p % w, y = (p - x) / w;
      area++; sx += x; sy += y;
      if (x < minX) minX = x; if (x > maxX) maxX = x;
      if (y < minY) minY = y; if (y > maxY) maxY = y;
      for (let dy = -1; dy <= 1; dy++) {
        for (let dx = -1; dx <= 1; dx++) {
          const nx = x + dx, ny = y + dy;
          if (nx < 0 || ny < 0 || nx >= w || ny >= h) continue;
          const q = ny * w + nx;
          if (mask[q] && !seen[q]) { seen[q] = 1; stack.push(q); }
        }
      }
    }
    out.push({ area, minX, minY, maxX, maxY, cx: sx / area, cy: sy / area });
  }
  return out;
}

const distToBox = (px, py, c) => {
  const dx = Math.max(c.minX - px, 0, px - c.maxX);
  const dy = Math.max(c.minY - py, 0, py - c.maxY);
  return Math.hypot(dx, dy);
};

let badLevels = 0, badPoints = 0, strayTotal = 0;
for (let i = 0; i < scenes.length; i++) {
  const sc = scenes[i];
  const problems = [];
  let cA, cB;
  try {
    cA = createCanvas(IMG_W, IMG_H); cB = createCanvas(IMG_W, IMG_H);
    sc.draw(cA.getContext('2d'), 'A');
    sc.draw(cB.getContext('2d'), 'B');
  } catch (e) {
    console.log(`第${i + 1}关 ${sc.name}：✗ 渲染抛错 -> ${e.message}`);
    badLevels++; badPoints++;
    continue;
  }
  const da = cA.getContext('2d').getImageData(0, 0, IMG_W, IMG_H).data;
  const db = cB.getContext('2d').getImageData(0, 0, IMG_W, IMG_H).data;
  const isDiff = p =>
    Math.abs(da[p] - db[p]) + Math.abs(da[p + 1] - db[p + 1]) + Math.abs(da[p + 2] - db[p + 2]) > 24;

  const mask = new Uint8Array(IMG_W * IMG_H);
  let total = 0;
  for (let n = 0, p = 0; n < mask.length; n++, p += 4) {
    if (isDiff(p)) { mask[n] = 1; total++; }
  }
  if (total < 60) problems.push(`A/B 两版几乎一样（差异像素仅 ${total}）`);

  if (!sc.diffs || sc.diffs.length !== 5) {
    problems.push(`差异点数量为 ${sc.diffs ? sc.diffs.length : 0}，应为 5`);
  }

  (sc.diffs || []).forEach((d, k) => {
    /* ---- 正向：点位中心必须有差异 ---- */
    let core = 0;
    for (let y = Math.max(0, d.y - CORE_R); y < Math.min(IMG_H, d.y + CORE_R); y++) {
      for (let x = Math.max(0, d.x - CORE_R); x < Math.min(IMG_W, d.x + CORE_R); x++) {
        if (mask[y * IMG_W + x]) core++;
      }
    }
    if (core < CORE_MIN) {
      /* 找一下附近真正该指的地方，直接给出可用的新坐标 */
      let bx = null, by = null, bn = 0;
      for (let y = Math.max(0, d.y - 80); y < Math.min(IMG_H, d.y + 80); y += 2) {
        for (let x = Math.max(0, d.x - 80); x < Math.min(IMG_W, d.x + 80); x += 2) {
          let n = 0;
          for (let yy = Math.max(0, y - CORE_R); yy < Math.min(IMG_H, y + CORE_R); yy++) {
            for (let xx = Math.max(0, x - CORE_R); xx < Math.min(IMG_W, x + CORE_R); xx++) {
              if (mask[yy * IMG_W + xx]) n++;
            }
          }
          if (n > bn) { bn = n; bx = x; by = y; }
        }
      }
      problems.push(`D${k + 1} (${d.x},${d.y}) "${d.desc}" 点位中心仅 ${core} 个差异像素` +
        (bx !== null ? ` → 建议改为 (${bx},${by})，该处 ${bn} 个` : '，且 80px 内找不到差异'));
      badPoints++;
    }
    /* ---- 边距 / 间距 ---- */
    if (d.x < 34 || d.y < 34 || d.x > IMG_W - 34 || d.y > IMG_H - 34) {
      problems.push(`D${k + 1} (${d.x},${d.y}) "${d.desc}" 离画布边太近（<34px），幼儿点不准`);
      badPoints++;
    }
    for (let j = 0; j < k; j++) {
      const gap = Math.hypot(sc.diffs[j].x - d.x, sc.diffs[j].y - d.y);
      if (gap < 56) {
        problems.push(`D${k + 1} 与 D${j + 1} 只隔 ${gap.toFixed(0)}px，判定圈会互相抢命中`);
        badPoints++;
      }
    }
  });

  /* ---- 反向：每一块差异都必须被某个点位覆盖 ---- */
  const cs = clusters(mask, IMG_W, IMG_H).filter(c => c.area >= STRAY_MIN_AREA);
  for (const c of cs) {
    const covered = (sc.diffs || []).some(d => {
      const inBox = d.x >= c.minX - COVER_PAD && d.x <= c.maxX + COVER_PAD &&
                    d.y >= c.minY - COVER_PAD && d.y <= c.maxY + COVER_PAD;
      return inBox || Math.hypot(d.x - c.cx, d.y - c.cy) < 55;
    });
    if (!covered) {
      strayTotal++;
      problems.push(
        `多出一处未声明的差异：面积 ${c.area}px，中心约 (${Math.round(c.cx)},${Math.round(c.cy)})` +
        `，范围 x[${c.minX}..${c.maxX}] y[${c.minY}..${c.maxY}]` +
        ` —— 要么补一个差异点，要么让 A/B 两版在这里画得一样`);
    }
  }

  if (problems.length) {
    badLevels++;
    console.log(`第${i + 1}关 ${sc.name}`);
    problems.forEach(p => console.log('  ✗ ' + p));
  }
}
console.log(`\n结果：${scenes.length - badLevels}/${scenes.length} 关通过；` +
            `点位问题 ${badPoints} 处，未覆盖的多余差异 ${strayTotal} 块。`);
process.exit(badLevels ? 1 : 0);
