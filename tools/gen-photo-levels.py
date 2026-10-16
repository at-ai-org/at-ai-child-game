#!/usr/bin/env python3
"""找不同·关卡底图生成（官方接口一次生成两张一致性图）

用的是千问平台的「图像组生成」能力（文档见
https://platform.qianwenai.com/docs/api-reference/image-generation/wan27-image-gen-edit/synchronous）：

  POST {base}/chat/completions
  {
    "model": "wan2.7-image-pro",
    "parameters": {
        "enable_sequential": true,   # 关键：生成"同一场景的连续图组"，特征保持一致
        "n": 2,                      # 一次出两张
        "size": "2K",
        "watermark": false           # 不要水印
    },
    "messages": [{"role":"user","content":[{"type":"text","text":"..."}]}]
  }

提示词要求"两张几乎一样、第二张有 N 处小差异、其余必须完全相同"，
于是直接拿到 A/B 两版，差异是模型自然画的（物体消失 / 变色 / 变了大小），
边缘光影都连贯。

踩过的坑：
  · n 必须写在 parameters 里；写在顶层会被忽略，只返回一张
  · 只写 n 不写 enable_sequential 也只会返回一张
  · 曾经让模型"在一张图里画左右两幅找不同"，结果两幅差 43% 像素
    （窗户、脚手架、树全被重画过），根本没法用；连续图组只差 4%，才是对的

用法:
  python3 tools/gen-photo-levels.py --list
  python3 tools/gen-photo-levels.py --only hard-1 --force
  python3 tools/gen-photo-levels.py
"""
import argparse, json, os, subprocess, sys, time
import numpy as np
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, 'spot-difference', 'images')
CFG_PATH = os.path.join(ROOT, 'image-config.json')
TMP = '/tmp/spotgen'
TARGET = (400, 300)
MODEL = 'wan2.7-image-pro'
SIZE = '2K'

MARGIN = 40        # 点位离画布边至少这么远
MIN_GAP = 34       # 点位两两至少这么远（判定是区域判定，点位只用于提示圈，
                   # 不必像老的圆圈判定那样卡 56px；太大反而会让某些变化没点位）
DIFF_TH = 120      # 单像素色差阈值（RGB 之和）。两图有轻微色调漂移，
                   # 阈值太低会把漂移也当差异（实测 34 时 40% 像素都超阈值）
MIN_AREA = 48      # 一处变化至少覆盖这么多像素才算"看得见"
REGION_PAD = 10    # 判定区域外扩（孩子点起来更好中）


# 要改哪些地方，逐条列出来。
# 实测：只说"改 6 处"模型经常只给 1~3 处；逐条列清楚就能稳定给到 4~6 处。
CHANGE_IDEAS = [
    'remove one small object completely',
    'remove another small object completely',
    'change the color of one object to a completely different color',
    'change the color of a second object to a completely different color',
    'change the color of a third object to a completely different color',
    'make one object clearly bigger or smaller',
    'add one small new object that belongs in this scene',
    'move one small object to a different place',
    'make one object clearly brighter or darker',
]


def make_prompt(scene, k):
    """提示词：要两张几乎一样的图，第二张逐条做 k 处改动。

    反复强调"其余必须完全相同"，否则模型会把窗户、脚手架都重画一遍。"""
    items = CHANGE_IDEAS[:max(3, k)]
    listed = ' '.join('%d) %s.' % (i + 1, t) for i, t in enumerate(items))
    return (
        "Generate two nearly identical pictures of " + scene + " "
        "Both pictures MUST show exactly the same scene, the same composition, the same "
        "art style, the same colors and the same objects in the same positions. "
        "The SECOND picture must be identical to the first EXCEPT for these %d changes: %s "
        "Every change must be small but clearly visible to a child. Everything else must be "
        "EXACTLY the same - do not redraw the background, do not move anything else, do not "
        "change the layout or the lighting. "
        "Flat vector children picture book illustration, bright cheerful colors, "
        "thick black outlines, rich detail filling the whole frame, "
        "no text, no letters, no numbers, no signs, no watermark."
    ) % (len(items), listed)


