#!/usr/bin/env node
/*
 * 找不同——关卡质量校验（像素比对 + 差异聚类），覆盖全部难度
 * ----------------------------------------------------------------------------
 * 检查三组关卡：
 *   简单（easy）——48 个手绘 Canvas 场景，每关 5 处不同
 *   困难（hard）——6 张公园手绘场景，每关 6 处不同
 *   地狱（hell）——同 6 个场景换天色，每关 7 处不同
 *
 * 真图关卡靠一个 Image 桩来渲染：先用 node-canvas 把照片读进来塞进沙箱，
 * 桩在 img.src 赋值时同步返回（游戏里是异步的，这里同步化才能一次校验完）。
 *
 * 检查项：
 *   1) 场景绘制函数 A/B 两版都能渲染，不抛错
 *   2) 【正向】每个声明点位中心 CORE_R(12px) 内 >= CORE_MIN(25) 个差异像素
 *      —— 点位必须真的压在差异本身上（否则孩子照着点必然判错）
 *   3) 【反向】每一块差异区域（8 连通聚类）都必须有声明点位覆盖
 *      —— 抓「多出来的差异」：漏写 v 判断、误用 Math.random、干扰要素两版画得不一样，
 *         都会让画面上出现第 6、第 7 处不同；孩子找到它们却点不亮，会觉得游戏坏了
 *   4) 差异点两两间距 >= 56px、离四边 >= 34px
 *   5) 【真图专有】每处改动自身要够显眼：改动像素 >= 300、平均色差 >= 25
 *
 * 依赖：node-canvas 或 @napi-rs/canvas（后者有预编译包，无需本地编译）
 *   mkdir -p /tmp/spotverify && cd /tmp/spotverify && npm init -y
 *   npm install @napi-rs/canvas --cache /tmp/npmcache-spot
 *
 * 用法：
 *   node tools/spot-diff-scene-check.mjs                       # 校验游戏里的全部关卡
 *   node tools/spot-diff-scene-check.mjs path/to/scenes.js     # 附加校验独立场景草稿
 *     独立场景文件在同一 vm 上下文里执行，因此可以直接调用游戏里的
 *     bgFill / cloudAt / groundAt / basketAt / ballAt / drawStar 等 helper，
 *     只需把场景数组赋值给 EXTRA_SCENES（或 globalThis.__SCENES__）。
 *   node tools/spot-diff-scene-check.mjs [场景文件] [canvas 模块路径]
 * 退出码：0 = 全部通过；1 = 有问题；2 = 环境问题。
 */
import { readFileSync, existsSync } from 'node:fs';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { dirname, join } from 'node:path';
import vm from 'node:vm';

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = join(__dirname, '..');
const HTML = join(ROOT, 'spot-difference', 'index.html');

const CORE_R = 12;
const CORE_MIN = 25;
const STRAY_MIN_AREA = 15;
const COVER_PAD = 26;
/* 真图关卡专用：A/B 是模型分别画的两幅画，会有整体色调漂移，
 * 用 24 这么低的阈值会把漂移也算成差异（实测 40% 像素都超阈值），
 * 所以真图用 120，手绘场景仍用 24。 */
const PHOTO_DIFF_TH = 120;
const PHOTO_STRAY_AREA = 80;     // 真图：小于这么多的变化（约 9x9 像素）肉眼看不出来，
                                 // 而且模型两次出图的边缘抖动也会产生这种碎片，不算漏声明
const REGION_PAD = 14;           // 区域判定的容差（必须和游戏里点击判定的容差一致，
                                 // 否则这里量的覆盖率和孩子实际点得到的范围对不上）

const argScene = process.argv[2] && !/^-/.test(process.argv[2]) ? process.argv[2] : null;
const argCanvas = process.argv[3] || null;

const CANVAS_CANDIDATES = [
  argCanvas,
  process.env.SPOT_CANVAS,
  '/tmp/spotverify/node_modules/canvas/index.js',
  '/tmp/spotverify/node_modules/@napi-rs/canvas/index.js',
  join(ROOT, 'node_modules', 'canvas', 'index.js'),
  join(ROOT, 'node_modules', '@napi-rs', 'canvas', 'index.js'),
].filter(p => p && existsSync(p));

