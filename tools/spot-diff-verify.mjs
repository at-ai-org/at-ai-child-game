#!/usr/bin/env node
/*
 * 找不同——像素级校验（最严格的一关）
 * ------------------------------------------------------------
 * 用 node-canvas 把每一关的 A/B 两版真画出来，逐个差异点验证：
 *   1) 场景绘制函数不抛错（能 catch 掉 undefined 变量、笔误等运行期错误）
 *   2) A/B 两版整图确实不同（差异像素 > 60）
 *   3) 每个差异点周围 34px 内确实有差异像素（> 20）
 *      —— 能抓出「坐标写错位置」「数量差异指到了两张图都有的那个」
 *   4) 【最关键】以声明点为圆心、半径 CORE_R(12px) 内必须有 >= CORE_MIN(25) 个差异像素
 *      —— 第 3 条（34px 内 >= 20）太宽松：只要附近"有一点差异"就放行，
 *         实测漏掉了 34 处错位，例如：
 *           · 货架罐头点在第 2、3 罐中间（偏 50px）
 *           · 药瓶点在第 2、3 瓶中间、烟囱冒烟点在烟柱左边 34px
 *           · 数量类差异点在了"两版都有的那一个"上，而真正消失的是另一个
 *         这些点孩子照着点必然判错，但"附近有差异"这个弱条件全都能通过。
 *         收紧到中心 12px 后，上述 34 处全部被抓出并修正。
 *     这类错位*只有真渲染出来才看得见*，静态检查完全发现不了。
 *
 * 依赖：node-canvas。若未安装：
 *   mkdir -p /tmp/spotverify && cd /tmp/spotverify && npm init -y
 *   npm install canvas --cache /tmp/npmcache-spot
 * 用法：
 *   node tools/spot-diff-verify.mjs [node_modules 路径]
 *   默认从 /tmp/spotverify/node_modules/canvas 加载
 * 退出码：0 = 全部通过；1 = 有问题。
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { dirname, join } from 'node:path';
import vm from 'node:vm';

const __dirname = dirname(fileURLToPath(import.meta.url));
const HTML = join(__dirname, '..', 'spot-difference', 'index.html');
const canvasPath = process.argv[2] || '/tmp/spotverify/node_modules/canvas/index.js';

let createCanvas;
try {
  ({ createCanvas } = await import(pathToFileURL(canvasPath).href));
} catch (e) {
  console.error(`✗ 无法加载 node-canvas（${canvasPath}）：${e.message}`);
  console.error('  请先安装：mkdir -p /tmp/spotverify && cd /tmp/spotverify && npm init -y && npm install canvas --cache /tmp/npmcache-spot');
  process.exit(2);
}

/* 以声明点为圆心，多大范围内必须存在差异像素（核心命中区） */
const CORE_R = 12;
const CORE_MIN = 25;

const html = readFileSync(HTML, 'utf8');
const m = html.match(/<script>\n\/\* 屏蔽右键菜单[\s\S]*?<\/script>/);
if (!m) { console.error('✗ 未找到主游戏脚本'); process.exit(2); }
const js = m[0].replace(/^<script>/, '').replace(/<\/script>$/, '');

/* ---- 用 DOM stub 在 vm 沙箱里跑游戏脚本（只为了拿到 SCENES 和绘制函数） ---- */
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
};
sandbox.globalThis = sandbox;
const ctxObj = vm.createContext(sandbox);
let SCENES, IMG_W, IMG_H;
try {
  vm.runInContext(js + '\n;globalThis.__out = { SCENES, IMG_W, IMG_H };', ctxObj);
  ({ SCENES, IMG_W, IMG_H } = vm.runInContext('__out', ctxObj));
} catch (e) {
  console.error('✗ 游戏脚本执行失败:', e.message);
  process.exit(1);
}
console.log(`像素级校验：共 ${SCENES.length} 关，逐关渲染 A/B 两版对比（判定：点位中心 ${CORE_R}px 内需有 >= ${CORE_MIN} 个差异像素）\n`);

let badLevels = 0, badPoints = 0;
for (let i = 0; i < SCENES.length; i++) {
  const sc = SCENES[i];
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

  let total = 0;
  for (let p = 0; p < da.length; p += 4) if (isDiff(p)) total++;
  if (total < 60) problems.push(`A/B 两版几乎一样（差异像素仅 ${total}）`);

  sc.diffs.forEach((d, k) => {
    const R = 34;
    let hit = 0;
    for (let y = Math.max(0, Math.round(d.y - R)); y < Math.min(IMG_H, Math.round(d.y + R)); y++) {
      for (let x = Math.max(0, Math.round(d.x - R)); x < Math.min(IMG_W, Math.round(d.x + R)); x++) {
        if (isDiff((y * IMG_W + x) * 4)) hit++;
      }
    }
    if (hit < 20) {
      problems.push(`D${k + 1} (${d.x},${d.y}) "${d.desc}" 附近 A/B 几乎无差异（仅 ${hit} 像素）`);
      badPoints++;
      return;
    }
    /* 关键检查：点位中心必须真的落在差异上 */
    let core = 0;
    for (let y = Math.max(0, d.y - CORE_R); y < Math.min(IMG_H, d.y + CORE_R); y++) {
      for (let x = Math.max(0, d.x - CORE_R); x < Math.min(IMG_W, d.x + CORE_R); x++) {
        if (isDiff((y * IMG_W + x) * 4)) core++;
      }
    }
    if (core >= CORE_MIN) return;
    /* 没落在差异上：找一下 80px 内真正该指的地方，方便直接改坐标 */
    let bx = null, by = null, bn = 0;
    for (let y = Math.max(0, d.y - 80); y < Math.min(IMG_H, d.y + 80); y += 2) {
      for (let x = Math.max(0, d.x - 80); x < Math.min(IMG_W, d.x + 80); x += 2) {
        let n = 0;
        for (let yy = Math.max(0, y - CORE_R); yy < Math.min(IMG_H, y + CORE_R); yy++) {
          for (let xx = Math.max(0, x - CORE_R); xx < Math.min(IMG_W, x + CORE_R); xx++) {
            if (isDiff((yy * IMG_W + xx) * 4)) n++;
          }
        }
        if (n > bn) { bn = n; bx = x; by = y; }
      }
    }
    problems.push(
      `D${k + 1} (${d.x},${d.y}) "${d.desc}" 点位中心 ${CORE_R}px 内仅 ${core} 个差异像素` +
      (bx !== null ? ` → 应指向 (${bx},${by})，该处 ${bn} 个` : '，且 80px 内找不到差异'));
    badPoints++;
  });

  if (problems.length) {
    badLevels++;
    console.log(`第${i + 1}关 ${sc.name}`);
    problems.forEach(p => console.log('  ✗ ' + p));
  }
}
console.log(`\n结果：${SCENES.length - badLevels}/${SCENES.length} 关通过，问题差异点 ${badPoints} 处。`);
process.exit(badLevels ? 1 : 0);