LEVELS = [
    # 困难/地狱 12 关已经改成 SVG 手绘公园场景（tools/gen-svg-park-levels.py 出图），
    # 这里留空是为了不让本脚本把那些图重新覆盖成 AI 实景。
    # 以后想再加"实景照片"关卡，把 dict(key=..., theme=..., title=..., k=..., scene=...) 填回来即可。
]


# ------------------------------------------------------------------ 接口
def load_cfg():
    with open(CFG_PATH, encoding='utf-8') as f:
        return json.load(f)


def gen_pair(cfg, prompt, retries=3):
    """一次调用拿到两张一致性图，返回 [urlA, urlB]"""
    payload = {
        "model": MODEL,
        "parameters": {"enable_sequential": True, "n": 2, "size": SIZE, "watermark": False},
        "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}]}],
    }
    os.makedirs(TMP, exist_ok=True)
    pfile = os.path.join(TMP, 'payload.json')
    with open(pfile, 'w') as f:
        json.dump(payload, f)
    last = ''
    for attempt in range(1, retries + 1):
        r = subprocess.run(
            ['curl', '-sS', '--max-time', '300', '-X', 'POST',
             '-H', 'Authorization: Bearer ' + cfg['key'],
             '-H', 'Content-Type: application/json',
             '-d', '@' + pfile, cfg['url'].rstrip('/') + '/chat/completions'],
            capture_output=True)
        out = r.stdout.decode('utf-8', 'ignore')
        try:
            d = json.loads(out)
            cont = d['output']['choices'][0]['message']['content']
            urls = [c['image'] for c in cont if c.get('type') == 'image']
            if len(urls) >= 2:
                return urls[0], urls[1]
            last = '只返回 %d 张图' % len(urls)
        except Exception:
            last = out[:200]
        if attempt < retries:
            time.sleep(5)
    raise RuntimeError('出图失败: ' + last)


def fetch(url, path):
    subprocess.run(['curl', '-sSL', '--max-time', '180', '-o', path, url], check=False)
    if not os.path.exists(path) or os.path.getsize(path) < 2000:
        raise RuntimeError('下载失败')
    return Image.open(path).convert('RGB')


def to_game(im, path):
    """居中裁成 4:3 再缩到 400x300 JPEG（质量 82，每张约 40KB，避免文件过大）"""
    w, h = im.size
    tw, th = w, int(round(w * 3 / 4))
    if th > h:
        th, tw = h, int(round(h * 4 / 3))
    l, t = (w - tw) // 2, (h - th) // 2
    im.crop((l, t, l + tw, t + th)).resize(TARGET, Image.LANCZOS).save(
        path, 'JPEG', quality=82, optimize=True, progressive=True)
    return os.path.getsize(path)


# ------------------------------------------------------------------ 差异检测
def find_clusters(a, b):
    """返回 (差异块列表, 标签图)。标签图用于精确定位每处变化的像素。"""
    x = np.asarray(a).astype(np.int16)
    y = np.asarray(b).astype(np.int16)
    dsum = np.abs(x - y).sum(2).astype(np.float32)
    mask = dsum > DIFF_TH
    H, W = mask.shape
    seen = np.zeros_like(mask, bool)
    label = np.zeros((H, W), np.int32)
    out = []
    for sy in range(H):
        for sx in range(W):
            if not mask[sy, sx] or seen[sy, sx]:
                continue
            cid = len(out) + 1
            st = [(sy, sx)]; seen[sy, sx] = True; label[sy, sx] = cid
            area = 0; minx, miny, maxx, maxy, sxx, syy = W, H, -1, -1, 0, 0
            while st:
                cy, cx = st.pop(); area += 1; sxx += cx; syy += cy
                minx = min(minx, cx); maxx = max(maxx, cx)
                miny = min(miny, cy); maxy = max(maxy, cy)
                for dy in (-1, 0, 1):
                    for dx in (-1, 0, 1):
                        ny, nx = cy + dy, cx + dx
                        if 0 <= ny < H and 0 <= nx < W and mask[ny, nx] and not seen[ny, nx]:
                            seen[ny, nx] = True; label[ny, nx] = cid; st.append((ny, nx))
            sub = dsum[miny:maxy + 1, minx:maxx + 1]
            subm = mask[miny:maxy + 1, minx:maxx + 1]
            out.append(dict(area=area, bbox=(minx, miny, maxx, maxy), cid=cid,
                            cx=sxx / area, cy=syy / area,
                            strength=float(sub[subm].mean()) if subm.any() else 0.0))
    return sorted(out, key=lambda c: -c['area']), label