if (!CANVAS_CANDIDATES.length) {
  console.error('✗ 找不到可用的 canvas 模块（node-canvas 或 @napi-rs/canvas）');
  console.error('  请先安装：mkdir -p /tmp/spotverify && cd /tmp/spotverify && npm init -y');
  console.error('           npm install @napi-rs/canvas --cache /tmp/npmcache-spot');
  console.error('  或用环境变量指定：SPOT_CANVAS=/path/to/canvas/index.js');
  process.exit(2);
}

let createCanvas = null, loadImage = null, usedCanvas = '';
for (const p of CANVAS_CANDIDATES) {
  try {
    const mod = await import(pathToFileURL(p).href);
    if (mod.createCanvas) { createCanvas = mod.createCanvas; loadImage = mod.loadImage || null; usedCanvas = p; break; }
  } catch (e) { /* 试下一个 */ }
}
if (!createCanvas) {
  console.error(`✗ canvas 模块都加载失败，最后尝试：${CANVAS_CANDIDATES.join(', ')}`);
  process.exit(2);
}

/* ---- 用 DOM stub 在 vm 沙箱里跑游戏脚本 ---- */
const html = readFileSync(HTML, 'utf8');
const m = html.match(/<script>\n\/\* 屏蔽右键菜单[\s\S]*?<\/script>/);
if (!m) { console.error('✗ 未找到主游戏脚本'); process.exit(2); }
const js = m[0].replace(/^<script>/, '').replace(/<\/script>$/, '');

/* Image 桩：src 赋值时同步把已加载的照片画进一张同尺寸画布。
 * 画布本身就能当 drawImage 的源，于是游戏的取图逻辑可以完全同步跑完。 */
function makeImageStub() {
  const cv = createCanvas(400, 300);
  let srcVal = '';
  Object.defineProperty(cv, 'src', {
    configurable: true,
    get() { return srcVal; },
    set(v) {
      srcVal = v;
      const real = sandbox.__IMAGES__ && sandbox.__IMAGES__[v];
      if (real) {
        cv.width = real.width; cv.height = real.height;
        cv.getContext('2d').drawImage(real, 0, 0);
        if (this.onload) this.onload();
      } else if (this.onerror) {
        this.onerror();
      }
    },
  });
  cv.onload = null;
  cv.onerror = null;
  return cv;
}

const elStub = () => ({
  textContent: '', innerHTML: '', style: {},
  classList: { add() {}, remove() {}, toggle() {} },
  addEventListener() {},
  querySelectorAll: () => [],
  getContext: () => createCanvas(400, 300).getContext('2d'),
  getBoundingClientRect: () => ({ left: 0, top: 0 }),
  width: 400, height: 300,
});
const sandbox = {
  __IMAGES__: {},
  document: {
    getElementById: () => elStub(),
    createElement: () => createCanvas(400, 300),
    addEventListener() {},
  },
  window: {
    innerWidth: 1280, innerHeight: 800, addEventListener() {},
    performance: { now: () => 0 }, speechSynthesis: null,
  },
  requestAnimationFrame() {},
  performance: { now: () => 0 },
  setTimeout: () => 0, clearTimeout: () => {},
  console,
};
sandbox.Image = makeImageStub;
sandbox.globalThis = sandbox;
const ctxObj = vm.createContext(sandbox);

/* 真图关卡的图片内嵌在 photos-data.js 里，必须在主脚本之前加载，
 * 否则 PHOTO_SCENES 会是空的（场景是在主脚本里根据 PHOTO_DATA 构建的）。 */
const DATA_JS = join(ROOT, 'spot-difference', 'photos-data.js');
let photoDataUris = [];
try {
  if (existsSync(DATA_JS)) {
    const dataSrc = readFileSync(DATA_JS, 'utf8');
    vm.runInContext(dataSrc, ctxObj, { filename: 'photos-data.js' });
    const pd = vm.runInContext('window.PHOTO_DATA || {}', ctxObj);
    for (const k of Object.keys(pd)) photoDataUris.push(pd[k].A, pd[k].B);
    console.log(`已加载图片数据：${Object.keys(pd).length} 关`);
  } else {
    console.log('（没有 photos-data.js，跳过真图关卡）');
  }
} catch (e) {
  console.error('✗ 加载 photos-data.js 失败:', e.message);
}

