"""七巧板 9 关布局设计与验证工具。
   锚点 = 块原点(0,0) 在 U 坐标中的位置（U = 游戏 unit*2）。
   槽位换算：sl.x = ax/2, sl.y = ay/2。
"""
import math, sys, json

# 每种块的顶点（未缩放），与游戏 PIECE_DEFS 一致
PTS = [
    [(-1, -1), (1, -1), (-1, 1)],                     # 0 大三角A
    [(-1, -1), (1, -1), (-1, 1)],                     # 1 大三角B
    [(-1, -1), (1, -1), (-1, 1)],                     # 2 中三角
    [(-1, -1), (1, -1), (-1, 1)],                     # 3 小三角A
    [(-1, -1), (1, -1), (-1, 1)],                     # 4 小三角B
    [(-1, -1), (1, -1), (1, 1), (-1, 1)],             # 5 正方形
    [(-2, -2), (0, -2), (2, 0), (0, 0)],              # 6 平四边
]
# 半跨度 K：顶点坐标 * K 后即 U 单位下的实际大小
K = [1, 1, 0.70710678, 0.5, 0.5, 0.5, 0.5]

def poly(idx, ax, ay, rot):
    """rot 顺时针 90°*rot；屏幕 y 向下"""
    out = []
    a = -rot * math.pi / 2
    ca, sa = math.cos(a), math.sin(a)
    for (x, y) in PTS[idx]:
        X, Y = x * K[idx], y * K[idx]
        out.append((ax + X * ca - Y * sa, ay + X * sa + Y * ca))
    return out

def build(items):
    return {chr(65 + i): poly(*it) for i, it in enumerate(items)}

def inpoly(p, px, py):
    n = len(p); ins = False
    for i in range(n):
        x1, y1 = p[i]; x2, y2 = p[(i + 1) % n]
        if (y1 > py) != (y2 > py):
            if px < (x2 - x1) * (py - y1) / (y2 - y1) + x1: ins = not ins
    return ins

def area(p):
    a = 0
    for i in range(len(p)):
        x1, y1 = p[i]; x2, y2 = p[(i + 1) % len(p)]
        a += x1 * y2 - x2 * y1
    return abs(a) / 2

def check_overlap(polys, tol=0.06):
    ks = list(polys); bad = []
    for i in range(len(ks)):
        for j in range(i + 1, len(ks)):
            a, b = polys[ks[i]], polys[ks[j]]
            xs = [q[0] for q in a] + [q[0] for q in b]
            ys = [q[1] for q in a] + [q[1] for q in b]
            step = 0.04; ov = 0
            y = min(ys)
            while y < max(ys):
                x = min(xs)
                while x < max(xs):
                    if inpoly(a, x, y) and inpoly(b, x, y): ov += 1
                    x += step
                y += step
            if ov * step * step > tol: bad.append((ks[i], ks[j], round(ov * step * step, 3)))
    return bad

def preview(items, title='', W=52, H=26, polys=None):
    polys = polys or build(items)
    if title: print(f'=== {title} ===')
    xs = [q[0] for v in polys.values() for q in v]
    ys = [q[1] for v in polys.values() for q in v]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    print(f'    bbox x:[{x0:.2f},{x1:.2f}] y:[{y0:.2f},{y1:.2f}]  size {x1-x0:.2f}x{y1-y0:.2f}')
    for r in range(H):
        line = ''
        for c in range(W):
            px = x0 + (c + .5) * (x1 - x0) / W
            py = y0 + (r + .5) * (y1 - y0) / H
            ch = '.'
            for k, v in polys.items():
                if inpoly(v, px, py): ch = k; break
            line += ch
        print(line)
    print()
    return polys

def report(name, items, W=52, H=26):
    polys = build(items)
    p = preview(items, name, W, H, polys)
    bad = check_overlap(polys)
    tot = sum(area(v) for v in polys.values())
    print(f'[{name}] overlap = {bad if bad else "NONE ✓"}   pieces={[it[0] for it in items]}  area={tot:.2f}')
    slots = [{"p": it[0], "x": round(it[1] / 2, 2), "y": round(it[2] / 2, 2), "r": it[3]} for it in items]
    print(f'[{name}] pieces: {[s["p"] for s in slots]}')
    print(f'[{name}] slots:  {json.dumps(slots, ensure_ascii=False)}')
    print()
    return bad

if __name__ == '__main__':
    pass

def edges(idx, rot, ax=0.0, ay=0.0):
    p = poly(idx, ax, ay, rot)
    return [(p[i], p[(i + 1) % len(p)]) for i in range(len(p))]

def snap_edge(base_items, base_edge_i, new_idx, new_rot, new_edge_i, flip=True):
    """把 new_idx 的第 new_edge_i 条边贴到 base_items[0] 的第 base_edge_i 条边上。
       flip=True 表示新块顶点顺序与底边相反（两块在同侧相反方向）。"""
    poly0 = poly(*base_items[0])
    be = [(poly0[i], poly0[(i + 1) % len(poly0)]) for i in range(len(poly0))][base_edge_i]
    np_ = poly(new_idx, 0, 0, new_rot)
    ne = [(np_[i], np_[(i + 1) % len(np_)]) for i in range(len(np_))][new_edge_i]
    if flip:
        # new edge start -> base edge end
        src, dst = ne[0], be[1]
    else:
        src, dst = ne[0], be[0]
    ax = dst[0] - src[0]; ay = dst[1] - src[1]
    return (new_idx, round(ax, 6), round(ay, 6), new_rot)