MAX_GW, MAX_GH = 220, 180      # 一组变化的最大包围盒（防止连锁合并吞掉整幅图）


def group_changes(clusters, merge_dist=30):
    """把挨得近的碎块合并成"一处变化"，并按 面积x强度 打分。

    同一处变化常被拆成好几块（描边、阴影分开），不合并就会算成好几处。
    但两组中心离得比判定间距还近时也必须合并 —— 否则第二组没点位，
    那块差异就没人声明，孩子点它会判错。"""
    groups = []
    for c in clusters:
        if c['area'] < MIN_AREA:
            continue
        x0, y0, x1, y1 = c['bbox']
        placed = False
        for g in groups:
            gx0, gy0, gx1, gy1 = g['bbox']
            gcx, gcy = (gx0 + gx1) / 2, (gy0 + gy1) / 2
            nx0, ny0 = min(gx0, x0), min(gy0, y0)
            nx1, ny1 = max(gx1, x1), max(gy1, y1)
            near = (x0 <= gx1 + merge_dist and x1 >= gx0 - merge_dist and
                    y0 <= gy1 + merge_dist and y1 >= gy0 - merge_dist)
            too_close = np.hypot(c['cx'] - gcx, c['cy'] - gcy) < MIN_GAP
            if (near or too_close) and nx1 - nx0 <= MAX_GW and ny1 - ny0 <= MAX_GH:
                g['members'].append(c)
                g['bbox'] = [nx0, ny0, nx1, ny1]
                g['area'] += c['area']
                placed = True
                break
        if not placed:
            groups.append(dict(members=[c], bbox=[x0, y0, x1, y1], area=c['area']))
    for _ in range(4):
        merged = []
        for g in groups:
            hit = None
            gx0, gy0, gx1, gy1 = g['bbox']
            gcx, gcy = (gx0 + gx1) / 2, (gy0 + gy1) / 2
            for m in merged:
                mx0, my0, mx1, my1 = m['bbox']
                mcx, mcy = (mx0 + mx1) / 2, (my0 + my1) / 2
                nx0, ny0 = min(mx0, gx0), min(my0, gy0)
                nx1, ny1 = max(mx1, gx1), max(my1, gy1)
                near = (gx0 <= mx1 + merge_dist and gx1 >= mx0 - merge_dist and
                        gy0 <= my1 + merge_dist and gy1 >= my0 - merge_dist)
                too_close = np.hypot(gcx - mcx, gcy - mcy) < MIN_GAP
                if (near or too_close) and nx1 - nx0 <= MAX_GW and ny1 - ny0 <= MAX_GH:
                    hit = m
                    break
            if hit:
                hx0, hy0, hx1, hy1 = hit['bbox']
                hit['members'] += g['members']
                hit['bbox'] = [min(hx0, gx0), min(hy0, gy0), max(hx1, gx1), max(hy1, gy1)]
                hit['area'] += g['area']
            else:
                merged.append(g)
        groups = merged
    for g in groups:
        tw = sum(c['area'] for c in g['members']) or 1
        g['strength'] = sum(c['strength'] * c['area'] for c in g['members']) / tw
        g['score'] = g['area'] * (g['strength'] / 100.0)
    groups.sort(key=lambda g: -g['score'])
    return groups


def guess_kind(g):
    x0, y0, x1, y1 = g['bbox']
    w, h = x1 - x0 + 1, y1 - y0 + 1
    fill = g['area'] / max(1, w * h)
    if g['strength'] > 150:
        return '颜色不一样'
    if fill > 0.55 and g['area'] > 900:
        return '有东西不见了'
    if fill < 0.30:
        return '多了或少了个小东西'
    return '形状不一样'