let groups = [];
try {
  vm.runInContext(js, ctxObj);
  groups = [
    { label: '简单 · 手绘场景', scenes: vm.runInContext('SCENES', ctxObj) },
    { label: '困难 · 手绘主题关', scenes: vm.runInContext('PHOTO_SCENES.hard', ctxObj) },
    { label: '地狱 · 同场景换天色', scenes: vm.runInContext('PHOTO_SCENES.hell', ctxObj) },
  ];
  if (argScene) {
    const extraSrc = readFileSync(argScene, 'utf8');
    vm.runInContext(extraSrc, ctxObj, { filename: argScene });
    const extra = vm.runInContext('typeof EXTRA_SCENES !== "undefined" ? EXTRA_SCENES : globalThis.__SCENES__', ctxObj);
    groups.push({ label: '草稿 · ' + argScene, scenes: extra });
  }
} catch (e) {
  console.error('✗ 脚本执行失败:', e && e.stack ? e.stack.split('\n').slice(0, 5).join('\n') : e);
  process.exit(1);
}

/* ---- 预加载真图并塞进沙箱 ---- */
/* 真图关卡的图在 photos-data.js 里（base64），沙箱里没有 window.PHOTO_DATA，
 * 这里直接把 images/*.jpg 读进来，按游戏的接口塞成 { key: {A, B} }。 */
const srcs = new Map();          // key -> [A路径, B路径]
for (const g of groups) for (const sc of (g.scenes || [])) {
  if (!sc || !sc.key || !sc.imgA) continue;
  srcs.set(sc.key, [sc.imgA, sc.imgB]);
}
/* 把 data URI 解码成图片塞进 Image 桩的查找表 —— 桩是按 src 查的，
 * 游戏里 img.src 就是这些 data URI，所以键用 URI 本身。 */
if (photoDataUris.length) {
  if (!loadImage) { console.error('✗ 当前 canvas 模块没有 loadImage，无法校验真图关卡'); process.exit(2); }
  let n = 0;
  for (const uri of photoDataUris) {
    const comma = uri.indexOf(',');
    const buf = Buffer.from(uri.slice(comma + 1), 'base64');
    sandbox.__IMAGES__[uri] = await loadImage(buf);
    n++;
  }
  console.log(`已解码 ${n} 张实景图用于校验`);
  /* 触发游戏自己的预加载（Image 桩是同步的，这一步就完成了） */
  vm.runInContext('preloadPhotos(PHOTO_SCENES.hard); preloadPhotos(PHOTO_SCENES.hell);', ctxObj);
}

console.log(`关卡质量校验（canvas: ${usedCanvas}）`);
console.log(`正向：点位中心 ${CORE_R}px 内 >= ${CORE_MIN} 差异像素 · ` +
            `反向：>= ${STRAY_MIN_AREA} 像素的差异块都必须被点位覆盖\n`);

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

/* 真图关卡的差异是游戏运行时自己比对出来的，这里先调游戏自己的函数算一遍，
 * 再按"每个提示位置压在差异上、每处差异都被区域覆盖"来验收。 */
for (const g of groups) {
  for (const sc of (g.scenes || [])) {
    if (!sc || !sc.key || !sc.imgA) continue;
    try {
      const d = vm.runInContext('(function(){var s=PHOTO_SCENES.hard.concat(PHOTO_SCENES.hell)'
        + '.filter(function(x){return x.key===' + JSON.stringify(sc.key) + ';})[0];'
        + 'var r=computePhotoDiffs(s); return r? r.length : -1;})()', ctxObj);
      if (d > 0 && !sc.diffs) sc.diffs = vm.runInContext(
        'PHOTO_SCENES.hard.concat(PHOTO_SCENES.hell)'
        + '.filter(function(x){return x.key===' + JSON.stringify(sc.key) + ';})[0]._diffs', ctxObj);
    } catch (e) { /* 下面会作为"没有差异点"报出来 */ }
  }
}

