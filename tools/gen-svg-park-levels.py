#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""找不同·困难模式 1-6 关：SVG 手绘公园场景生成器

为什么用 SVG 手画而不是 AI 出图（对比 tools/gen-photo-levels.py）：
  · A/B 两版由**同一份代码**派生，只有差异处走 variant 分支，其余路径逐字节一致，
    绝不会出现"模型顺手把窗户重画一遍"这种整图漂移，差异全部可控；
  · 差异的形状/颜色/位置都是自己摆的，能精确满足游戏检测算法的门槛
    （最小面积、合并距离、点位间距），不会出现"看得见却点不亮"的陷阱；
  · 矢量图形，放大不糊，将来改一处颜色不用重出整张图。

产物（每关 A/B 两张）：
  spot-difference/images/<key>-A.jpg / -B.jpg        400×300 JPEG（游戏用，同 AI 关卡规格）
  spot-difference/images/<key>.json                  该关检测结果（点位/判定框，供排查用）
  （SVG 中间产物写到临时目录，见下方说明）
之后照旧跑 python3 tools/embed-photo-levels.py 内嵌成 photos-data.js。

用法:
  python3 tools/gen-svg-park-levels.py            # 生成全部 6 关并自检
  python3 tools/gen-svg-park-levels.py --only hard-3
  python3 tools/gen-svg-park-levels.py --svg-only # 只写 SVG，不渲染 JPG

SVG 只是渲染用的中间产物，默认写到系统临时目录（$SPOT_SVG_DIR 可改），仓库里不留。
想留一份可编辑的 SVG 出来，就设 SPOT_SVG_DIR 指到你想放的地方。

自检内容（复刻 index.html 里 computePhotoDiffs() 的阈值与合并规则）：
  · 每关必须正好 6 处差异（困难模式 spots = 6）
  · 每处差异 >= MIN_DIFF_PX 个高对比像素（太小的差异孩子看不见、也过不了检测）
  · 点位两两 >= 34px（游戏的 PHOTO_MIN_GAP，太近会被丢弃一整处）；本次按 56px 富余量设计
  · 点位离画布边 >= 34px（否则提示圈被裁到画面外）