def points_for(groups, mask):
    """每处变化安排一个提示位置（放大镜圈画在这里）。

    不用质心 —— 质心可能落在变化边缘，圈看着不在差异上。
    这里在质心附近搜一圈，取"以该点为圆心 12px 内差异像素最多"的位置。"""
    H, W = mask.shape

    def density(cx, cy, r=12):
        y0, y1 = max(0, cy - r), min(H, cy + r)
        x0, x1 = max(0, cx - r), min(W, cx + r)
        return int(mask[y0:y1, x0:x1].sum())

    def best_near(cx, cy):
        cx = min(max(int(cx), MARGIN), W - MARGIN)
        cy = min(max(int(cy), MARGIN), H - MARGIN)
        best, bv = (cx, cy), density(cx, cy)
        for dy in range(-18, 19, 3):
            for dx in range(-18, 19, 3):
                nx, ny = cx + dx, cy + dy
                if not (MARGIN <= nx <= W - MARGIN and MARGIN <= ny <= H - MARGIN):
                    continue
                v = density(nx, ny)
                if v > bv:
                    best, bv = (nx, ny), v
        return best, bv

    pts = []
    for g in groups:
        members = sorted(g['members'], key=lambda c: -(c['area'] * c['strength']))
        spot, score = None, -1
        for c in members:
            q, v = best_near(c['cx'], c['cy'])
            if v > score:
                spot, score = q, v
        if spot is None or score <= 0:
            continue
        px, py = spot
        if any(np.hypot(px - p['x'], py - p['y']) < MIN_GAP for p in pts):
            # 太近也不能整组丢掉 —— 丢掉就没有声明区域，那块差异孩子点它会判错。
            # 挪到该组另一个成员上试试；都不行就还是用它（宁可靠近也别漏）
            moved = False
            for c in members:
                qx, qy = int(round(c['cx'])), int(round(c['cy']))
                if all(np.hypot(qx - p['x'], qy - p['y']) >= MIN_GAP for p in pts):
                    px, py, moved = qx, qy, True
                    break
            if not moved:
                px, py = min(max(int(g['bbox'][0]), MARGIN), 400 - MARGIN), \
                         min(max(int(g['bbox'][1]), MARGIN), 300 - MARGIN)
        boxes = [[max(0, int(c['bbox'][0]) - REGION_PAD), max(0, int(c['bbox'][1]) - REGION_PAD),
                  min(399, int(c['bbox'][2]) + REGION_PAD), min(299, int(c['bbox'][3]) + REGION_PAD)]
                 for c in sorted(g['members'], key=lambda c: -c['area'])]
        pts.append(dict(x=px, y=py, area=g['area'], strength=round(g['strength'], 1),
                        bbox=[int(v) for v in g['bbox']], boxes=boxes, core=score,
                        desc=guess_kind(g)))
    return pts


def verify(a, b, pts, pad=6):
    """复核：每个提示位置都压在真差异上，且每一处变化都落在某个声明区域里。"""
    cl, _ = find_clusters(a, b)
    x = np.asarray(a).astype(np.int16); y = np.asarray(b).astype(np.int16)
    mask = (np.abs(x - y).sum(2) > DIFF_TH)
    problems = []
    for i, p in enumerate(pts, 1):
        cx, cy = p['x'], p['y']
        core = int(mask[max(0, cy - 12):cy + 12, max(0, cx - 12):cx + 12].sum())
        if core < 25:
            problems.append('D%d(%d,%d) 中心只有 %d 个差异像素' % (i, cx, cy, core))
    for i in range(len(pts)):
        for j in range(i):
            gap = float(np.hypot(pts[i]['x'] - pts[j]['x'], pts[i]['y'] - pts[j]['y']))
            if gap < 24:
                problems.append('D%d 与 D%d 只隔 %.0fpx' % (i + 1, j + 1, gap))
    for c in cl:
        if c['area'] < MIN_AREA:
            continue
        ok = False
        for p in pts:
            for bx0, by0, bx1, by1 in p['boxes']:
                if bx0 - pad <= c['cx'] <= bx1 + pad and by0 - pad <= c['cy'] <= by1 + pad:
                    ok = True
                    break
            if ok:
                break
        if not ok:
            problems.append('差异块(中心 %d,%d 面积 %d) 不在任何声明区域内'
                            % (round(c['cx']), round(c['cy']), c['area']))
    return problems


