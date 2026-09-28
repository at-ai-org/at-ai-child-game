#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
少儿 AI 游戏乐园 · Windows kiosk 图标生成器

本机没有 PIL / numpy，所以这里自己做了最小可用的光栅化（形状用有符号距离场 + 4x4 超采样），
再自己写 PNG 与 ICO 容器（≤64px 用 BMP 位图，128/256 用 PNG——Windows Vista 以后都认）。

设计：紫→蓝渐变圆角方块 + 白色游戏手柄（十字方向键 + 四色按钮）+ 两颗星光；
      16/24px 自动切换「简化放大版」（去掉星光、加大按钮），保证任务栏里也看得清。

用法：
    python3 windows-kiosk/make-icon.py            # 生成 app.ico + 预览图
    python3 windows-kiosk/make-icon.py --preview  # 只出预览图，不覆盖 app.ico
"""

import math
import os
import struct
import sys
import zlib

OUT_DIR = os.path.dirname(os.path.abspath(__file__))
ICO_PATH = os.path.join(OUT_DIR, "app.ico")
SIZES = [16, 24, 32, 48, 64, 128, 256]
SS = 4  # 超采样倍数：每像素 4x4 = 16 个采样点

# ---------- 配色 ----------
GRAD_A = (138, 92, 255)      # 左上：紫
GRAD_B = (31, 162, 255)      # 右下：蓝
PAD = (255, 255, 255)        # 手柄主体
DPAD = (44, 37, 96)          # 十字键（深靛蓝）
BTN = [(255, 92, 122), (255, 211, 77), (92, 227, 154), (79, 184, 255)]  # 上 右 下 左
SPARK = (255, 255, 255)
SHADOW = (28, 20, 78)


# ---------- 基础工具 ----------
def mix(c1, c2, a):
    return tuple(c1[i] + (c2[i] - c1[i]) * a for i in range(3))


def rounded_rect(x, y, x0, y0, x1, y1, r):
    """点是否在圆角矩形内"""
    if x < x0 or x > x1 or y < y0 or y > y1:
        return False
    cx = min(max(x, x0 + r), x1 - r)
    cy = min(max(y, y0 + r), y1 - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def circle(x, y, cx, cy, r):
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def star4(x, y, cx, cy, r):
    """四角星光：|dx|^p + |dy|^p <= 1（p<1 → 内凹尖角）"""
    p = 0.55
    dx, dy = abs(x - cx) / r, abs(y - cy) / r
    if dx > 1 or dy > 1:
        return False
    return dx ** p + dy ** p <= 1


def sdf_roundrect(x, y, x0, y0, x1, y1, r):
    """圆角矩形有符号距离（<0 在内部）"""
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    hw, hh = (x1 - x0) / 2.0, (y1 - y0) / 2.0
    qx = abs(x - cx) - (hw - r)
    qy = abs(y - cy) - (hh - r)
    return math.hypot(max(qx, 0.0), max(qy, 0.0)) + min(max(qx, qy), 0.0) - r


def sdf_roundrect_rot(x, y, cx, cy, w, h, r, ang):
    """旋转后的圆角矩形有符号距离"""
    ca, sa = math.cos(-ang), math.sin(-ang)
    dx, dy = x - cx, y - cy
    lx, ly = dx * ca - dy * sa, dx * sa + dy * ca
    return sdf_roundrect(lx, ly, -w / 2.0, -h / 2.0, w / 2.0, h / 2.0, r)


def pad_sdf(x, y, body, grips):
    """手柄轮廓的有符号距离（<0 在轮廓内）：中段 + 两个外八字握把"""
    d = sdf_roundrect(x, y, body[0], body[1], body[2], body[3], body[4])
    for gx, gy, gw, gh, gr, ang in grips:
        d = min(d, sdf_roundrect_rot(x, y, gx, gy, gw, gh, gr, ang))
    return d


# ---------- 图形定义（单位坐标 0~1） ----------
# 握把几何可调参数（用于对比不同造型）
GEO = dict(body_bottom=0.520, grip_w=0.150, grip_h=0.235, grip_r=0.058,
           grip_off=0.215, grip_cy=0.515, grip_ang=0.36)


def shapes(compact, G=None):
    """返回 (圆角半径, 手柄, 十字键, 按钮, 星光)"""
    G = G or GEO
    s = 1.16 if compact else 1.0            # 小尺寸整体放大，减少留白
    border = 0.20 if compact else 0.235

    def tx(v):
        return 0.5 + (v - 0.5) * s

    def tr(r):
        return r * s

    def box(x0, y0, x1, y1):
        return (tx(x0), tx(y0), tx(x1), tx(y1))

    body = box(0.245, 0.305, 0.755, G['body_bottom']) + (tr(0.085),)
    grips = []
    for sgn in (1, -1):                                  # 左右握把，外八字
        grips.append((tx(0.5 + sgn * G['grip_off']), tx(G['grip_cy']),
                      tr(G['grip_w']), tr(G['grip_h']), tr(G['grip_r']), sgn * G['grip_ang']))

    rects = [box(0.302, 0.398, 0.418, 0.427), box(0.345, 0.359, 0.375, 0.466)]

    btnr = tr(0.036 if compact else 0.030)
    dist = (0.070 if compact else 0.064) * s
    bx, by = tx(0.640), tx(0.415)
    buttons = [(bx + ox * dist, by + oy * dist, btnr, BTN[i])
               for i, (ox, oy) in enumerate(((0, -1), (1, 0), (0, 1), (-1, 0)))]

    sparks = [] if compact else [(0.795, 0.215, 0.078, 0.95), (0.684, 0.135, 0.044, 0.78)]
    return border, body, grips, rects, buttons, sparks


CACHE = {}


def color_at(x, y, compact):
    """返回该点颜色 (r, g, b, a)，自底向上逐层合成。"""
    border, body, grips, rects, buttons, sparks = CACHE[compact]

    if not rounded_rect(x, y, 0.0, 0.0, 1.0, 1.0, border):
        return (0.0, 0.0, 0.0, 0.0)

    t = max(0.0, min(1.0, x * 0.55 + y * 0.45))
    c = [GRAD_A[i] + (GRAD_B[i] - GRAD_A[i]) * t for i in range(3)]
    if y < 0.52:                                        # 顶部一层很淡的高光
        c = list(mix(c, (255, 255, 255), 0.13 * (1.0 - y / 0.52)))

    d = pad_sdf(x, y, body, grips)
    if d > 0.0:
        if d < 0.055:                                   # 手柄下方的柔和投影
            c = list(mix(c, SHADOW, 0.20 * (1.0 - d / 0.055) ** 1.5))
    else:
        c = list(PAD)
        for (x0, y0, x1, y1) in rects:
            if rounded_rect(x, y, x0, y0, x1, y1, 0.022):
                c = list(DPAD)
                break
        else:
            for bx, by, br, bc in buttons:
                if circle(x, y, bx, by, br):
                    c = list(bc)
                    break
    for sx, sy, sr, sa in sparks:                       # 星光压在最上层
        if star4(x, y, sx, sy, sr):
            c = list(mix(c, SPARK, sa))
    return (c[0], c[1], c[2], 255.0)


def render(size, compact, G=None):
    CACHE[compact] = shapes(compact, G)
    buf = bytearray(size * size * 4)
    n = SS * SS
    for py in range(size):
        for px in range(size):
            r = g = b = a = 0.0
            for sy in range(SS):
                for sx in range(SS):
                    x = (px + (sx + 0.5) / SS) / size
                    y = (py + (sy + 0.5) / SS) / size
                    cr, cg, cb, ca = color_at(x, y, compact)
                    al = ca / 255.0
                    r += cr * al                            # 预乘 alpha 再平均，避免边缘发黑
                    g += cg * al
                    b += cb * al
                    a += al
            if a > 0:
                o = (py * size + px) * 4
                buf[o] = int(round(r / a))
                buf[o + 1] = int(round(g / a))
                buf[o + 2] = int(round(b / a))
                buf[o + 3] = int(round(a / n * 255))
    return bytes(buf)


# ---------- PNG / ICO 编码 ----------
def png_encode(size, rgba):
    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    raw = b"".join(b"\x00" + rgba[y * size * 4:(y + 1) * size * 4] for y in range(size))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def bmp_encode(size, rgba):
    """ICO 里的 BMP 位图：40 字节头 + 自下而上的 BGRA + AND 掩码"""
    hdr = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, size * size * 4, 0, 0, 0, 0)
    rows = b""
    for y in range(size - 1, -1, -1):
        row = rgba[y * size * 4:(y + 1) * size * 4]
        rows += bytes(b for i in range(size)
                      for b in (row[i * 4 + 2], row[i * 4 + 1], row[i * 4], row[i * 4 + 3]))
    mask_row = ((size + 31) // 32) * 4
    return hdr + rows + b"\x00" * (mask_row * size)


def build_ico(entries):
    """entries: [(size, payload)]"""
    out = struct.pack("<HHH", 0, 1, len(entries))
    offset = 6 + 16 * len(entries)
    dirs = blobs = b""
    for size, payload in entries:
        dirs += struct.pack("<BBBBHHII", size if size < 256 else 0, size if size < 256 else 0,
                            0, 0, 1, 32, len(payload), offset)
        blobs += payload
        offset += len(payload)
    return out + dirs + blobs


VARIANTS = {
    "A 现状": GEO,
    "B 扁握把": dict(GEO, grip_w=0.150, grip_h=0.205, grip_r=0.050, grip_off=0.218,
                  grip_cy=0.512, grip_ang=0.30, body_bottom=0.512),
    "C 深凹口": dict(GEO, grip_h=0.245, grip_r=0.062, grip_off=0.205, grip_cy=0.520,
                  grip_ang=0.454, body_bottom=0.502),
}


def montage():
    """把几个握把变体并排渲染成一张对比图"""
    size = 200
    names = list(VARIANTS)
    out = bytearray(size * len(names) * size * 4)
    W = size * len(names)
    for k, name in enumerate(names):
        rgba = render(size, False, VARIANTS[name])
        for y in range(size):
            for x in range(size):
                src = (y * size + x) * 4
                dst = (y * W + k * size + x) * 4
                out[dst:dst + 4] = rgba[src:src + 4]
        print(f"  变体 {name}")
    open(os.path.join(OUT_DIR, "variants.png"), "wb").write(png_encode_raw(W, size, bytes(out)))


def png_encode_raw(w, h, rgba):
    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    raw = b"".join(b"\x00" + rgba[y * w * 4:(y + 1) * w * 4] for y in range(h))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def main():
    if "--variants" in sys.argv:
        montage()
        print("\n已写入 variants.png")
        return
    preview_only = "--preview" in sys.argv
    entries = []
    for size in SIZES:
        compact = size < 28
        rgba = render(size, compact)
        entries.append((size, bmp_encode(size, rgba) if size <= 64 else png_encode(size, rgba)))
        print(f"  {size:3d}x{size:<3d} {'简化版' if compact else '标准版'}  {len(entries[-1][1]):>7d} 字节")
        if size == 256:                                     # 仓库里留一张参考图
            open(os.path.join(OUT_DIR, "icon-preview.png"), "wb").write(png_encode(size, rgba))
        if size in (32, 16):                                # 小尺寸放大图，便于肉眼检查
            zoom = 256 // size
            big = bytearray(256 * 256 * 4)
            for y in range(256):
                for x in range(256):
                    o = ((y // zoom) * size + (x // zoom)) * 4
                    d = (y * 256 + x) * 4
                    big[d:d + 4] = rgba[o:o + 4]
            open(os.path.join(OUT_DIR, f"preview-{size}-zoom.png"), "wb").write(png_encode(256, bytes(big)))

    if not preview_only:
        open(ICO_PATH, "wb").write(build_ico(entries))
        print(f"\n已写入 {ICO_PATH}（{os.path.getsize(ICO_PATH)} 字节，{len(entries)} 个尺寸）")


if __name__ == "__main__":
    main()