"""
import argparse
import json
import math
import os
import subprocess
import sys
import tempfile
import time

import numpy as np
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# SVG 是可再生产物（每次跑本脚本都会重新生成），放到临时构建目录，不占仓库
SVG_DIR = os.environ.get('SPOT_SVG_DIR') or os.path.join(tempfile.gettempdir(), 'spot-diff-svg')
IMG_DIR = os.path.join(ROOT, 'spot-difference', 'images')
CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'

# 设计基准：SVG 画在 800×600，游戏里缩到 400×300（和现有真图关卡同规格）
W, H = 800, 600
GAME = (400, 300)

# ---- 游戏检测参数（与 index.html 一致） ----
PHOTO_DIFF_TH = 110
PHOTO_MIN_AREA = 45
PHOTO_MERGE = 30
PHOTO_MAX_W = 220
PHOTO_MAX_H = 180
PHOTO_MIN_GAP = 34
PHOTO_PAD = 18

# ---- 本次的自定门槛（比游戏更严，留富余量） ----
MIN_DIFF_PX = 300      # 每处差异至少这么多像素（400×300 系）
WANT_GAP = 56          # 点位两两至少这么远
WANT_MARGIN = 34       # 点位离四边至少这么远


# ==========================================================================
# 一、SVG 小工具（全部按 800×600 摆位）
# ==========================================================================
def _blend(c1, c2, t):
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return '#%02x%02x%02x' % tuple(round(x + t * (y - x)) for x, y in zip(a, b))


def defs(pal, extra=''):
    """背景渐变由调色板决定：地狱模式只换天色，物体颜色一律不动 ——
    消失/大小/转向类差异靠的是"物体 vs 背景"的色差，整体压暗会把它们压到检测阈值以下。

    天空特意加了一个 55% 的中段停靠点：上半部（云、太阳、气球都在这儿）保持深色，
    只有靠地平线那一小条才转亮。否则"浅色天空 + 黄色气球"这种组合色差只有 ~100，
    低于检测阈值 110，那处差异孩子看得见、程序却判不出来。"""
    mid = _blend(pal['sky_top'], pal['sky_bottom'], 0.30)
    grad = ('<defs>'
            '<linearGradient id="sky" x1="0" y1="0" x2="0" y2="1">'
            '<stop offset="0" stop-color="%s"/><stop offset="0.55" stop-color="%s"/>'
            '<stop offset="1" stop-color="%s"/></linearGradient>'
            '<linearGradient id="grass" x1="0" y1="0" x2="0" y2="1">'
            '<stop offset="0" stop-color="%s"/><stop offset="1" stop-color="%s"/></linearGradient>'
            '<radialGradient id="water" cx="0.5" cy="0.4" r="0.75">'
            '<stop offset="0" stop-color="%s"/><stop offset="1" stop-color="%s"/></radialGradient>'
            '</defs>')
    return grad % (pal['sky_top'], mid, pal['sky_bottom'],
                   pal['grass_top'], pal['grass_bottom'], pal['water1'], pal['water2'])


def sky(horizon=400):
    return '<rect x="0" y="0" width="800" height="%d" fill="url(#sky)"/>' % horizon


def grass(horizon=400):
    """草地在山丘之后画：山体只露出地平线以上的那一截"""
    return '<rect x="0" y="%d" width="800" height="%d" fill="url(#grass)"/>' % (horizon, H - horizon)


def hills(*items):
    """items: (cx, cy, rx, ry, color)"""
    return ''.join('<ellipse cx="%s" cy="%s" rx="%s" ry="%s" fill="%s"/>' % it for it in items)


def cloud(x, y, s=1.0):
    return ('<g transform="translate(%s,%s) scale(%s)">'
            '<ellipse cx="0" cy="0" rx="42" ry="26" fill="#fff"/>'
            '<ellipse cx="-34" cy="8" rx="28" ry="18" fill="#fff"/>'
            '<ellipse cx="34" cy="9" rx="30" ry="19" fill="#fff"/></g>' % (x, y, s))


def sun(x, y, r=34, rays=None, color='#ffd93b', edge='#f5b721'):
    """rays=(内半径, 外半径, 线宽) 有光芒；None 就是光秃秃的太阳"""
    out = ''
    if rays:
        i, o, w = rays
        for k in range(8):
            a = math.radians(k * 45)
            out += ('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f"/>'
                    % (x + i * math.cos(a), y + i * math.sin(a),
                       x + o * math.cos(a), y + o * math.sin(a)))
        out = ('<g stroke="#ffc93c" stroke-width="%s" stroke-linecap="round">%s</g>' % (w, out))
    return out + ('<circle cx="%s" cy="%s" r="%s" fill="%s" stroke="%s" stroke-width="3"/>'
                  % (x, y, r, color, edge))


def birds(*pts):
    return ('<g fill="none" stroke="#7a9bb0" stroke-width="3.5" stroke-linecap="round">' +
            ''.join('<path d="M%g,%g q7,-9 14,0 q7,-9 14,0"/>' % (x, y) for x, y in pts) +
            '</g>')


def tree(x, base, r, apples=(), canopy='#4fb84f', trunk='#a06a3c', side=True):
    """x,base = 树干落地点；r = 主树冠半径；apples = 苹果圆心列表"""
    cy = base - 105 - r + 16
    out = ('<rect x="%s" y="%s" width="22" height="105" rx="6" fill="%s"/>'
           % (x - 11, base - 105, trunk))
    out += '<circle cx="%s" cy="%s" r="%s" fill="%s"/>' % (x, cy, r, canopy)
    if side:
        out += '<circle cx="%s" cy="%s" r="%s" fill="%s"/>' % (x - r * 0.7, cy + r * 0.5, r * 0.7, canopy)
        out += '<circle cx="%s" cy="%s" r="%s" fill="%s"/>' % (x + r * 0.7, cy + r * 0.5, r * 0.72, canopy)
        out += '<circle cx="%s" cy="%s" r="%s" fill="%s"/>' % (x, cy - r * 0.7, r * 0.66, canopy)
    for ax, ay in apples:
        out += '<circle cx="%s" cy="%s" r="22" fill="#e5453a"/>' % (ax, ay)
        out += '<path d="M%s,%s q4,-10 8,-12" fill="none" stroke="#3f9c37" stroke-width="4"/>' % (ax, ay - 18)
    return out


def bush(x, base, s=1.0, color='#4fb84f'):
    return ('<g transform="translate(%s,%s) scale(%s)">'
            '<ellipse cx="0" cy="0" rx="46" ry="30" fill="%s"/>'
            '<ellipse cx="-32" cy="10" rx="30" ry="20" fill="%s"/>'
            '<ellipse cx="32" cy="10" rx="30" ry="20" fill="%s"/></g>'
            % (x, base, s, color, color, color))


def flower(x, y, color, s=1.4):
    petals = [(0, -12), (-11.4, -3.7), (-7.1, 9.6), (7.1, 9.6), (11.4, -3.7)]
    out = '<g transform="translate(%s,%s) scale(%s)">' % (x, y, s)
    out += '<path d="M0,10 C-2,0 -1,-8 0,-16" fill="none" stroke="#3f9c37" stroke-width="4" stroke-linecap="round"/>'
    for px, py in petals:
        out += '<circle cx="%s" cy="%s" r="9" fill="%s"/>' % (px, py, color)
    out += '<circle cx="0" cy="0" r="6" fill="#ffe97a"/></g>'
    return out


def tuft(x, y, s=1.0, color='#4aa83f'):
    return ('<g transform="translate(%s,%s) scale(%s)" fill="none" stroke="%s" '
            'stroke-width="3.5" stroke-linecap="round">'
            '<path d="M0,0 C-3,-8 -6,-12 -9,-16"/><path d="M0,0 C0,-9 1,-14 2,-19"/>'
            '<path d="M0,0 C3,-8 7,-11 10,-15"/></g>' % (x, y, s, color))


def stone(x, y, rx=30, ry=18, color='#d8d2c4'):
    return ('<ellipse cx="%s" cy="%s" rx="%s" ry="%s" fill="%s"/>'
            '<ellipse cx="%s" cy="%s" rx="%s" ry="%s" fill="#ffffff" opacity="0.25"/>'
            % (x, y, rx, ry, color, x - rx * 0.2, y - ry * 0.4, rx * 0.5, ry * 0.4))


def fence(x0, y, n, step=34, skip=(), wood='#d8b382', rail='#c9a06a', bw=26, bh=80):
    """y = 地面线；skip = 缺掉的栏板下标（差异：少一根栏板）"""
    out = ''
    for i in range(n):
        if i in skip:
            continue
        out += ('<rect x="%s" y="%s" width="%s" height="%s" rx="5" fill="%s"/>'
                % (x0 + i * step, y - bh, bw, bh, wood))
    out += '<rect x="%s" y="%s" width="%s" height="11" rx="5" fill="%s"/>' % (x0 - 8, y - 58, n * step + 4, rail)
    out += '<rect x="%s" y="%s" width="%s" height="11" rx="5" fill="%s"/>' % (x0 - 8, y - 30, n * step + 4, rail)
    return out


def bench(x, y, wood='#c98b52', leg='#8b6b4a'):
    return ('<g transform="translate(%(x)s,%(y)s)">'
            '<rect x="-52" y="18" width="9" height="32" rx="3" fill="%(leg)s"/>'
            '<rect x="43" y="18" width="9" height="32" rx="3" fill="%(leg)s"/>'
            '<rect x="-60" y="4" width="120" height="13" rx="5" fill="%(wood)s"/>'
            '<rect x="-60" y="-30" width="120" height="13" rx="5" fill="%(wood)s"/>'
            '<rect x="-52" y="-30" width="9" height="38" rx="3" fill="%(leg)s"/>'
            '<rect x="43" y="-30" width="9" height="38" rx="3" fill="%(leg)s"/>'
            '<rect x="-24" y="-27" width="8" height="10" rx="2" fill="%(leg)s"/>'
            '<rect x="-2" y="-27" width="8" height="10" rx="2" fill="%(leg)s"/>'
            '<rect x="20" y="-27" width="8" height="10" rx="2" fill="%(leg)s"/></g>'
            % dict(x=x, y=y, wood=wood, leg=leg))


def lamp(x, base, lit=True):
    """base = 杆子落地 y；灯罩在 base-158 附近。lit=False 就是关灯（差异）"""
    gy = base - 158
    glass = '#ffd93b' if lit else '#b9c2cb'
    return ('<rect x="%s" y="%s" width="9" height="%s" rx="4" fill="#6b7280"/>'
            '<rect x="%s" y="%s" width="44" height="40" rx="9" fill="%s" stroke="#8b949e" stroke-width="3"/>'
            '<path d="M%s,%s L%s,%s L%s,%s L%s,%s Z" fill="#6b7280"/>'
            % (x - 4, gy + 12, base - gy - 12,
               x - 18, gy, glass,
               x - 24, gy + 2, x + 24, gy + 2, x + 14, gy - 18, x - 14, gy - 18))


def pond(cx, cy, rx, ry):
    return ('<ellipse cx="%s" cy="%s" rx="%s" ry="%s" fill="url(#water)"/>'
            '<ellipse cx="%s" cy="%s" rx="%s" ry="%s" fill="#ffffff" opacity="0.22"/>'
            % (cx, cy, rx, ry, cx - rx * 0.1, cy - ry * 0.15, rx * 0.62, ry * 0.42))


def path_center(y0=400, w0=34, w1=270, fill='#f3e3bb', skew=-0.35):
    """中间一条由远及近变宽的小路（skew<0 往左拐）"""
    xl0, xr0 = 400 - w0 / 2, 400 + w0 / 2
    xl1, xr1 = 400 + skew * w1, 400 + (skew + 1) * w1
    return ('<path d="M%g,%g C%g,%g %g,%g %g,%g L%g,%g C%g,%g %g,%g %g,%g Z" fill="%s"/>'
            % (xl0, y0, xl0 + 8, y0 + 62, xl0 - 8, y0 + 130, xl1, H,
               xr1, H, xr0 + 86, y0 + 130, xr0 + 6, y0 + 62, xr0, y0, fill))


def duck(x, y, mirror=False, body='#fffdf5', s=1.0):
    f = ' scale(-1,1)' if mirror else ''
    return ('<g transform="translate(%s,%s) scale(%s)%s">'
            '<ellipse cx="0" cy="0" rx="34" ry="19" fill="%s"/>'
            '<path d="M-28,-3 L-52,-14 L-30,8 Z" fill="#f2ecdc"/>'
            '<circle cx="24" cy="-21" r="16" fill="%s"/>'
            '<path d="M38,-26 L58,-20 L38,-14 Z" fill="#ff9f2e"/>'
            '<circle cx="27" cy="-24" r="2.8" fill="#3b3b3b"/></g>'
            % (x, y, s, f, body, body))


def swan(x, y, mirror=False, s=1.0):
    f = ' scale(-1,1)' if mirror else ''
    return ('<g transform="translate(%s,%s) scale(%s)%s">'
            '<ellipse cx="0" cy="0" rx="42" ry="22" fill="#fffdf5"/>'
            '<path d="M28,2 C40,-10 34,-30 22,-38" fill="none" stroke="#fffdf5" stroke-width="13" stroke-linecap="round"/>'
            '<circle cx="20" cy="-42" r="15" fill="#fffdf5"/>'
            '<path d="M31,-48 L50,-43 L31,-38 Z" fill="#ff9f2e"/>'
            '<circle cx="23" cy="-45" r="2.6" fill="#3b3b3b"/>'
            '<path d="M-40,-6 L-62,-20 L-44,10 Z" fill="#f2ecdc"/></g>'
            % (x, y, s, f))


def lily(x, y, r=30, flower_color=None):
    out = ('<path d="M%g,%g A%g,%g 0 1 1 %g,%g Z" fill="#57bf5a"/>'
           % (x, y - r * 0.5, r, r * 0.5, x + r * 0.6, y - r * 0.1))
    if flower_color:
        out += flower(x + r * 0.2, y - r * 0.2, flower_color, 0.9)
    return out


def dragonfly(x, y, s=1.0, color='#1f8f88', wing='#3fb8ae'):
    """翅膀必须用饱和色：浅蓝翅膀压在浅蓝天空上，消失时色差不到阈值，检测不到"""
    return ('<g transform="translate(%s,%s) scale(%s)">'
            '<ellipse cx="-20" cy="-8" rx="24" ry="12" fill="%s"/>'
            '<ellipse cx="20" cy="-8" rx="24" ry="12" fill="%s"/>'
            '<ellipse cx="-18" cy="8" rx="20" ry="10" fill="%s"/>'
            '<ellipse cx="18" cy="8" rx="20" ry="10" fill="%s"/>'
            '<rect x="-30" y="-5" width="60" height="10" rx="5" fill="%s"/>'
            '<circle cx="-34" cy="0" r="8" fill="%s"/></g>' % (x, y, s, wing, wing, wing, wing, color, color))


def butterfly(x, y, color, s=1.0, color2=None):
    color2 = color2 or color
    return ('<g transform="translate(%s,%s) scale(%s)">'
            '<ellipse cx="-20" cy="-12" rx="24" ry="18" fill="%s"/>'
            '<ellipse cx="20" cy="-12" rx="24" ry="18" fill="%s"/>'
            '<ellipse cx="-16" cy="14" rx="18" ry="14" fill="%s"/>'
            '<ellipse cx="16" cy="14" rx="18" ry="14" fill="%s"/>'
            '<rect x="-5" y="-22" width="10" height="46" rx="5" fill="#4a4a4a"/>'
            '<path d="M-4,-22 q-8,-14 -14,-18" fill="none" stroke="#4a4a4a" stroke-width="3"/>'
            '<path d="M4,-22 q8,-14 14,-18" fill="none" stroke="#4a4a4a" stroke-width="3"/></g>'
            % (x, y, s, color, color2, color, color2))


def bee(x, y, s=1.0):
    return ('<g transform="translate(%s,%s) scale(%s)">'
            '<ellipse cx="-14" cy="-12" rx="20" ry="12" fill="#cfe9ff" opacity="0.9"/>'
            '<ellipse cx="14" cy="-12" rx="20" ry="12" fill="#cfe9ff" opacity="0.9"/>'
            '<ellipse cx="0" cy="0" rx="26" ry="18" fill="#ffd93b"/>'
            '<rect x="-12" y="-18" width="9" height="36" fill="#4a4a4a"/>'
            '<rect x="4" y="-18" width="9" height="36" fill="#4a4a4a"/>'
            '<circle cx="-24" cy="-2" r="11" fill="#4a4a4a"/>'
            '<circle cx="-27" cy="-6" r="3" fill="#fff"/></g>' % (x, y, s))


def balloon(x, y, color, knot=None):
    knot = knot or color
    return ('<path d="M%g,%g C%g,%g %g,%g %g,%g" fill="none" stroke="#c9d6dd" stroke-width="2.5"/>'
            '<path d="M%g,%g L%g,%g L%g,%g Z" fill="%s"/>'
            '<ellipse cx="%s" cy="%s" rx="26" ry="32" fill="%s"/>'
            '<ellipse cx="%s" cy="%s" rx="7" ry="11" fill="#fff" opacity="0.45"/>'
            % (x, y + 32, x + 4, y + 50, x + 10, y + 62, x + 4, y + 80,
               x - 6, y + 29, x + 6, y + 29, x, y + 40, color,
               x, y, color, x - 9, y - 10))


def gate(cx, sign, pillar='#c9a06a', cap='#b0855a', sy=272, w=180):
    """公园门：两根柱子 + 门牌（门牌颜色是差异）；门牌做窄一点，
    否则它的包围盒会和相邻差异挤在一起被合并成一处"""
    return ('<rect x="%s" y="%s" width="40" height="196" rx="8" fill="%s"/>'
            '<rect x="%s" y="%s" width="40" height="196" rx="8" fill="%s"/>'
            '<rect x="%s" y="%s" width="56" height="20" rx="6" fill="%s"/>'
            '<rect x="%s" y="%s" width="56" height="20" rx="6" fill="%s"/>'
            '<rect x="%s" y="%s" width="%s" height="56" rx="12" fill="%s"/>'
            '<circle cx="%s" cy="%s" r="12" fill="#ffffff" opacity="0.5"/>'
            % (cx - 150, sy + 44, pillar, cx + 110, sy + 44, pillar,
               cx - 156, sy + 24, cap, cx + 100, sy + 24, cap,
               cx - w / 2, sy, w, sign, cx + w / 2 - 34, sy + 28))


def booth(x, base, window=True):
    """售票亭；窗户有无是差异"""
    out = ('<rect x="%s" y="%s" width="112" height="%s" rx="10" fill="#f2e2c4"/>'
           '<path d="M%s,%s L%s,%s L%s,%s L%s,%s Z" fill="#8b6b4a"/>'
           '<rect x="%s" y="%s" width="40" height="60" rx="6" fill="#b98a5a"/>'
           % (x - 56, base - 118, 118,
              x - 72, base - 118, x + 72, base - 118, x + 52, base - 146, x - 52, base - 146,
              x - 20, base - 60))
    if window:
        out += ('<rect x="%s" y="%s" width="64" height="54" rx="8" fill="#8fd9f5" stroke="#b98a5a" stroke-width="5"/>'
                '<path d="M%s,%s L%s,%s" stroke="#ffffff" stroke-width="4" opacity="0.7"/>'
                % (x - 32, base - 106, x - 18, base - 92, x - 2, base - 76))
    return out


def slide(x, base, body):
    """滑梯；梯身颜色是差异"""
    return ('<rect x="%s" y="%s" width="20" height="170" rx="8" fill="#9aa3ad"/>'
            '<rect x="%s" y="%s" width="20" height="170" rx="8" fill="#9aa3ad"/>'
            '<rect x="%s" y="%s" width="120" height="18" rx="8" fill="#c98b52"/>'
            '<path d="M%s,%s L%s,%s L%s,%s L%s,%s Z" fill="%s"/>'
            '<rect x="%s" y="%s" width="86" height="16" rx="8" fill="#c98b52"/>'
            % (x - 70, base - 170, x - 30, base - 170,
               x - 78, base - 176,
               x + 20, base - 168, x + 96, base - 12, x + 158, base - 12, x + 82, base - 168, body,
               x - 84, base - 56))


def swing(x, base, seat=True):
    """秋千；座板有无是差异"""
    out = ('<rect x="%s" y="%s" width="16" height="200" rx="6" fill="#9aa3ad"/>'
           '<rect x="%s" y="%s" width="16" height="200" rx="6" fill="#9aa3ad"/>'
           '<rect x="%s" y="%s" width="210" height="16" rx="7" fill="#9aa3ad"/>'
           '<path d="M%s,%s L%s,%s" stroke="#8b949e" stroke-width="5"/>'
           '<path d="M%s,%s L%s,%s" stroke="#8b949e" stroke-width="5"/>'
           % (x - 105, base - 200, x + 89, base - 200,
              x - 113, base - 216,
              x - 52, base - 200, x - 52, base - 78,
              x + 52, base - 200, x + 52, base - 78))
    if seat:
        out += '<rect x="%s" y="%s" width="104" height="20" rx="8" fill="#c98b52"/>' % (x - 52, base - 84)
    return out


def seesaw(x, base, frame):
    """跷跷板；支架颜色是差异"""
    return ('<path d="M%s,%s L%s,%s L%s,%s Z" fill="%s"/>'
            '<rect x="%s" y="%s" width="200" height="18" rx="9" fill="#c98b52" transform="rotate(-8 %s %s)"/>'
            % (x - 26, base, x + 26, base, x, base - 96, frame,
               x - 100, base - 88, x, base - 80))


def ball(x, y, r, color):
    return ('<circle cx="%s" cy="%s" r="%s" fill="%s"/>'
            '<ellipse cx="%s" cy="%s" rx="%s" ry="%s" fill="#fff" opacity="0.4"/>'
            % (x, y, r, color, x - r * 0.3, y - r * 0.35, r * 0.35, r * 0.28))


def kite(x, y, color, tail=True):
    out = ('<path d="M%s,%s L%s,%s L%s,%s L%s,%s Z" fill="%s"/>'
           '<path d="M%s,%s L%s,%s" stroke="#fff" stroke-width="3" opacity="0.7"/>'
           % (x, y - 48, x + 52, y, x, y + 48, x - 52, y, color, x, y + 48, x, y - 48))
    if tail:
        out += ('<path d="M%s,%s q18,26 -6,44 q-22,18 4,38" fill="none" stroke="#8b949e" stroke-width="3"/>'
                '<path d="M%s,%s l16,-10 l-4,18 Z" fill="#ff6fa5"/>' % (x, y + 48, x - 14, y + 100))
    return out


def arch(x, base, color, w=190, h=210):
    """花架拱门；拱架颜色是差异"""
    return ('<path d="M%s,%s L%s,%s L%s,%s L%s,%s Z" fill="%s"/>'
            '<path d="M%s,%s L%s,%s L%s,%s L%s,%s Z" fill="%s"/>'
            '<path d="M%s,%s A%s,%s 0 0 1 %s,%s L%s,%s A%s,%s 0 0 0 %s,%s Z" fill="%s"/>'
            % (x - w / 2 - 18, base - h, x - w / 2 + 18, base - h, x - w / 2 + 18, base, x - w / 2 - 18, base, color,
               x + w / 2 - 18, base - h, x + w / 2 + 18, base - h, x + w / 2 + 18, base, x + w / 2 - 18, base, color,
               x - w / 2 + 18, base - h, w / 2 - 18, 70, x + w / 2 - 18, base - h,
               x + w / 2 - 18, base - h - 18, w / 2 - 18, 70, x - w / 2 + 18, base - h - 18, color))


def watering(x, y, color):
    """喷壶；有无是差异"""
    return ('<g transform="translate(%s,%s)">'
            '<rect x="-40" y="-46" width="80" height="62" rx="12" fill="%s"/>'
            '<path d="M40,-30 L78,-52 L86,-42 L46,-14 Z" fill="%s"/>'
            '<path d="M-40,-34 q-30,-6 -34,-26" fill="none" stroke="%s" stroke-width="10"/>'
            '<rect x="-26" y="-62" width="52" height="16" rx="8" fill="%s"/></g>'
            % (x, y, color, color, color, color))


def basket(x, y):
    """野餐篮；有无是差异"""
    return ('<g transform="translate(%s,%s)">'
            '<path d="M-56,-22 q56,42 112,0 Z" fill="#c98b52"/>'
            '<rect x="-60" y="-34" width="120" height="16" rx="7" fill="#8b6b4a"/>'
            '<path d="M-34,-34 a34,30 0 0 1 68,0" fill="none" stroke="#8b6b4a" stroke-width="7"/>'
            '<path d="M-56,-14 h112" stroke="#8b6b4a" stroke-width="4"/>'
            '<path d="M-30,-18 v14 M0,-6 v12 M30,-18 v14" stroke="#8b6b4a" stroke-width="4"/></g>'
            % (x, y))


def blanket(x, y, rx, ry, color, color2='#ffffff'):
    """野餐毯；颜色是差异"""
    out = '<ellipse cx="%s" cy="%s" rx="%s" ry="%s" fill="%s"/>' % (x, y, rx, ry, color)
    for i in range(-2, 3):
        out += ('<path d="M%g,%g q%g,%g 0,%g" fill="none" stroke="%s" stroke-width="4" opacity="0.75"/>'
                % (x + i * rx * 0.36, y - ry * 0.7, rx * 0.08, ry, ry * 1.4, color2))
    return out


def mushroom(x, y, cap, s=1.0):
    return ('<g transform="translate(%s,%s) scale(%s)">'
            '<rect x="-11" y="-14" width="22" height="34" rx="8" fill="#fdf3e0"/>'
            '<path d="M-30,-12 a30,26 0 0 1 60,0 Z" fill="%s"/>'
            '<circle cx="-12" cy="-22" r="5" fill="#fff" opacity="0.7"/>'
            '<circle cx="10" cy="-18" r="4" fill="#fff" opacity="0.7"/></g>' % (x, y, s, cap))


def squirrel(x, y, s=1.0):
    return ('<g transform="translate(%s,%s) scale(%s)">'
            '<path d="M30,10 q46,-6 34,-52 q-26,20 -40,44 Z" fill="#c98b52"/>'
            '<ellipse cx="0" cy="0" rx="30" ry="26" fill="#d9a06a"/>'
            '<circle cx="-22" cy="-26" r="19" fill="#d9a06a"/>'
            '<circle cx="-38" cy="-26" r="7" fill="#f0d3b4"/>'
            '<circle cx="-27" cy="-30" r="3.4" fill="#3b3b3b"/>'
            '<path d="M-34,-40 q6,-14 16,-16" fill="none" stroke="#c98b52" stroke-width="7"/></g>'
            % (x, y, s))


def boat(x, y, color, s=1.0):
    """小船；船身颜色是差异"""
    return ('<g transform="translate(%s,%s) scale(%s)">'
            '<path d="M-90,0 q90,44 180,0 Z" fill="%s"/>'
            '<rect x="-6" y="-96" width="9" height="96" rx="4" fill="#8b6b4a"/>'
            '<path d="M8,-92 L8,-14 L74,-14 Z" fill="#fff6e5"/>'
            '<path d="M-10,-80 L-10,-14 L-64,-14 Z" fill="#ffe9c9"/></g>' % (x, y, s, color))


def reeds(x, base, n=5, step=26, skip=(), color='#4aa83f'):
    """芦苇；skip = 少掉的那根（差异）"""
    out = ''
    for i in range(n):
        if i in skip:
            continue
        bx = x + i * step
        out += ('<path d="M%g,%g q%g,-70 %g,-96" fill="none" stroke="%s" stroke-width="9" stroke-linecap="round"/>'
                '<ellipse cx="%g" cy="%g" rx="11" ry="26" fill="#8b6b4a"/>'
                % (bx, base, -6, 2, color, bx + 2, base - 112))
    return out


# ==========================================================================
# 二、6 个公园场景（v = 'A' / 'B'，只有差异处才读 v）
# ==========================================================================
# ---- 6 个公园场景：hard 6 处差异；hell 复用同场景 + 换天色 + 多 1 处（7 处）----
def _base(pal):
    return [defs(pal), sky(), hills((180, 420, 260, 90, pal['hill1']),
                                    (620, 430, 300, 100, pal['hill2'])), grass(400)]


def scene_gate(v, pal, hell):
    """公园大门"""
    o = _base(pal)
    o.append(cloud(560, 120) + cloud(760, 250) + birds((250, 170), (312, 206)))
    o.append(path_center())
    o.append(tree(60, 470, 50, apples=((40, 430),)))
    o.append(fence(40, 570, 4) + tuft(430, 580) + stone(760, 560, 26, 16))
    o.append(gate(400, '#e5533f' if v == 'A' else '#3f7ff0'))                  # 门牌颜色
    if v == 'A':
        o.append(balloon(150, 140, '#ffd93b'))                                 # 气球数量
    o.append(balloon(660, 380, '#ef5350'))
    if v == 'A':
        o.append(cloud(400, 140))                                              # 云朵数量
    o.append(sun(650, 140, 34, rays=(46, 66, 14) if v == 'A' else None))       # 太阳光芒
    o.append(booth(650, 559, window=(v == 'A')))                              # 售票亭窗户
    fc = '#ff6fa5' if v == 'A' else '#b06ff0'                                  # 花丛颜色
    o.append(flower(120, 470, fc, 1.8) + flower(160, 495, fc, 1.8) + flower(190, 465, fc, 1.8))
    if hell and v == 'A':                                                      # 地狱第 7 处
        o.append(butterfly(400, 480, '#8b5cf6', 1.0))
    return o


def scene_pond(v, pal, hell):
    """池塘边"""
    o = _base(pal)
    o.append(cloud(760, 200) + birds((300, 190), (362, 224)))
    if v == 'A':
        o.append(cloud(400, 120))                                          # 云朵数量
    o.append(pond(450, 480, 250, 80))
    o.append(tree(760, 470, 50, apples=()))
    o.append(reeds(560, 440, 3, step=30) + tuft(150, 580) + stone(90, 570, 24, 14))
    o.append(duck(560, 430))                                                   # 干扰：一直在
    if v == 'A':
        o.append(duck(400, 480))                                               # 鸭子数量
    if v == 'A':
        o.append(lily(650, 480, 32, flower_color='#ff6fa5'))                   # 荷叶
    if v == 'A':
        o.append(dragonfly(650, 180))                                          # 蜻蜓
    stones = [(140, 500), (70, 545)]
    if v == 'A':
        stones.append((240, 560))                                              # 石头数量
    for sx, sy in stones:
        o.append(stone(sx, sy, 30, 18))
    o.append(tree(150, 439, 70, canopy='#4fb84f' if v == 'A' else '#c9d94a'))  # 柳树颜色
    if hell and v == 'A':
        o.append(balloon(400, 300, '#ffd93b'))
    return o


def scene_play(v, pal, hell):
    """儿童乐园"""
    o = _base(pal)
    o.append('<ellipse cx="380" cy="520" rx="260" ry="74" fill="%s"/>' % pal['sand'])
    o.append(cloud(430, 100) + cloud(760, 230) + birds((330, 190), (392, 224)))
    o.append(bush(700, 470, 0.8) + tuft(760, 570) + stone(300, 580, 22, 13))
    o.append(slide(300, 470, '#f2a13c' if v == 'A' else '#5aa9e6'))            # 滑梯颜色
    o.append(swing(600, 470, seat=(v == 'A')))                                 # 秋千座板
    o.append(seesaw(150, 480, '#8b6b4a' if v == 'A' else '#4aa8e0'))            # 跷跷板支架
    if v == 'A':
        o.append(ball(650, 500, 34, '#ef5350'))                                # 皮球
    if v == 'A':
        o.append(cloud(150, 120))                                              # 云朵数量
    if v == 'A':
        o.append(kite(650, 140, '#8b5cf6', tail=False))                        # 风筝
    if hell and v == 'A':
        o.append(balloon(400, 140, '#ffd93b'))
    return o


def scene_garden(v, pal, hell):
    """花园花圃"""
    o = _base(pal)
    o.append(cloud(520, 100) + cloud(760, 240) + birds((360, 180), (422, 214)))
    o.append(path_center(skew=0.3))
    o.append(bush(60, 520, 0.9) + tuft(520, 580) + stone(240, 570, 24, 14))
    o.append(arch(650, 380, '#8fd15a' if v == 'A' else '#e5533f'))             # 花架颜色
    o.append(butterfly(400, 140, '#ff6fa5' if v == 'A' else '#7f6ff0', 1.0))   # 蝴蝶颜色
    if v == 'A':
        o.append(cloud(150, 120))                                              # 云朵数量
    fc = '#ff6fa5' if v == 'A' else '#ffb703'                                  # 花坛颜色
    o.append(flower(120, 470, fc, 1.8) + flower(160, 495, fc, 1.8) + flower(190, 465, fc, 1.8))
    if v == 'A':
        o.append(watering(400, 500, '#4aa8e0'))                                # 喷壶
    if v == 'A':
        o.append(bee(150, 320, 1.0))                                           # 蜜蜂
    if hell and v == 'A':
        o.append(balloon(400, 300, '#ffd93b'))
    return o


def scene_picnic(v, pal, hell):
    """树林野餐"""
    o = _base(pal)
    o.append(cloud(400, 220) + birds((430, 200), (492, 234)))
    o.append(tree(760, 470, 50, apples=((730, 410),)))
    o.append(tuft(560, 580) + stone(320, 580, 26, 15) + bush(700, 560, 0.7))
    apple = ((150, 300),) if v == 'A' else ()                                  # 苹果数量
    o.append(tree(150, 430, 84, apples=((100, 330),) + apple))
    o.append(blanket(150, 500, 110, 60, '#ff8fb1' if v == 'A' else '#6fa8f0'))  # 野餐毯颜色
    if v == 'A':
        o.append(basket(400, 480))                                             # 野餐篮
    if v == 'A':
        o.append(mushroom(650, 510, '#e5533f'))                                # 蘑菇数量
    o.append(mushroom(710, 540, '#e5533f'))
    if v == 'A':
        o.append(cloud(150, 120))                                              # 云朵数量
    if v == 'A':
        o.append(kite(650, 140, '#8b5cf6', tail=False))                        # 风筝
    if hell and v == 'A':
        o.append(balloon(400, 140, '#ffd93b'))
    return o


def scene_lake(v, pal, hell):
    """湖边散步"""
    o = _base(pal)
    o.append('<ellipse cx="540" cy="490" rx="230" ry="95" fill="url(#water)"/>')
    o.append('<ellipse cx="520" cy="466" rx="150" ry="42" fill="#ffffff" opacity="0.18"/>')
    o.append(birds((430, 200), (492, 234)))
    o.append(tree(80, 470, 44, apples=()))
    o.append(swan(430, 520, s=0.9) + tuft(240, 570) + stone(180, 585, 24, 14))
    o.append(sun(150, 140, 30 if v == 'A' else 42, rays=None,
                 color=pal['sun'], edge=pal['sun_edge']))                       # 落日大小
    o.append(cloud(650, 140) if v == 'A' else '')                               # 云朵数量
    o.append(bench(150, 480) if v == 'A' else '')                               # 长椅
    o.append(reeds(400, 480, 4, step=30, skip=() if v == 'A' else (2,)))        # 芦苇数量
    o.append(boat(650, 470, '#e5533f' if v == 'A' else '#3f7ff0', s=0.9))       # 小船颜色
    o.append(lamp(240, 440, lit=(v == 'A')))                                    # 路灯亮灭
    if hell and v == 'A':
        o.append(balloon(400, 140, '#ffd93b'))
    return o


# ---- 工地道具 ----
def dirt(cx, cy, rx=340, ry=92, color='#c9a06a'):
    return ('<ellipse cx="%s" cy="%s" rx="%s" ry="%s" fill="%s"/>'
            '<ellipse cx="%s" cy="%s" rx="%s" ry="%s" fill="#ffffff" opacity="0.12"/>'
            % (cx, cy, rx, ry, color, cx - rx * 0.15, cy - ry * 0.3, rx * 0.7, ry * 0.5))


def cone(x, base, color='#ff7a1a'):
    """锥筒（有无是差异）；base = 落地 y"""
    return ('<path d="M%s,%s L%s,%s L%s,%s Z" fill="%s"/>'
            '<rect x="%s" y="%s" width="76" height="14" rx="6" fill="#e8620f"/>'
            '<rect x="%s" y="%s" width="52" height="12" rx="5" fill="#ffffff"/>'
            % (x - 34, base, x + 34, base, x, base - 96, color,
               x - 38, base - 6, x - 22, base - 58))


def pipes(x, base, n=3, color='#cfd6dd'):
    """水泥管堆（有无是差异）"""
    out = ''
    for i in range(n):
        cx = x - 40 + i * 40
        out += ('<circle cx="%s" cy="%s" r="26" fill="%s"/>'
                '<circle cx="%s" cy="%s" r="13" fill="#9aa3ad"/>' % (cx, base - 26, color, cx, base - 26))
    out += '<rect x="%s" y="%s" width="128" height="14" rx="6" fill="#9aa3ad"/>' % (x - 64, base - 8)
    return out


def bricks(x, base, rows=3, color='#d1603d'):
    """砖堆（数量差异用）"""
    out = ''
    for r in range(rows):
        for c in range(rows - r):
            out += ('<rect x="%s" y="%s" width="46" height="24" rx="4" fill="%s"/>'
                    % (x - (rows - r) * 24 + c * 48, base - 24 * (r + 1), color))
    return out


def windsock(x, base, color='#ff7a1a'):
    """风向袋（杆 + 袋子；有无是差异）"""
    return ('<rect x="%s" y="%s" width="12" height="%s" rx="5" fill="#8b949e"/>'
            '<path d="M%s,%s L%s,%s L%s,%s L%s,%s Z" fill="%s"/>'
            % (x - 6, base - 262, 262, x + 6, base - 262, x + 96, base - 250, x + 84, base - 214,
               x + 4, base - 226, color))


def barrier(x, y, n=6):
    """工地围挡（蓝白斜条）"""
    out = ''
    for i in range(n):
        cx = x + i * 62
        out += ('<rect x="%s" y="%s" width="56" height="76" rx="6" fill="#eaf4ff"/>'
                '<path d="M%s,%s L%s,%s L%s,%s L%s,%s Z" fill="#4a86d8"/>'
                % (cx, y - 76, cx, y - 20, cx + 22, y - 76, cx + 56, y - 76, cx + 34, y - 20))
    return out


def building(x, base, color='#cbbba0', roof='#9c8f7a'):
    """远处半成品楼房"""
    return ('<rect x="%s" y="%s" width="170" height="150" rx="6" fill="%s"/>'
            '<rect x="%s" y="%s" width="186" height="18" rx="5" fill="%s"/>'
            '<rect x="%s" y="%s" width="34" height="30" rx="4" fill="#8fd9f5"/>'
            '<rect x="%s" y="%s" width="34" height="30" rx="4" fill="#8fd9f5"/>'
            '<rect x="%s" y="%s" width="34" height="30" rx="4" fill="#8fd9f5"/>'
            % (x, base - 150, color, x - 8, base - 168, roof,
               x + 18, base - 122, x + 70, base - 122, x + 118, base - 122))


# ---- 工程车（统一朝左，作业部件在右侧 = 差异所在）----
def _wheel(x, y, r=26):
    return ('<circle cx="%s" cy="%s" r="%s" fill="#3f3f3f"/>'
            '<circle cx="%s" cy="%s" r="%s" fill="#b9b9b9"/>' % (x, y, r, x, y, r * 0.45))


def _track(x, y, w=250, h=48, wheels=3):
    out = '<rect x="%s" y="%s" width="%s" height="%s" rx="%s" fill="#4a4a4a"/>' % (x, y - h, w, h, h / 2)
    for i in range(wheels):
        out += '<circle cx="%.0f" cy="%s" r="14" fill="#9a9a9a"/>' % (x + 34 + i * (w - 68) / (wheels - 1), y - h / 2)
    return out


def excavator(x, y, body, part):
    """挖掘机：履带 + 机身 + 驾驶室 + 臂 + 挖斗（颜色差异）"""
    return (_track(x - 150, y, 250, 48, 3)
            + '<rect x="%s" y="%s" width="176" height="82" rx="14" fill="%s"/>' % (x - 140, y - 130, body)
            + '<rect x="%s" y="%s" width="94" height="74" rx="12" fill="%s"/>' % (x - 132, y - 204, body)
            + '<rect x="%s" y="%s" width="66" height="48" rx="7" fill="#8fd9f5"/>' % (x - 118, y - 194)
            + '<path d="M%s,%s L%s,%s L%s,%s" fill="none" stroke="#e0a33c" stroke-width="26" stroke-linecap="round"/>'
              % (x + 26, y - 160, x + 112, y - 206, x + 172, y - 126)
            + '<path d="M%s,%s L%s,%s" stroke="#e0a33c" stroke-width="24" stroke-linecap="round"/>'
              % (x + 172, y - 126, x + 186, y - 74)
            + '<path d="M%s,%s q56,4 50,58 q-56,10 -56,-22 Z" fill="%s"/>' % (x + 182, y - 86, part))


def bulldozer(x, y, body, part):
    """推土机：履带 + 机身 + 驾驶室 + 推土铲（颜色差异）"""
    return (_track(x - 150, y, 240, 50, 3)
            + '<rect x="%s" y="%s" width="176" height="80" rx="14" fill="%s"/>' % (x - 140, y - 132, body)
            + '<rect x="%s" y="%s" width="92" height="76" rx="12" fill="%s"/>' % (x - 132, y - 208, body)
            + '<rect x="%s" y="%s" width="64" height="50" rx="7" fill="#8fd9f5"/>' % (x - 118, y - 198)
            + '<rect x="%s" y="%s" width="18" height="46" rx="8" fill="#6b7280"/>' % (x - 30, y - 250)
            + '<circle cx="%s" cy="%s" r="16" fill="#c9ced6" opacity="0.85"/>' % (x - 21, y - 268)
            + '<path d="M%s,%s L%s,%s L%s,%s L%s,%s Z" fill="%s"/>' % (x + 152, y - 128, x + 196, y - 128,
                                                                      x + 196, y - 4, x + 152, y - 4, part))


def mixer_truck(x, y, cab, part):
    """水泥搅拌车：轮子 + 底盘 + 驾驶室 + 搅拌筒（颜色差异）"""
    return ('<rect x="%s" y="%s" width="330" height="52" rx="12" fill="#6b7280"/>' % (x - 165, y - 96)
            + _wheel(x - 116, y - 30) + _wheel(x + 66, y - 30) + _wheel(x + 118, y - 30)
            + '<rect x="%s" y="%s" width="96" height="82" rx="12" fill="%s"/>' % (x - 168, y - 174, cab)
            + '<rect x="%s" y="%s" width="66" height="46" rx="7" fill="#8fd9f5"/>' % (x - 156, y - 164)
            + '<path d="M%s,%s q140,-24 210,10 q-30,86 -210,44 Z" fill="%s"/>' % (x - 30, y - 190, part)
            + '<path d="M%s,%s L%s,%s" stroke="#8b6b4a" stroke-width="14"/>' % (x + 96, y - 150, x + 168, y - 96))


def dump_truck(x, y, cab, part):
    """自卸卡车：轮子 + 底盘 + 驾驶室 + 车斗（颜色差异）+ 土方"""
    return ('<rect x="%s" y="%s" width="330" height="50" rx="12" fill="#6b7280"/>' % (x - 165, y - 94)
            + _wheel(x - 116, y - 28) + _wheel(x + 66, y - 28) + _wheel(x + 118, y - 28)
            + '<rect x="%s" y="%s" width="96" height="80" rx="12" fill="%s"/>' % (x - 168, y - 172, cab)
            + '<rect x="%s" y="%s" width="66" height="44" rx="7" fill="#8fd9f5"/>' % (x - 156, y - 162)
            + '<path d="M%s,%s L%s,%s L%s,%s L%s,%s Z" fill="%s"/>' % (x - 34, y - 176, x + 156, y - 176,
                                                                      x + 140, y - 92, x - 42, y - 92, part)
            + '<path d="M%s,%s q90,-52 180,0 Z" fill="#b08a55"/>' % (x - 30, y - 172))


def road_roller(x, y, body, part):
    """压路机：机身 + 驾驶室 + 大滚筒（颜色差异）"""
    return ('<rect x="%s" y="%s" width="220" height="90" rx="18" fill="%s"/>' % (x - 130, y - 160, body)
            + '<rect x="%s" y="%s" width="96" height="76" rx="12" fill="%s"/>' % (x - 122, y - 232, body)
            + '<rect x="%s" y="%s" width="66" height="48" rx="7" fill="#8fd9f5"/>' % (x - 108, y - 222)
            + _wheel(x - 96, y - 34, 34)
            + '<circle cx="%s" cy="%s" r="62" fill="%s"/>' % (x + 130, y - 62, part)
            + '<circle cx="%s" cy="%s" r="22" fill="#9aa3ad"/>' % (x + 130, y - 62))


def crane_truck(x, y, cab, part):
    """汽车吊：轮子 + 底盘 + 驾驶室 + 支腿 + 吊臂（颜色差异）+ 吊钩"""
    return ('<rect x="%s" y="%s" width="320" height="48" rx="12" fill="#6b7280"/>' % (x - 160, y - 92)
            + _wheel(x - 112, y - 28) + _wheel(x + 40, y - 28) + _wheel(x + 122, y - 28)
            + '<rect x="%s" y="%s" width="92" height="76" rx="12" fill="%s"/>' % (x - 164, y - 166, cab)
            + '<rect x="%s" y="%s" width="64" height="42" rx="7" fill="#8fd9f5"/>' % (x - 152, y - 156)
            + '<rect x="%s" y="%s" width="120" height="26" rx="10" fill="#8b949e"/>' % (x - 20, y - 116)
            + '<path d="M%s,%s L%s,%s L%s,%s L%s,%s Z" fill="%s"/>' % (x - 10, y - 104, x + 152, y - 150,
                                                                      x + 162, y - 118, x + 4, y - 76, part)
            + '<path d="M%s,%s L%s,%s" stroke="#6b7280" stroke-width="5"/>' % (x + 158, y - 134, x + 158, y - 60)
            + '<rect x="%s" y="%s" width="30" height="20" rx="5" fill="#6b7280"/>' % (x + 143, y - 60))


def site_scene(v, pal, hell, machine, mcolor, pcolor, extra_prop=None):
    """工地场景通用底：云/太阳/围挡/土场 + 一台工程车 + 锥筒/水泥管/风向袋"""
    o = [defs(pal), sky(), hills((180, 420, 260, 90, pal['hill1']),
                                 (620, 430, 300, 100, pal['hill2'])), grass(400)]
    o.append(building(150, 400))
    o.append(barrier(560, 400, 4))
    o.append(dirt(500, 520, 360, 96))
    o.append(cloud(760, 250) + birds((360, 210), (422, 244)))
    o.append(machine(500, 500))                                                # 挖斗/铲/筒 颜色是差异
    if hell and v == 'A':
        o.append(balloon(400, 140, '#ffd93b'))                                 # 地狱第 7 处
    if v == 'A':
        o.append(cloud(150, 130))                                              # 云朵数量
    o.append(sun(650, 130, 34, rays=(46, 66, 14) if v == 'A' else None))       # 太阳光芒
    if v == 'A':
        o.append(pipes(120, 512, 3))                                           # 水泥管
    if v == 'A':
        o.append(cone(300, 520))                                               # 锥筒
    o.append(windsock(250, 545, '#ff7a1a' if v == 'A' else '#4a86d8'))          # 风向袋颜色
    return o


def scene_excavator(v, pal, hell):
    return site_scene(v, pal, hell,
                      lambda x, y: excavator(x, y, '#e8b53c', '#ff8f3c' if v == 'A' else '#3f7ff0'),
                      '#e8b53c', '#ff8f3c')


def scene_dozer(v, pal, hell):
    return site_scene(v, pal, hell,
                      lambda x, y: bulldozer(x, y, '#e8b53c', '#ffd93b' if v == 'A' else '#4aa8e0'),
                      '#e8b53c', '#ffd93b')


def scene_mixer(v, pal, hell):
    return site_scene(v, pal, hell,
                      lambda x, y: mixer_truck(x, y, '#c94f3d', '#ff8f3c' if v == 'A' else '#7fce8a'),
                      '#c94f3d', '#ff8f3c')


def scene_dump(v, pal, hell):
    return site_scene(v, pal, hell,
                      lambda x, y: dump_truck(x, y, '#2f7fd0', '#4aa8e0' if v == 'A' else '#ff6fa5'),
                      '#2f7fd0', '#4aa8e0')


def scene_roller(v, pal, hell):
    return site_scene(v, pal, hell,
                      lambda x, y: road_roller(x, y, '#e8b53c', '#c9c95e' if v == 'A' else '#b06ff0'),
                      '#e8b53c', '#c9c95e')


def scene_crane_truck(v, pal, hell):
    return site_scene(v, pal, hell,
                      lambda x, y: crane_truck(x, y, '#2f7fd0', '#ffd93b' if v == 'A' else '#5aa9e6'),
                      '#2f7fd0', '#ffd93b')


PAL_DAY = dict(sky_top='#7fc4ee', sky_bottom='#d8f1ff', grass_top='#8ed86e', grass_bottom='#5fbf52',
               hill1='#a9dd8d', hill2='#9ad47f', water1='#8fdcf7', water2='#33a6dd',
               sand='#f0dfae', sun='#ffd93b', sun_edge='#f5b721')

# 地狱用不同时段的天色（只换背景渐变，不动物体颜色：物体与背景的色差不会被削弱，
# 消失/大小类差异才不会被压到检测阈值以下）
PALETTES = [
    dict(PAL_DAY, sky_top='#c9561a', sky_bottom='#f0b878', grass_top='#7cbc63', grass_bottom='#4f9448',
         hill1='#8dbf72', hill2='#7cb066'),                                    # 黄昏
    dict(PAL_DAY, sky_top='#6fc3ea', sky_bottom='#d8f2ff', grass_top='#9de27c', grass_bottom='#68c65a',
         hill1='#b3e39a', hill2='#a3da8b'),                                    # 清晨
    dict(PAL_DAY, sky_top='#4aa8e0', sky_bottom='#c8e8ff', grass_top='#86d266', grass_bottom='#55b54b',
         hill1='#9fd881', hill2='#8fd073'),                                    # 午后
    dict(PAL_DAY, sky_top='#7cc4ec', sky_bottom='#dff4ff', grass_top='#a3e281', grass_bottom='#6fd061',
         hill1='#bde8a4', hill2='#ade094'),                                    # 春日
    dict(PAL_DAY, sky_top='#6fb0d6', sky_bottom='#d6e9f8', grass_top='#c9c95e', grass_bottom='#a3ad45',
         hill1='#c3c96e', hill2='#b3bd5e'),                                    # 秋日
    dict(PAL_DAY, sky_top='#2b3f6b', sky_bottom='#4a6a9c', grass_top='#3f6b4a', grass_bottom='#26452f',
         hill1='#3a6247', hill2='#33583f', water1='#3f6f96', water2='#213f5c',
         sand='#6b6b5c', sun='#fdf3c0', sun_edge='#e8d98a'),                   # 夜晚
]

SCENES = []
_HARD = [
    ('hard-1', '公园大门', scene_gate,
     ['太阳光芒', '云朵数量', '气球数量', '门牌颜色', '售票亭窗户', '花丛颜色'], '蝴蝶'),
    ('hard-2', '池塘边', scene_pond,
     ['柳树颜色', '云朵数量', '蜻蜓', '鸭子数量', '荷叶', '石头数量'], '气球数量'),
    ('hard-3', '儿童乐园', scene_play,
     ['云朵数量', '风筝', '滑梯颜色', '秋千座板', '跷跷板支架', '皮球'], '气球数量'),
    ('hard-4', '花园花圃', scene_garden,
     ['云朵数量', '花架颜色', '蝴蝶颜色', '花坛颜色', '喷壶', '蜜蜂'], '气球数量'),
    ('hard-5', '树林野餐', scene_picnic,
     ['云朵数量', '风筝', '苹果数量', '野餐毯颜色', '野餐篮', '蘑菇数量'], '气球数量'),
    ('hard-6', '湖边散步', scene_lake,
     ['落日大小', '云朵数量', '长椅', '芦苇数量', '小船颜色', '路灯亮灭'], '气球数量'),
    ('hard-7', '挖掘机作业', scene_excavator,
     ['云朵数量', '太阳光芒', '挖斗颜色', '水泥管', '锥筒', '风向袋'], '风筝'),
    ('hard-8', '推土机平整', scene_dozer,
     ['云朵数量', '太阳光芒', '推土铲颜色', '水泥管', '锥筒', '风向袋'], '风筝'),
    ('hard-9', '水泥搅拌车', scene_mixer,
     ['云朵数量', '太阳光芒', '搅拌筒颜色', '水泥管', '锥筒', '风向袋'], '风筝'),
    ('hard-10', '自卸卡车', scene_dump,
     ['云朵数量', '太阳光芒', '车斗颜色', '水泥管', '锥筒', '风向袋'], '风筝'),
    ('hard-11', '压路机', scene_roller,
     ['云朵数量', '太阳光芒', '滚筒颜色', '水泥管', '锥筒', '风向袋'], '风筝'),
    ('hard-12', '汽车吊', scene_crane_truck,
     ['云朵数量', '太阳光芒', '吊臂颜色', '水泥管', '锥筒', '风向袋'], '风筝'),
]
_HELL_SUFFIX = ['黄昏', '清晨', '午后', '春日', '秋日', '夜晚']
for _k, (_key, _name, _build, _diffs, _extra) in enumerate(_HARD):
    SCENES.append(dict(key=_key, name=_name, build=_build, pal=PAL_DAY, hell=False,
                       diffs=_diffs, want=6))
    SCENES.append(dict(key='hell-%d' % (_k + 1), name=_name + '·' + _HELL_SUFFIX[_k % len(_HELL_SUFFIX)],
                       build=_build, pal=PALETTES[_k % len(PALETTES)], hell=True,
                       diffs=_diffs + [_extra], want=7))


# ==========================================================================
# 三、渲染 + 自检
# ==========================================================================
def write_svg(sc, v):
    body = sc['build'](v, sc['pal'], sc['hell'])
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 600" width="800" height="600">\n'
           '  <title>%s - %s</title>\n' % (sc['name'], v) +
           '\n'.join('  ' + s for s in body if s) + '\n</svg>\n')
    path = os.path.join(SVG_DIR, '%s-%s.svg' % (sc['key'], v))
    with open(path, 'w', encoding='utf-8') as f:
        f.write(svg)
    return path


def chrome_shot(html_path, png_path, size, tag, tries=3):
    """无头 Chrome 截图。两个坑：① 截完图不会自己退出，必须等文件出现后 kill；
    ② 偶发启动失败（连续跑几十次会遇到），所以失败要重试，每次换一个干净的配置目录。"""
    import shutil
    last = ''
    for attempt in range(1, tries + 1):
        profile = '/tmp/dshc-svg-%s-%d' % (tag, attempt)
        if os.path.exists(png_path):
            os.remove(png_path)
        cmd = [CHROME, '--headless=new', '--no-sandbox', '--disable-gpu', '--disable-crash-reporter',
               '--disable-breakpad', '--hide-scrollbars', '--force-device-scale-factor=1',
               '--virtual-time-budget=4000', '--user-data-dir=%s' % profile,
               '--window-size=%d,%d' % size, '--screenshot=%s' % png_path, 'file://' + html_path]
        p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(80):
                if os.path.exists(png_path) and os.path.getsize(png_path) > 0:
                    time.sleep(0.6)
                    break
                time.sleep(0.4)
        finally:
            p.kill()
            p.wait()
            subprocess.run(['pkill', '-9', '-f', 'dshc-svg-%s' % tag],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            shutil.rmtree(profile, ignore_errors=True)
        if os.path.exists(png_path) and os.path.getsize(png_path) >= 4000:
            return png_path
        last = ('%d 字节' % os.path.getsize(png_path)) if os.path.exists(png_path) else '文件缺失'
        print('      Chrome 第 %d 次截图异常（%s），重试…' % (attempt, last))
    raise RuntimeError('Chrome 截图连续 %d 次失败：%s（%s）' % (tries, png_path, last))


def render_jpg(svg_path, jpg_path, tag):
    html = svg_path[:-4] + '.html'
    with open(html, 'w', encoding='utf-8') as f:
        f.write('<html><body style="margin:0;background:#fff">'
                '<img src="%s" width="%d" height="%d" style="display:block"></body></html>'
                % (os.path.basename(svg_path), GAME[0], GAME[1]))
    png = svg_path[:-4] + '.png'
    try:
        chrome_shot(html, png, GAME, tag)
        Image.open(png).convert('RGB').save(
            jpg_path, 'JPEG', quality=88, subsampling=0, optimize=True, progressive=True)
    finally:
        for p in (html, png):
            if os.path.exists(p):
                os.remove(p)
    return os.path.getsize(jpg_path)


# ---- 差异检测：复刻 index.html 的 computePhotoDiffs() ----
def detect(a_path, b_path):
    ia = Image.open(a_path).convert('RGB')
    ib = Image.open(b_path).convert('RGB')
    w, h = ia.size
    da = np.asarray(ia, dtype=np.int16)
    db = np.asarray(ib, dtype=np.int16)
    delta = np.abs(da - db).sum(axis=2)
    mask = (delta > PHOTO_DIFF_TH).astype(np.uint8)

    seen = np.zeros_like(mask)
    clusters = []
    idx = np.argwhere(mask)
    for sy, sx in idx:
        if seen[sy, sx]:
            continue
        stack = [(sy, sx)]
        seen[sy, sx] = 1
        area = 0
        sx_sum = sy_sum = 0
        dsum = 0
        minx, miny, maxx, maxy = w, h, -1, -1
        while stack:
            y, x = stack.pop()
            area += 1
            sx_sum += x
            sy_sum += y
            dsum += delta[y, x]
            if x < minx: minx = x
            if x > maxx: maxx = x
            if y < miny: miny = y
            if y > maxy: maxy = y
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    ny, nx = y + dy, x + dx
                    if 0 <= nx < w and 0 <= ny < h and mask[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = 1
                        stack.append((ny, nx))
        clusters.append(dict(area=area, bbox=[minx, miny, maxx, maxy],
                             cx=sx_sum / area, cy=sy_sum / area, strength=dsum / area))

    def near(a, b):
        nx0, ny0 = min(a[0], b[0]), min(a[1], b[1])
        nx1, ny1 = max(a[2], b[2]), max(a[3], b[3])
        return (a[0] <= b[2] + PHOTO_MERGE and a[2] >= b[0] - PHOTO_MERGE and
                a[1] <= b[3] + PHOTO_MERGE and a[3] >= b[1] - PHOTO_MERGE and
                nx1 - nx0 <= PHOTO_MAX_W and ny1 - ny0 <= PHOTO_MAX_H)

    groups = []
    for c in clusters:
        if c['area'] < PHOTO_MIN_AREA:
            continue
        box = c['bbox']
        hit = None
        for g in groups:
            if near(box, g['bbox']) or math.hypot(c['cx'] - g['cx'], c['cy'] - g['cy']) < PHOTO_MIN_GAP:
                hit = g
                break
        if hit:
            hit['members'].append(c)
            hit['bbox'] = [min(hit['bbox'][0], box[0]), min(hit['bbox'][1], box[1]),
                           max(hit['bbox'][2], box[2]), max(hit['bbox'][3], box[3])]
            hit['area'] += c['area']
        else:
            groups.append(dict(members=[c], bbox=box, area=c['area'], cx=c['cx'], cy=c['cy']))
    for _ in range(2):
        i = 0
        while i < len(groups):
            j = i + 1
            while j < len(groups):
                a, b = groups[i], groups[j]
                if near(a['bbox'], b['bbox']) or math.hypot(a['cx'] - b['cx'], a['cy'] - b['cy']) < PHOTO_MIN_GAP:
                    a['members'] += b['members']
                    a['bbox'] = [min(a['bbox'][0], b['bbox'][0]), min(a['bbox'][1], b['bbox'][1]),
                                 max(a['bbox'][2], b['bbox'][2]), max(a['bbox'][3], b['bbox'][3])]
                    a['area'] += b['area']
                    groups.pop(j)
                    continue
                j += 1
            i += 1

    def density(cx, cy):
        y0, y1 = max(0, cy - 12), min(h, cy + 12)
        x0, x1 = max(0, cx - 12), min(w, cx + 12)
        return int(mask[y0:y1, x0:x1].sum())

    diffs = []
    for g in groups:
        members = sorted(g['members'], key=lambda c: c['area'] * c['strength'], reverse=True)
        best, px, py = -1, None, None
        for c in members:
            cx = min(max(round(c['cx']), 40), w - 40)
            cy = min(max(round(c['cy']), 40), h - 40)
            v = density(cx, cy)
            if v > best:
                best, px, py = v, cx, cy
        if px is None:
            px = py = 40
        if any(math.hypot(d['x'] - px, d['y'] - py) < PHOTO_MIN_GAP for d in diffs):
            moved = False
            for c in members:
                qx = min(max(round(c['cx']), 40), w - 40)
                qy = min(max(round(c['cy']), 40), h - 40)
                if not any(math.hypot(d['x'] - qx, d['y'] - qy) < PHOTO_MIN_GAP for d in diffs):
                    px, py, moved = qx, qy, True
                    break
            if not moved:
                continue
        diffs.append(dict(x=px, y=py, area=g['area'], core=best, bbox=g['bbox'],
                          boxes=[[max(0, c['bbox'][0] - PHOTO_PAD), max(0, c['bbox'][1] - PHOTO_PAD),
                                  min(w - 1, c['bbox'][2] + PHOTO_PAD), min(h - 1, c['bbox'][3] + PHOTO_PAD)]
                                 for c in members]))
    diffs.sort(key=lambda d: -d['area'])
    return diffs


def check_level(sc, verbose=True):
    a = os.path.join(IMG_DIR, '%s-A.jpg' % sc['key'])
    b = os.path.join(IMG_DIR, '%s-B.jpg' % sc['key'])
    ds = detect(a, b)
    problems = []
    if len(ds) != sc['want']:
        problems.append('识别到 %d 处差异（应为 %d 处）' % (len(ds), sc['want']))
    for i, d in enumerate(ds, 1):
        if d['area'] < MIN_DIFF_PX:
            problems.append('第 %d 处只有 %d 像素（< %d）' % (i, d['area'], MIN_DIFF_PX))
        if not (WANT_MARGIN <= d['x'] <= 400 - WANT_MARGIN and WANT_MARGIN <= d['y'] <= 300 - WANT_MARGIN):
            problems.append('第 %d 处点位 (%d,%d) 离边太近' % (i, d['x'], d['y']))
        for j in range(i - 1):
            gap = math.hypot(ds[j]['x'] - d['x'], ds[j]['y'] - d['y'])
            if gap < WANT_GAP:
                problems.append('第 %d 处与第 %d 处只隔 %.0fpx（< %d）'
                                % (i, j + 1, gap, WANT_GAP))
    if verbose:
        print('  %-7s %-10s %d 处' % (sc['key'], sc['name'], len(ds)))
        for i, d in enumerate(ds, 1):
            print('      %d) (%3d,%3d) %5d px  框=%s' % (i, d['x'], d['y'], d['area'], d['bbox']))
        for p in problems:
            print('      ✗ ' + p)
    return ds, problems


def write_meta(sc, ds):
    import json
    path = os.path.join(IMG_DIR, '%s.json' % sc['key'])
    data = dict(key=sc['key'], name=sc['name'], theme='公园', k=len(ds),
                sizeA=os.path.getsize(os.path.join(IMG_DIR, '%s-A.jpg' % sc['key'])),
                sizeB=os.path.getsize(os.path.join(IMG_DIR, '%s-B.jpg' % sc['key'])),
                diffs=[dict(x=d['x'], y=d['y'], area=d['area'], core=d['core'],
                            bbox=[int(v) for v in d['bbox']],
                            boxes=[[int(v) for v in b] for b in d['boxes']],
                            desc=sc['diffs'][i] if i < len(sc['diffs']) else '不一样')
                       for i, d in enumerate(ds)])
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--only', help='只做指定关卡，逗号分隔，例如 hard-3,hell-3')
    ap.add_argument('--svg-only', action='store_true', help='只写 SVG，不渲染 JPG')
    ap.add_argument('--no-check', action='store_true')
    args = ap.parse_args()

    os.makedirs(SVG_DIR, exist_ok=True)
    os.makedirs(IMG_DIR, exist_ok=True)
    only = set((args.only or '').split(',')) - {''}
    todo = [sc for sc in SCENES if not only or sc['key'] in only]
    if not todo:
        print('✗ 没有匹配的关卡：%s' % args.only)
        return 2

    bad = 0
    for sc in todo:
        print('· %s %s' % (sc['key'], sc['name']))
        for v in ('A', 'B'):
            svg = write_svg(sc, v)
            if not args.svg_only:
                size = render_jpg(svg, os.path.join(IMG_DIR, '%s-%s.jpg' % (sc['key'], v)),
                                  '%s%s' % (sc['key'], v))
                print('    %s.svg → %s.jpg (%.0f KB)' % (os.path.basename(svg), sc['key'] + '-' + v, size / 1024))
        if not args.svg_only and not args.no_check:
            ds, problems = check_level(sc)
            write_meta(sc, ds)
            bad += 1 if problems else 0
    print('')
    if bad:
        print('✗ %d 关没过自检' % bad)
        return 1
    print('✓ 全部通过自检')
    return 0


if __name__ == '__main__':
    sys.exit(main())