# ------------------------------------------------------------------ 单关
def build_one(cfg, lv, force=False):
    key, k = lv['key'], lv['k']
    dstA = os.path.join(OUT, key + '-A.jpg')
    dstB = os.path.join(OUT, key + '-B.jpg')
    meta = os.path.join(OUT, key + '.json')
    if os.path.exists(dstA) and os.path.exists(dstB) and os.path.exists(meta) and not force:
        print('  %-8s 已存在，跳过' % key)
        return True

    os.makedirs(TMP, exist_ok=True)
    os.makedirs(OUT, exist_ok=True)
    prompt = make_prompt(lv['scene'], k)
    for attempt in range(1, 6):
        try:
            urlA, urlB = gen_pair(cfg, prompt)
        except Exception as e:
            print('        第 %d 轮出图失败：%s' % (attempt, e))
            continue
        imA = fetch(urlA, os.path.join(TMP, '%s_A%d.png' % (key, attempt)))
        imB = fetch(urlB, os.path.join(TMP, '%s_B%d.png' % (key, attempt)))
        to_game(imA, dstA)
        to_game(imB, dstB)
        a = Image.open(dstA).convert('RGB')
        b = Image.open(dstB).convert('RGB')

        clusters, label = find_clusters(a, b)
        big = clusters[0]['area'] if clusters else 0
        if big > 400 * 300 * 0.10:
            print('        第 %d 轮两张图差别太大（最大差异块 %d px），重来…' % (attempt, big))
            continue
        groups = group_changes(clusters)
        # 模型给几处不稳定：3 处以上就收（游戏按实际数量显示，
        # 用户也反馈过"差异太多"，所以宁少勿多）；只有 1~2 处才重来
        if len(groups) < 3:
            print('        第 %d 轮只改了 %d 处（太少），重来…' % (attempt, len(groups)))
            continue
        if len(groups) > 9:
            print('        第 %d 轮改了 %d 处（太多），重来…' % (attempt, len(groups)))
            continue
        mask = (np.abs(np.asarray(a).astype(np.int16) -
                       np.asarray(b).astype(np.int16)).sum(2) > DIFF_TH)
        pts = points_for(groups, mask)
        if len(pts) < 3:
            print('        第 %d 轮只有 %d 处能放点位，重来…' % (attempt, len(pts)))
            continue
        problems = verify(a, b, pts)
        if problems:
            print('        第 %d 轮复核不过：%s' % (attempt, '; '.join(problems[:2])))
            continue
        json.dump(dict(key=key, name=lv['title'], theme=lv['theme'], k=len(pts),
                       sizeA=os.path.getsize(dstA), sizeB=os.path.getsize(dstB),
                       diffs=pts), open(meta, 'w'), ensure_ascii=False, indent=1)
        print('  %-8s ✓ 第%d轮 %d 处不同  A=%dKB B=%dKB  %s'
              % (key, attempt, len(pts), os.path.getsize(dstA) // 1024,
                 os.path.getsize(dstB) // 1024, '/'.join(p['desc'] for p in pts)))
        return True
    print('  %-8s ✗ 试了 5 轮都不合适' % key)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--only')
    ap.add_argument('--force', action='store_true')
    ap.add_argument('--list', action='store_true')
    args = ap.parse_args()
    if args.list:
        for lv in LEVELS:
            print('%-8s [%s] %-8s 目标 %d 处' % (lv['key'], lv['theme'], lv['title'], lv['k']))
        return
    cfg = load_cfg()
    todo = [lv for lv in LEVELS if not args.only or lv['key'] == args.only]
    ok = 0
    for lv in todo:
        try:
            if build_one(cfg, lv, args.force):
                ok += 1
        except Exception as e:
            print('  %-8s ✗ %s' % (lv['key'], e))
        time.sleep(1)
    print('\n完成 %d/%d' % (ok, len(todo)))


if __name__ == '__main__':
    main()