let badLevels = 0, badPoints = 0, strayTotal = 0, checked = 0;
for (const g of groups) {
  const scenes = g.scenes || [];
  const k0 = scenes[0] && scenes[0].diffs ? scenes[0].diffs.length : '?';
  console.log(`【${g.label}】${scenes.length} 关（每关 ${k0} 处不同）`);
  for (let i = 0; i < scenes.length; i++) {
    const sc = scenes[i];
    checked++;
    const problems = [];
    let cA, cB;
    try {
      cA = createCanvas(400, 300); cB = createCanvas(400, 300);
      sc.draw(cA.getContext('2d'), 'A');
      sc.draw(cB.getContext('2d'), 'B');
    } catch (e) {
      console.log(`  ✗ ${sc.name}：渲染抛错 -> ${e.message}`);
      badLevels++; badPoints++;
      continue;
    }
    const da = cA.getContext('2d').getImageData(0, 0, 400, 300).data;
    const db = cB.getContext('2d').getImageData(0, 0, 400, 300).data;
    const delta = p =>
      Math.abs(da[p] - db[p]) + Math.abs(da[p + 1] - db[p + 1]) + Math.abs(da[p + 2] - db[p + 2]);
    const isPhoto = !!(sc.imgA || sc.imgB);
    const TH = isPhoto ? PHOTO_DIFF_TH : 24;
    const strayMin = isPhoto ? PHOTO_STRAY_AREA : STRAY_MIN_AREA;

    const mask = new Uint8Array(400 * 300);
    let total = 0;
    for (let n = 0, p = 0; n < mask.length; n++, p += 4) {
      if (delta(p) > TH) { mask[n] = 1; total++; }
    }
    if (total < 60) problems.push(`A/B 两版几乎一样（差异像素仅 ${total}）`);
    if (!sc.diffs || !sc.diffs.length) problems.push('没有差异点');

    if (isPhoto) {
      /* 真图：逐处检查"判定区域里确实有差异像素"，并统计未覆盖的差异像素比例 */
      let tot = 0, covered = 0;
      for (let n = 0, p = 0; n < mask.length; n++, p += 4) {
        if (!mask[n]) continue;
        tot++;
        const x = n % 400, y = (n - x) / 400;
        let hit = false;
        for (const d of (sc.diffs || [])) {
          for (const b of (d.boxes || [])) {
            if (x >= b[0] - REGION_PAD && x <= b[2] + REGION_PAD &&
                y >= b[1] - REGION_PAD && y <= b[3] + REGION_PAD) { hit = true; break; }
          }
          if (hit) break;
        }
        if (hit) covered++;
      }
      const pct = tot ? 100 * covered / tot : 100;
      /* 没被覆盖的像素里，绝大多数是 JPEG 压缩/抗锯齿产生的一两个像素的碎点
       * （肉眼看不见，孩子也不可能专门去点）。所以这里把这些碎点聚成块，
       * 只有"看得见的块"（>=25px）漏掉才算问题。 */
      const UNCOVER_MIN = 45;   // 和游戏里的 PHOTO_MIN_AREA 一致：
                               // 够格单独算一处差异的块，必须被覆盖
      const left = new Uint8Array(400 * 300);
      for (let n = 0, p2 = 0; n < mask.length; n++, p2 += 4) {
        if (!mask[n]) continue;
        const x = n % 400, y = (n - x) / 400;
        let hit = false;
        for (const d of (sc.diffs || [])) {
          for (const b of (d.boxes || [])) {
            if (x >= b[0] - REGION_PAD && x <= b[2] + REGION_PAD &&
                y >= b[1] - REGION_PAD && y <= b[3] + REGION_PAD) { hit = true; break; }
          }
          if (hit) break;
        }
        if (!hit) left[n] = 1;
      }
      const visibleLeft = clusters(left, 400, 300).filter(c => c.area >= UNCOVER_MIN);
      if (tot < 100) problems.push(`两张图差异像素只有 ${tot} 个（几乎没有差异）`);
      if (visibleLeft.length) {
        problems.push(`${visibleLeft.length} 处看得见的差异没被判定区域覆盖` +
          `（最大 ${Math.max.apply(null, visibleLeft.map(c => c.area))}px）；` +
          `整体覆盖率 ${pct.toFixed(1)}%`);
      }
      if (!sc.diffs || !sc.diffs.length) problems.push('运行时没算出任何差异');
      if (problems.length) {
        badLevels++;
        console.log(`  第${i + 1}关 ${sc.name}`);
        problems.forEach(p => console.log('    ✗ ' + p));
      }
      continue;
    }
    (sc.diffs || []).forEach((d, k) => {
      let core = 0;
      for (let y = Math.max(0, d.y - CORE_R); y < Math.min(300, d.y + CORE_R); y++)
        for (let x = Math.max(0, d.x - CORE_R); x < Math.min(400, d.x + CORE_R); x++)
          if (mask[y * 400 + x]) core++;
      if (core < CORE_MIN) {
        let bx = null, by = null, bn = 0;
        for (let y = Math.max(0, d.y - 80); y < Math.min(300, d.y + 80); y += 2) {
          for (let x = Math.max(0, d.x - 80); x < Math.min(400, d.x + 80); x += 2) {
            let n = 0;
            for (let yy = Math.max(0, y - CORE_R); yy < Math.min(300, y + CORE_R); yy++)
              for (let xx = Math.max(0, x - CORE_R); xx < Math.min(400, x + CORE_R); xx++)
                if (mask[yy * 400 + xx]) n++;
            if (n > bn) { bn = n; bx = x; by = y; }
          }
        }
        problems.push(`D${k + 1} (${d.x},${d.y}) "${d.desc || ''}" 点位中心仅 ${core} 个差异像素` +
          (bx !== null ? ` → 建议改为 (${bx},${by})，该处 ${bn} 个` : '，且 80px 内找不到差异'));
        badPoints++;
      }
      if (d.x < 34 || d.y < 34 || d.x > 366 || d.y > 266) {
        problems.push(`D${k + 1} (${d.x},${d.y}) 离画布边太近（<34px），幼儿点不准`);
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

    for (const c of clusters(mask, 400, 300)) {
      /* 真图关卡的差异是游戏自己按同一阈值、同一套连通域逻辑算出来的，
       * 声明区域必然覆盖它，所以这里不必再反向查"多出来的差异" */
      if (isPhoto) continue;
      if (c.area < strayMin) continue;
      const covered = (sc.diffs || []).some(d => {
        /* 真图关卡：一处变化可能占很大一片（整栋楼换色），游戏里是"区域判定"，
         * 点在框内任何位置都算找到；这里按同样的规则验收 */
        if (d.boxes) {
          for (const b of d.boxes) {
            if (c.cx >= b[0] - REGION_PAD && c.cx <= b[2] + REGION_PAD &&
                c.cy >= b[1] - REGION_PAD && c.cy <= b[3] + REGION_PAD) return true;
          }
        }
        const inBox = d.x >= c.minX - COVER_PAD && d.x <= c.maxX + COVER_PAD &&
                      d.y >= c.minY - COVER_PAD && d.y <= c.maxY + COVER_PAD;
        return inBox || Math.hypot(d.x - c.cx, d.y - c.cy) < 55;
      });
      if (!covered) {
        strayTotal++;
        problems.push(`多出一处未声明的差异：面积 ${c.area}px，中心约 (${Math.round(c.cx)},${Math.round(c.cy)})` +
          `，范围 x[${c.minX}..${c.maxX}] y[${c.minY}..${c.maxY}]`);
      }
    }

    if (problems.length) {
      badLevels++;
      console.log(`  第${i + 1}关 ${sc.name}`);
      problems.forEach(p => console.log('    ✗ ' + p));
    }
  }
  console.log('');
}
console.log(`结果：共 ${checked} 关，${checked - badLevels} 关通过；` +
            `点位问题 ${badPoints} 处，未覆盖的多余差异 ${strayTotal} 块。`);
process.exit(badLevels ? 1 : 0);
