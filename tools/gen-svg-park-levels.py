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
GAME = (400, 300)          # 游戏逻辑尺寸（检测/显示都会缩到这个尺寸）
SHOT = (GAME[0] * 2, GAME[1] * 2)   # 关卡图渲染分辨率：2 倍，缩到 GAME 显示时更锐利

# ---- 游戏检测参数（与 index.html 一致） ----
PHOTO_DIFF_TH = 110
PHOTO_MIN_AREA = 45
PHOTO_MERGE = 30
PHOTO_MAX_W = 220
PHOTO_MAX_H = 180
PHOTO_MIN_GAP = 34
PHOTO_PAD = 18

# ---- 本次的自定门槛（比游戏更严，留富余量） ----
MIN_DIFF_PX = 110      # 每处差异至少这么多像素（400×300 系）：游戏下限 45，
                       # 之前卡 300 是过度保守 —— 差异又大又均匀，反而比简单模式还好找
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
        out += '<circle cx="%s" cy="%s" r="27" fill="#e5453a"/>' % (ax, ay)
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
    for tx, ty in ((90, 470), (250, 430), (700, 460), (770, 500)):
        o.append(tree(tx, ty, 40, apples=()))
    o.append(bush(200, 505, 0.6) + bush(620, 500, 0.55))
    for fx, fy in ((60, 520), (130, 550), (500, 520), (570, 550), (640, 520), (740, 552)):
        o.append(flower(fx, fy, '#ffd93b' if fx % 2 else '#ff6fa5', 0.85))
    for sx, sy, sr in ((180, 560, 15), (320, 575, 13), (460, 560, 16), (600, 575, 13)):
        o.append(stone(sx, sy, sr, round(sr * 0.6)))
    for tx in (150, 230, 310, 390, 470, 550, 630, 710):
        o.append(tuft(tx, 590))
    o.append(cloud(200, 100) + cloud(620, 90) + birds((300, 180), (360, 205), (420, 182)))
    o.append(gate(400, '#e5533f'))                                             # 门体固定
    o.append(_shrink(flag(400, 250, '#ffd93b' if v == 'A' else '#4aa8e0'), 400, 250, 0.7))  # 门顶小旗颜色
    if v == 'A':
        o.append(_shrink(balloon(150, 140, '#ffd93b'), 150, 140, 0.75))  # 气球数量
    o.append(balloon(660, 380, '#ef5350'))
    if v == 'A':
        o.append(_shrink(cloud(400, 140), 400, 140, 0.75))  # 云朵数量
    o.append(_shrink(sun(650, 140, 34, rays=(46, 66, 14) if v == 'A' else None), 650, 140, 0.75))  # 太阳光芒
    o.append(booth(650, 559, window=(v == 'A')))                              # 售票亭窗户
    fc = '#ff6fa5'                                                # 花丛底色固定
    o.append(flower(120, 470, fc, 1.2) + flower(160, 495, fc, 1.2)
             + flower(190, 465, '#ff6fa5' if v == 'A' else '#b06ff0', 1.2))  # 三朵里有一朵不同色
    if hell and v == 'A':                                                      # 地狱第 7 处
        o.append(butterfly(400, 480, '#8b5cf6', 1.0))
    return o


def duckling(x, y, mirror=False, body='#fffdf5', s=0.75):
    """小鸭子（比 duck 小一半，用于"数一数"或藏在同类里）"""
    f = ' scale(-1,1)' if mirror else ''
    return (f'<g transform="translate({x},{y}) scale({s}){f}">'
            f'<ellipse cx="0" cy="0" rx="26" ry="15" fill="{body}"/>'
            f'<path d="M-20,-2 L-36,-10 L-22,6 Z" fill="#f2ecdc"/>'
            f'<circle cx="18" cy="-16" r="12" fill="{body}"/>'
            f'<path d="M28,-20 L44,-15 L28,-10 Z" fill="#ff9f2e"/>'
            f'<circle cx="21" cy="-18" r="2.4" fill="#3b3b3b"/></g>')


def lily_pad(x, y, r=22, color='#57bf5a'):
    """睡莲叶（小片；数量差藏在几片相似的叶子里）"""
    return f'<ellipse cx="{x}" cy="{y}" rx="{r}" ry="{r * 0.54:.0f}" fill="{color}"/>'


def reed_one(x, base, h=96, color='#4aa83f'):
    """单根芦苇（细，整根消失也只有一两百像素）"""
    return (f'<path d="M{x},{base} q-6,-{h * 0.6:.0f} 2,-{h}" fill="none" stroke="{color}" '
            f'stroke-width="9" stroke-linecap="round"/>'
            f'<ellipse cx="{x + 2}" cy="{base - h - 16}" rx="11" ry="26" fill="#8b6b4a"/>')


def pond_sign(x, base, d, s=1.0):
    """池边木牌：数字是场景的一部分（不是天上飘的大牌子）"""
    return (f'<rect x="{x - 8 * s:.0f}" y="{base - 150 * s:.0f}" width="{16 * s:.0f}" '
            f'height="{150 * s:.0f}" fill="#8b6b4a"/>'
            f'<rect x="{x - 54 * s:.0f}" y="{base - 240 * s:.0f}" width="{108 * s:.0f}" '
            f'height="{88 * s:.0f}" rx="{10 * s:.0f}" fill="#f0dcae" stroke="#c9a06a" '
            f'stroke-width="{5 * s:.0f}"/>'
            + digit(x - 20 * s, base - 226 * s, d, 58 * s, '#5b4632'))


def scene_pond(v, pal, hell):
    """池塘边（改版：差异变小、藏在同类物里；牌子写 3，鸭子在 B 图多一只 —— 数一数）"""
    o = [defs(pal), sky(), hills((200, 425, 250, 85, pal['hill1']),
                                  (640, 435, 280, 95, pal['hill2'])), grass(400)]
    o.append(cloud(160, 90) + cloud(520, 60) + cloud(700, 170))
    o.append(birds((300, 200), (350, 226), (410, 205)))
    o.append(pond(390, 480, 300, 86))
    # 岸边：两棵树 + 两丛灌木 + 一排草丛（同类重复，差异才藏得住）
    o.append(tree(760, 470, 46, apples=()))
    o.append(tree(70, 430, 40, apples=()))
    o.append(bush(300, 430, 0.5) + bush(430, 420, 0.45))
    for tx in (450, 520, 590, 660, 720):
        o.append(tuft(tx, 580))
    # 睡莲叶 5 片（B 少一片小的）
    for px, py, pr in ((520, 470, 26), (612, 506, 30), (468, 524, 22), (600, 452, 20)):
        o.append(lily_pad(px, py, pr))
    if v == 'A':
        o.append(lily_pad(660, 500, 22))                    # ← 少一片
    # 小石子 6 块（B 少一块小的）
    for sx, sy, sr in ((110, 500, 20), (170, 522, 17), (60, 546, 18), (300, 526, 15), (352, 556, 17)):
        o.append(stone(sx, sy, sr, round(sr * 0.6)))
    # 芦苇 6 根（B 少一根）
    for rx in (430, 456, 482, 534, 560, 586):
        o.append(reed_one(rx, 540))
    if v == 'A':
        o.append(reed_one(500, 540))                        # ← 少一根
    # 岸边小花 6 朵（其中一朵颜色不同）
    for fx, fy in ((210, 470), (250, 492), (290, 466), (330, 490), (370, 468)):
        o.append(flower(fx, fy, '#ff6fa5', 1.1))
    o.append(flower(150, 476, '#ff6fa5' if v == 'A' else '#ff8f3c', 1.1))   # ← 颜色不同
    o.append(duckling(430, 470, mirror=True))
    o.append(duckling(520, 500))
    o.append(duckling(600, 466, mirror=True))
    if v == 'A':
        o.append(duckling(300, 430))                        # ← 数一数：A 是 3 只
    # 天上：小云一朵（B 少一朵） + 蜻蜓一只（B 少一只）
    if v == 'A':
        o.append(_shrink(cloud(640, 240, 0.7), 640, 240, 0.75))
    if v == 'A':
        o.append(_shrink(dragonfly(150, 300), 150, 300, 0.75))
    if hell and v == 'A':
        o.append(balloon(400, 110, '#ffd93b'))
    return o


def scene_play(v, pal, hell):
    """儿童乐园"""
    o = _base(pal)
    o.append('<ellipse cx="380" cy="520" rx="260" ry="74" fill="%s"/>' % pal['sand'])
    o.append(cloud(430, 100) + cloud(760, 230) + birds((330, 190), (392, 224)))
    o.append(bush(700, 470, 0.8) + tuft(760, 570) + stone(300, 580, 22, 13))
    o.append(slide(120, 470, '#5aa9e6'))
    o.append(seesaw(680, 480, '#4aa8e0'))
    for fx, fy in ((80, 500), (160, 540), (240, 505), (620, 520), (700, 545), (760, 505)):
        o.append(flower(fx, fy, '#ffd93b' if fx % 2 else '#ff6fa5', 0.8))
    for tx in (200, 280, 360, 440, 520, 600):
        o.append(tuft(tx, 585))
    o.append(cloud(200, 100) + cloud(600, 80) + birds((300, 180), (370, 205)))
    o.append(_shrink(slide(300, 470, '#f2a13c' if v == 'A' else '#5aa9e6'), 300, 470, 0.75))  # 滑梯颜色
    o.append(swing(600, 470, seat=(v == 'A')))                                 # 秋千座板
    o.append(_shrink(seesaw(150, 480, '#8b6b4a' if v == 'A' else '#4aa8e0'), 150, 480, 0.75))  # 跷跷板支架
    if v == 'A':
        o.append(_shrink(ball(650, 500, 34, '#ef5350'), 650, 500, 0.75))  # 皮球
    if v == 'A':
        o.append(_shrink(cloud(150, 120), 150, 120, 0.75))  # 云朵数量
    if v == 'A':
        o.append(_shrink(kite(650, 140, '#8b5cf6', tail=False), 650, 140, 0.75))  # 风筝
    if hell and v == 'A':
        o.append(balloon(400, 140, '#ffd93b'))
    return o


def scene_garden(v, pal, hell):
    """花园花圃"""
    o = _base(pal)
    o.append(cloud(520, 100) + cloud(760, 240) + birds((360, 180), (422, 214)))
    o.append(path_center(skew=0.3))
    o.append(bush(60, 520, 0.9) + tuft(520, 580) + stone(240, 570, 24, 14))
    o.append(arch(180, 380, '#8fd15a'))
    o.append(arch(430, 380, '#f2a13c'))
    for fx, fy in ((90, 500), (170, 470), (250, 505), (330, 470), (420, 505), (500, 470),
                   (590, 505), (680, 470), (760, 505)):
        o.append(flower(fx, fy, '#ff6fa5' if fx % 2 else '#ffb703', 0.9))
    o.append(butterfly(220, 200, '#ffd93b', 0.8) + butterfly(560, 230, '#4aa8e0', 0.8))
    o.append(cloud(150, 90) + cloud(640, 110))
    o.append(_shrink(arch(650, 380, '#8fd15a' if v == 'A' else '#e5533f'), 650, 380, 0.75))  # 花架颜色
    o.append(_shrink(butterfly(400, 140, '#ff6fa5' if v == 'A' else '#7f6ff0', 1.0), 400, 140, 0.75))  # 蝴蝶颜色
    if v == 'A':
        o.append(_shrink(cloud(150, 120), 150, 120, 0.75))  # 云朵数量
    fc = '#ff6fa5'                                                # 花坛底色固定
    o.append(flower(120, 470, fc, 1.2) + flower(160, 495, fc, 1.2)
             + flower(190, 465, '#ff6fa5' if v == 'A' else '#b06ff0', 1.2))  # 三朵里有一朵不同色
    if v == 'A':
        o.append(_shrink(watering(400, 500, '#4aa8e0'), 400, 500, 0.75))  # 喷壶
    if v == 'A':
        o.append(_shrink(bee(150, 320, 1.0), 150, 320, 0.75))  # 蜜蜂
    if hell and v == 'A':
        o.append(balloon(400, 300, '#ffd93b'))
    return o


def scene_picnic(v, pal, hell):
    """树林野餐"""
    o = _base(pal)
    o.append(cloud(400, 220) + birds((430, 200), (492, 234)))
    o.append(tree(760, 470, 50, apples=((730, 410),)))
    o.append(tuft(560, 580) + stone(320, 580, 26, 15) + bush(700, 560, 0.7))
    o.append(tree(120, 470, 44, apples=()))
    o.append(tree(680, 460, 40, apples=()))
    o.append(bush(300, 430, 0.6) + bush(520, 425, 0.55))
    for tx in (240, 320, 400, 480, 560, 640, 720):
        o.append(tuft(tx, 588))
    for fx, fy in ((200, 505), (280, 540), (360, 505), (440, 540), (520, 505), (600, 540)):
        o.append(flower(fx, fy, '#ffd93b' if fx % 2 else '#ff6fa5', 0.8))
    o.append(cloud(220, 90) + cloud(600, 120) + birds((330, 180), (400, 205)))
    apple = ((150, 300),) if v == 'A' else ()                                  # 苹果数量
    o.append(tree(150, 430, 84, apples=((100, 330),) + apple))
    o.append(blanket(150, 500, 110, 60, '#ff8fb1' if v == 'A' else '#6fa8f0'))  # 野餐毯颜色
    if v == 'A':
        o.append(_shrink(basket(400, 480), 400, 480, 0.75))  # 野餐篮
    if v == 'A':
        o.append(_shrink(mushroom(650, 510, '#e5533f'), 650, 510, 0.75))  # 蘑菇数量
    o.append(mushroom(710, 540, '#e5533f'))
    if v == 'A':
        o.append(_shrink(cloud(150, 120), 150, 120, 0.75))  # 云朵数量
    if v == 'A':
        o.append(_shrink(kite(650, 140, '#8b5cf6', tail=False), 650, 140, 0.75))  # 风筝
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
    o.append(reeds(80, 500, 5, step=26))
    o.append(reeds(720, 500, 5, step=26))
    o.append(boat(180, 470, '#ffd93b', s=0.7))
    o.append(bench(620, 480))
    for tx in (60, 140, 260, 340, 460, 540, 660, 740):
        o.append(tuft(tx, 585))
    o.append(cloud(200, 100) + cloud(620, 90))
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
    # 同类诱饵：整排锥筒/成对水泥管/草丛/树/花（两版完全一致，只会更难找，不会产生差异像素）
    for cx in (60, 130, 200, 380, 450, 620, 690, 760):
        o.append(cone(cx, 585))
    for px in (90, 200, 690):
        o.append(pipes(px, 560, 2))
    for tx in (250, 330, 410, 500, 580, 660, 740):
        o.append(tuft(tx, 596))
    for fx, fy in ((80, 500), (150, 530), (230, 505), (320, 535), (420, 505)):
        o.append(flower(fx, fy, '#ff6fa5' if fx % 2 else '#ffd93b', 0.8))
    o.append(cloud(300, 90) + cloud(560, 210) + cloud(240, 250))
    o.append(tree(760, 470, 44, apples=()))
    o.append(cloud(760, 250) + birds((360, 210), (422, 244)))
    o.append(_shrink(machine(500, 500), 500, 500, 0.75))  # 挖斗/铲/筒 颜色是差异
    if hell and v == 'A':
        o.append(_shrink(balloon(400, 140, '#ffd93b'), 400, 140, 0.75))  # 地狱第 7 处
    if v == 'A':
        o.append(_shrink(cloud(150, 130), 150, 130, 0.75))  # 云朵数量
    o.append(_shrink(sun(650, 130, 34, rays=(46, 66, 14) if v == 'A' else None), 650, 130, 0.8))  # 太阳光芒
    if v == 'A':
        o.append(_shrink(pipes(120, 512, 3), 120, 512, 0.75))  # 水泥管
    if v == 'A':
        o.append(_shrink(cone(300, 520), 300, 520, 0.75))  # 锥筒
    o.append(_shrink(windsock(250, 545, '#ff7a1a' if v == 'A' else '#4a86d8'), 250, 545, 0.85))  # 风向袋颜色
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


# ==========================================================================
# 数字教学：七段式数字（不依赖字体，任何机器渲染都一样）
# ==========================================================================
SEG = dict(
    A=((0.12, 0.06), (0.88, 0.06)), B=((0.88, 0.06), (0.88, 0.50)),
    C=((0.88, 0.50), (0.88, 0.94)), D=((0.12, 0.94), (0.88, 0.94)),
    E=((0.12, 0.50), (0.12, 0.94)), F=((0.12, 0.06), (0.12, 0.50)),
    G=((0.12, 0.50), (0.88, 0.50)),
)
DIGIT_SEGS = {
    '0': 'ABCDEF', '1': 'BC', '2': 'ABGED', '3': 'ABGCD', '4': 'FGBC',
    '5': 'AFGCD', '6': 'AFGEDC', '7': 'ABC', '8': 'ABCDEFG', '9': 'ABCDFG',
}


def digit(x, y, d, h=130, ink='#2f3b4a'):
    """七段式数字：(x,y) 是数字框左上角，h 是字高；10 会画成两位"""
    w = h * 0.72
    sw = max(6, round(h * 0.24))
    out = ''
    for ch in str(d):
        for seg in DIGIT_SEGS.get(ch, ''):
            (u1, v1), (u2, v2) = SEG[seg]
            out += ('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f"/>'
                    % (x + u1 * w, y + v1 * h, x + u2 * w, y + v2 * h))
        x += w + h * 0.16
    return ('<g stroke="%s" stroke-width="%d" stroke-linecap="round" fill="none">%s</g>'
            % (ink, sw, out))


def plaque(cx, cy, d, s=0.9, face='#fff6e5', edge='#c9a06a', ink='#2f3b4a'):
    """数字牌（差异就是牌上的数字；两版传不同 digit）"""
    h = 130 * s
    w = h * 0.72
    total = len(str(d)) * w + (len(str(d)) - 1) * h * 0.16
    pw, ph = max(120 * s, total + 56 * s), h + 44 * s
    return ('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" rx="%.1f" fill="%s" stroke="%s" stroke-width="%d"/>'
            % (cx - pw / 2, cy - ph / 2, pw, ph, 16 * s, face, edge, max(3, round(6 * s)))
            + digit(cx - total / 2, cy - h / 2, d, h, ink))


# ==========================================================================
# 通用场景装配：关卡用数据描述（deco 两版一致，diffs 才是差异）
# ==========================================================================
SIGN_STYLE = {
    'wood': ('#f0dcae', '#c9a06a', '#8b6b4a'),
    'zoo': ('#ffe9c6', '#d8a45c', '#a8762a'),
    'sea': ('#e6f6ff', '#7fc6e8', '#4a7f99'),
    'fun': ('#fff2cf', '#ffb347', '#e5533f'),
    'site': ('#ffe9a8', '#e8b53c', '#8b6b4a'),
}


def sign_board(cx, cy, d, s=0.9, style='wood'):
    # 把数字做成场景里的牌子：立柱 + 牌面（按主题配色），不再是天上飘的卡片
    face, edge, post = SIGN_STYLE.get(style, SIGN_STYLE['wood'])
    h = 130 * s
    w = h * 0.72
    total = len(str(d)) * w + (len(str(d)) - 1) * h * 0.16
    pw, ph = max(120 * s, total + 56 * s), h + 44 * s
    board = (f'<rect x="{cx - pw / 2:.1f}" y="{cy - ph / 2:.1f}" width="{pw:.1f}" height="{ph:.1f}" '
             f'rx="{16 * s:.0f}" fill="{face}" stroke="{edge}" stroke-width="{max(3, round(6 * s))}"/>'
             + digit(cx - total / 2, cy - h / 2, d, h, '#3b3a36'))
    if style == 'sea':                      # 水下：木牌柱子一直插到沙底才自然
        return (f'<rect x="{cx - 7 * s:.0f}" y="{cy + ph / 2 - 6 * s:.0f}" width="{14 * s:.0f}" '
                f'height="{560 - cy - ph / 2:.0f}" fill="{post}"/>' + board)
    if cy < 420:                            # 高处：像横幅一样用两根绳子吊下来
        top = cy - ph / 2
        return (f'<path d="M{cx - pw / 3:.0f},{top:.0f} L{cx - pw / 3:.0f},{top - 70 * s:.0f} '
                f'M{cx + pw / 3:.0f},{top:.0f} L{cx + pw / 3:.0f},{top - 70 * s:.0f}" '
                f'stroke="{post}" stroke-width="{8 * s:.0f}"/>' + board)
    return (f'<rect x="{cx - 9 * s:.0f}" y="{cy + ph / 2 - 6 * s:.0f}" width="{18 * s:.0f}" '
            f'height="{70 * s:.0f}" fill="{post}"/>' + board)


DIFF_SCALE = {'zoo': 0.8, 'sea': 0.7, 'fun': 0.7, 'site': 0.85}


def _shrink(body, ax, ay, k):
    if k >= 0.999:
        return body
    return (f'<g transform="translate({ax},{ay}) scale({k}) translate({-ax},{-ay})">{body}</g>')


def build_spec(v, pal, hell, spec):
    o = [defs(pal)]
    o.extend(THEME_BG[spec['theme']](pal, v, hell))
    for name, args in spec.get('deco', []):
        o.append(KIT[name](*args))
    if spec.get('puzzle_deco'):                     # 数一数：先把同类物摆好
        for name, args in spec['puzzle_deco']:
            o.append(KIT[name](*args))
    if spec.get('puzzle_sign'):                     # 牌子写着"应该有几个"
        px, py, pd, ps = spec['puzzle_sign']
        o.append(sign_board(px, py, pd, ps, spec.get('theme', 'wood')))
    k = DIFF_SCALE.get(spec['theme'], 1.0)
    for item in spec['diffs']:
        if item[0] == 'show':                       # 只在 A 出现（有无差异）
            if v == 'A':
                a = item[2]
                o.append(_shrink(KIT[item[1]](*a), a[0], a[1], k))
        elif item[0] == 'swap':                     # 两版都画，换参数（颜色/大小/光芒）
            a = item[2] if v == 'A' else item[3]
            o.append(_shrink(KIT[item[1]](*a), a[0], a[1], k))
        elif item[0] == 'digit':                    # 数字：牌子上的数字变了
            _, cx, cy, da, db, s = item
            o.append(sign_board(cx, cy, da if v == 'A' else db, s, spec.get('theme', 'wood')))
    if hell and v == 'A':                           # 地狱第 7 处
        a = spec['extra'][1]
        o.append(_shrink(KIT[spec['extra'][0]](*a), a[0], a[1], k))
    return o


def spec_builder(spec):
    return lambda v, pal, hell: build_spec(v, pal, hell, spec)


# ==========================================================================
# 动物园
# ==========================================================================
def zoo_bg(pal, v, hell):
    # 动物园背景：铺一圈同类小物（花/石/草丛/灌木），差异就藏在这些相似物里
    o = [sky(), hills((180, 420, 260, 90, pal['hill1']), (620, 430, 300, 100, pal['hill2'])), grass(400)]
    for fx, fy in ((60, 470), (110, 500), (160, 468), (215, 496), (270, 470), (325, 500),
                   (430, 476), (490, 505), (545, 472), (600, 500), (655, 474), (710, 502), (760, 476)):
        o.append(flower(fx, fy, '#ff6fa5' if fx % 2 else '#ffd93b', 0.75))
    for sx, sy, sr in ((90, 545, 15), (200, 560, 13), (300, 542, 16), (400, 562, 12),
                       (500, 545, 15), (600, 560, 13), (700, 543, 16)):
        o.append(stone(sx, sy, sr, round(sr * 0.6)))
    for tx in (140, 240, 340, 440, 540, 640, 740):
        o.append(tuft(tx, 585))
    o.append(bush(400, 430, 0.55) + bush(700, 424, 0.5))
    return o


def pen(x, base, w=320, color='#d8b382', rail='#c9a06a'):
    """笼舍围栏"""
    out = ''
    n = max(3, int(w / 76))
    for i in range(n + 1):
        out += ('<rect x="%.0f" y="%.0f" width="16" height="88" rx="6" fill="%s"/>'
                % (x - w / 2 + i * w / n, base - 88, color))
    out += '<rect x="%.0f" y="%.0f" width="%.0f" height="14" rx="6" fill="%s"/>' % (x - w / 2 - 8, base - 66, w + 16, rail)
    out += '<rect x="%.0f" y="%.0f" width="%.0f" height="14" rx="6" fill="%s"/>' % (x - w / 2 - 8, base - 30, w + 16, rail)
    return out


def elephant(x, y, s=1.0, body='#b9b4c4'):
    return ('<g transform="translate(%(x)s,%(y)s) scale(%(s)s)">'
            '<ellipse cx="0" cy="-72" rx="118" ry="70" fill="%(c)s"/>'
            '<circle cx="-116" cy="-100" r="50" fill="%(c)s"/>'
            '<ellipse cx="-146" cy="-104" rx="26" ry="34" fill="%(c)s"/>'
            '<path d="M-146,-74 q-26,52 -8,84" fill="none" stroke="%(c)s" stroke-width="26" stroke-linecap="round"/>'
            '<circle cx="-132" cy="-112" r="6" fill="#3b3b3b"/>'
            '<rect x="-74" y="-26" width="28" height="30" rx="9" fill="%(c)s"/>'
            '<rect x="6" y="-26" width="28" height="30" rx="9" fill="%(c)s"/>'
            '<rect x="58" y="-26" width="28" height="30" rx="9" fill="%(c)s"/>'
            '</g>' % dict(x=x, y=y, s=s, c=body))


def giraffe(x, y, s=1.0, body='#e8b53c'):
    return ('<g transform="translate(%(x)s,%(y)s) scale(%(s)s)">'
            '<ellipse cx="0" cy="-88" rx="62" ry="46" fill="%(c)s"/>'
            '<rect x="-96" y="-286" width="34" height="204" rx="14" fill="%(c)s" transform="rotate(8 -79 -184)"/>'
            '<ellipse cx="-108" cy="-300" rx="44" ry="34" fill="%(c)s"/>'
            '<circle cx="-124" cy="-310" r="6" fill="#3b3b3b"/>'
            '<circle cx="-96" cy="-330" r="7" fill="%(c)s"/><circle cx="-134" cy="-330" r="7" fill="%(c)s"/>'
            '<rect x="-40" y="-30" width="20" height="34" rx="8" fill="%(c)s"/>'
            '<rect x="8" y="-30" width="20" height="34" rx="8" fill="%(c)s"/>'
            '<circle cx="-22" cy="-104" r="9" fill="#a8762a"/><circle cx="18" cy="-72" r="9" fill="#a8762a"/>'
            '</g>' % dict(x=x, y=y, s=s, c=body))


def monkey(x, y, s=1.0, fur='#b07a4a'):
    return ('<g transform="translate(%(x)s,%(y)s) scale(%(s)s)">'
            '<ellipse cx="0" cy="-34" rx="42" ry="34" fill="%(c)s"/>'
            '<circle cx="0" cy="-80" r="30" fill="%(c)s"/>'
            '<circle cx="-30" cy="-84" r="13" fill="%(c)s"/><circle cx="30" cy="-84" r="13" fill="%(c)s"/>'
            '<ellipse cx="0" cy="-74" rx="18" ry="15" fill="#f0d3b4"/>'
            '<circle cx="-9" cy="-84" r="4" fill="#3b3b3b"/><circle cx="9" cy="-84" r="4" fill="#3b3b3b"/>'
            '<path d="M34,-40 q40,10 26,-40" fill="none" stroke="%(c)s" stroke-width="14" stroke-linecap="round"/>'
            '</g>' % dict(x=x, y=y, s=s, c=fur))


def rabbit(x, y, s=1.0, fur='#f2efe6'):
    return ('<g transform="translate(%(x)s,%(y)s) scale(%(s)s)">'
            '<ellipse cx="0" cy="-26" rx="38" ry="28" fill="%(c)s"/>'
            '<circle cx="-26" cy="-56" r="24" fill="%(c)s"/>'
            '<ellipse cx="-34" cy="-92" rx="9" ry="26" fill="%(c)s" transform="rotate(-12 -34 -92)"/>'
            '<ellipse cx="-16" cy="-94" rx="9" ry="26" fill="%(c)s" transform="rotate(8 -16 -94)"/>'
            '<circle cx="-34" cy="-58" r="4" fill="#3b3b3b"/>'
            '<circle cx="26" cy="-34" r="14" fill="#ffffff"/>'
            '</g>' % dict(x=x, y=y, s=s, c=fur))


def penguin(x, y, s=1.0, body='#3f4a5a'):
    return ('<g transform="translate(%(x)s,%(y)s) scale(%(s)s)">'
            '<ellipse cx="0" cy="-56" rx="42" ry="56" fill="%(c)s"/>'
            '<ellipse cx="0" cy="-46" rx="26" ry="38" fill="#ffffff"/>'
            '<circle cx="0" cy="-104" r="28" fill="%(c)s"/>'
            '<circle cx="-10" cy="-110" r="4" fill="#ffffff"/><circle cx="10" cy="-110" r="4" fill="#ffffff"/>'
            '<path d="M0,-100 l16,8 l-16,8 Z" fill="#ff9f2e"/>'
            '<ellipse cx="-44" cy="-40" rx="14" ry="30" fill="%(c)s"/>'
            '<ellipse cx="44" cy="-40" rx="14" ry="30" fill="%(c)s"/>'
            '</g>' % dict(x=x, y=y, s=s, c=body))


def panda(x, y, s=1.0):
    return ('<g transform="translate(%(x)s,%(y)s) scale(%(s)s)">'
            '<ellipse cx="0" cy="-40" rx="52" ry="42" fill="#fdfcf8"/>'
            '<circle cx="-34" cy="-40" r="15" fill="#3b3b3b"/><circle cx="30" cy="-58" r="13" fill="#3b3b3b"/>'
            '<circle cx="0" cy="-92" r="38" fill="#fdfcf8"/>'
            '<circle cx="-34" cy="-124" r="16" fill="#3b3b3b"/><circle cx="34" cy="-124" r="16" fill="#3b3b3b"/>'
            '<ellipse cx="-15" cy="-96" rx="11" ry="13" fill="#3b3b3b" transform="rotate(-18 -15 -96)"/>'
            '<ellipse cx="15" cy="-96" rx="11" ry="13" fill="#3b3b3b" transform="rotate(18 15 -96)"/>'
            '<ellipse cx="0" cy="-84" rx="12" ry="9" fill="#3b3b3b"/>'
            '</g>' % dict(x=x, y=y, s=s))


def bamboo(x, base, h=250, color='#4fb84f'):
    out = ''
    for i, dx in enumerate((-16, 0, 16)):
        out += ('<rect x="%s" y="%s" width="13" height="%s" rx="6" fill="%s"/>'
                % (x + dx, base - h + i * 26, h - i * 26, color))
        out += ('<ellipse cx="%s" cy="%s" rx="34" ry="11" fill="%s" transform="rotate(-20 %s %s)"/>'
                % (x + dx - 26, base - h + 40 + i * 60, color, x + dx - 26, base - h + 40 + i * 60))
    return out


def bowl(x, y, color='#4aa8e0'):
    """喂食盆"""
    return ('<path d="M%s,%s q62,52 124,0 Z" fill="%s"/>'
            '<ellipse cx="%s" cy="%s" rx="62" ry="16" fill="#ffffff" opacity="0.25"/>'
            % (x - 62, y - 34, color, x, y - 38))


def zoo_levels():
    gate = lambda: [('gate', (400, '#e5533f')), ('path_center', ())]
    spec = []
    # hard-13 动物园大门
    spec.append(dict(key='hard-13', name='动物园大门', theme='zoo',
        deco=[('gate', (400, '#e5533f')), ('path_center', ()),
              ('tree', (90, 470, 52, ((60, 430),))), ('bush', (740, 520, 0.9)),
              ('tuft', (300, 570)), ('stone', (700, 560, 24, 14))],
        diffs=[('show', 'cloud', (150, 130)),
               ('show', 'balloon', (400, 130, '#ffd93b')),
               ('swap', 'sun', (650, 130, 34, (46, 66, 14)), (650, 130, 34, None)),
               ('show', 'rabbit', (150, 505, 1.0)),
               ('show', 'bowl', (370, 505)),
               ('swap', 'flower', (600, 480, '#ff6fa5', 1.8), (600, 480, '#b06ff0', 1.8))],
        extra=('bee', (400, 300, 1.0))))
    # hard-14 大象馆
    spec.append(dict(key='hard-14', name='大象馆', theme='zoo',
        puzzle_deco=[('rabbit', (620, 500, 1.0)), ('rabbit', (700, 545, 0.9))],
        deco=[('pen', (560, 500, 300)), ('elephant', (560, 500, 1.0)),
              ('tree', (110, 470, 56, ((80, 425), (140, 450)))), ('pond', (760, 520, 90, 40)),
              ('tuft', (330, 580)), ('stone', (200, 570, 22, 13))],
        diffs=[('show', 'cloud', (150, 130)),
               ('swap', 'sun', (650, 130, 34, (46, 66, 14)), (650, 130, 34, None)),
               ('show', 'balloon', (400, 110, '#ef5350')),
               ('show', 'rabbit', (110, 500, 1.0)),
               ('show', 'monkey', (370, 505, 0.7)),
               ('swap', 'ball', (600, 400, 34, '#ffd93b'), (600, 400, 34, '#4aa8e0'))],
        extra=('bee', (400, 300, 1.0))))
    # hard-15 长颈鹿
    spec.append(dict(key='hard-15', name='长颈鹿', theme='zoo',
        deco=[('giraffe', (600, 500, 1.0)), ('tree', (110, 470, 60, ((85, 430), (135, 445)))),
              ('pen', (600, 500, 280)), ('bush', (300, 480, 0.9)), ('tuft', (430, 580))],
        diffs=[('show', 'cloud', (150, 130)),
               ('swap', 'sun', (650, 130, 30), (650, 130, 44)),
               ('show', 'kite', (400, 110, '#8b5cf6', False)),
               ('show', 'rabbit', (150, 505, 0.7)),
               ('show', 'penguin', (370, 510, 1.0)),
               ('swap', 'tree', (760, 470, 46, ((730, 410), (770, 440))), (760, 470, 46, ((730, 410),)))],
        extra=('bee', (400, 300, 1.0))))
    # hard-16 猴山
    spec.append(dict(key='hard-16', name='猴山', theme='zoo',
        puzzle_deco=[('rabbit', (620, 500, 1.0)), ('rabbit', (700, 545, 0.9))],
        deco=[('hillrock', (600, 500)), ('monkey', (600, 470, 1.1)), ('tree', (110, 470, 54, ())),
              ('bush', (300, 500, 0.9)), ('tuft', (180, 580)), ('stone', (740, 560, 24, 14))],
        diffs=[('show', 'cloud', (150, 130)),
               ('swap', 'sun', (650, 130, 34, (46, 66, 14)), (650, 130, 34, None)),
               ('show', 'balloon', (400, 110, '#ffd93b')),
               ('show', 'rabbit', (110, 500, 1.0)),
               ('show', 'banana', (370, 500)),
               ('swap', 'ball', (600, 470, 34, '#ffd93b'), (600, 470, 34, '#4aa8e0'))],
        extra=('bee', (400, 300, 1.0))))
    # hard-17 熊猫馆
    spec.append(dict(key='hard-17', name='熊猫馆', theme='zoo',
        deco=[('bamboo', (680, 500, 250)), ('panda', (560, 500, 1.1)), ('tree', (110, 470, 52, ())),
              ('bowl', (330, 500)), ('tuft', (430, 570)), ('stone', (200, 580, 22, 13))],
        diffs=[('show', 'cloud', (150, 130)),
               ('swap', 'sun', (650, 130, 34, (46, 66, 14)), (650, 130, 34, None)),
               ('show', 'balloon', (400, 110, '#ef5350')),
               ('show', 'rabbit', (150, 500, 0.7)),
               ('show', 'panda', (370, 505, 0.7)),
               ('swap', 'bamboo', (680, 500, 250, '#4fb84f'), (680, 500, 250, '#c9d94a'))],
        extra=('bee', (400, 300, 1.0))))
    # hard-18 百鸟园
    spec.append(dict(key='hard-18', name='百鸟园', theme='zoo',
        deco=[('tree', (110, 470, 58, ())), ('tree', (760, 470, 50, ())), ('bush', (600, 490, 0.9)),
              ('birds', ((300, 200), (362, 234))), ('tuft', (200, 580)), ('stone', (430, 570, 22, 13))],
        diffs=[('show', 'cloud', (150, 130)),
               ('swap', 'sun', (650, 130, 34, (46, 66, 14)), (650, 130, 34, None)),
               ('show', 'balloon', (400, 110, '#ef5350')),
               ('show', 'rabbit', (150, 505, 0.7)),
               ('show', 'penguin', (370, 510, 0.9)),
               ('swap', 'flower', (620, 480, '#ff9f4a', 1.8), (620, 480, '#ff6fa5', 1.8))],
        extra=('bee', (400, 300, 1.0))))
    return spec


def hillrock(x, base, color='#b9b4c4'):
    """猴山的石头山"""
    return ('<ellipse cx="%s" cy="%s" rx="150" ry="86" fill="%s"/>'
            '<ellipse cx="%s" cy="%s" rx="80" ry="46" fill="#a8a2b2"/>'
            % (x, base - 40, color, x - 40, base - 90))


def banana(x, y):
    """一串香蕉"""
    return ('<path d="M%s,%s q60,10 84,46 q-46,10 -92,-14 Z" fill="#ffd93b"/>'
            '<path d="M%s,%s q10,26 34,38" fill="none" stroke="#c9a06a" stroke-width="8"/>'
            % (x - 60, y - 40, x + 40, y - 40))


# ==========================================================================
# 海底世界
# ==========================================================================
def sea_bg(pal, v, hell):
    top = _blend(pal['water1'], pal['sky_top'], 0.25)
    mid = _blend(pal['water2'], pal['sky_bottom'], 0.18)
    deep = _blend(pal['water2'], '#062033', 0.55)
    out = ('<defs><linearGradient id="sea" x1="0" y1="0" x2="0" y2="1">'
           '<stop offset="0" stop-color="%s"/><stop offset="0.5" stop-color="%s"/>'
           '<stop offset="1" stop-color="%s"/></linearGradient></defs>'
           '<rect x="0" y="0" width="800" height="600" fill="url(#sea)"/>'
           % (top, mid, deep))
    # 同类诱饵：散落的海草/贝壳/气泡（差异藏在其中）
    for wx in (60, 150, 240, 330, 420, 510, 600, 690):
        out += seaweed(wx, 596, '#3f9e46', 0.55)
    for sx, sy in ((100, 556), (210, 574), (320, 552), (430, 578), (540, 556), (650, 576), (750, 554)):
        out += shell(sx, sy, '#e8cbb0' if sx % 3 else '#d9b48f', 0.5)
    for bx, by in ((170, 300), (480, 240), (700, 380), (300, 460)):
        out += bubbles(bx, by, 3, 0.6)
    # 水面透下来的光柱
    for i, x in enumerate((120, 380, 640)):
        out += ('<path d="M%s,0 L%s,0 L%s,420 L%s,420 Z" fill="#ffffff" opacity="0.10"/>'
                % (x - 40, x + 40, x + 150, x - 10))
    # 沙底
    out += ('<path d="M0,520 q200,-46 400,-6 q200,40 400,-14 L800,600 L0,600 Z" fill="#e8d5a8"/>'
            '<path d="M0,552 q220,-34 420,-2 q190,30 380,-12 L800,600 L0,600 Z" fill="#dcc596"/>')
    return [out]


def sand_patch(x, y, rx=90, ry=26, color='#efe0b8'):
    return '<ellipse cx="%s" cy="%s" rx="%s" ry="%s" fill="%s"/>' % (x, y, rx, ry, color)


def bubbles(x, y, n=5, s=1.0, color='#ffffff'):
    out = ''
    for i in range(n):
        r = (10 + (i % 3) * 6) * s
        out += ('<circle cx="%.0f" cy="%.0f" r="%.0f" fill="%s" opacity="0.35"/>'
                % (x + (i % 2) * 26 * s - 14 * s, y - i * 34 * s, r, color))
    return out


def fish(x, y, color, s=1.0, mirror=False):
    f = ' scale(-1,1)' if mirror else ''
    return ('<g transform="translate(%s,%s) scale(%s)%s">'
            '<ellipse cx="0" cy="0" rx="46" ry="30" fill="%s"/>'
            '<path d="M40,0 L82,-26 L82,26 Z" fill="%s"/>'
            '<circle cx="-24" cy="-6" r="7" fill="#ffffff"/><circle cx="-26" cy="-6" r="3.4" fill="#20303c"/>'
            '<path d="M-6,-28 q14,-20 30,-6" fill="%s"/></g>'
            % (x, y, s, f, color, color, color))


def clownfish(x, y, s=1.0, mirror=False):
    f = ' scale(-1,1)' if mirror else ''
    return ('<g transform="translate(%s,%s) scale(%s)%s">'
            '<ellipse cx="0" cy="0" rx="44" ry="28" fill="#ff8f3c"/>'
            '<path d="M-14,-24 q6,24 0,48" stroke="#ffffff" stroke-width="10" fill="none"/>'
            '<path d="M14,-22 q6,22 0,44" stroke="#ffffff" stroke-width="10" fill="none"/>'
            '<path d="M38,0 L78,-24 L78,24 Z" fill="#ff8f3c"/>'
            '<circle cx="-24" cy="-6" r="7" fill="#ffffff"/><circle cx="-26" cy="-6" r="3.4" fill="#20303c"/>'
            '</g>' % (x, y, s, f))


def butterfly_fish(x, y, color, s=1.0):
    return ('<g transform="translate(%s,%s) scale(%s)">'
            '<ellipse cx="0" cy="0" rx="42" ry="42" fill="%s"/>'
            '<path d="M36,0 L74,-20 L74,20 Z" fill="%s"/>'
            '<path d="M-30,-30 q30,30 0,60" stroke="#ffffff" stroke-width="8" fill="none"/>'
            '<circle cx="-22" cy="-8" r="7" fill="#ffffff"/><circle cx="-24" cy="-8" r="3.4" fill="#20303c"/>'
            '</g>' % (x, y, s, color, color))


def octopus(x, y, color, s=1.0):
    return ('<g transform="translate(%s,%s) scale(%s)">'
            '<ellipse cx="0" cy="-30" rx="54" ry="48" fill="%s"/>'
            '<circle cx="-18" cy="-40" r="8" fill="#ffffff"/><circle cx="18" cy="-40" r="8" fill="#ffffff"/>'
            '<circle cx="-18" cy="-40" r="4" fill="#20303c"/><circle cx="18" cy="-40" r="4" fill="#20303c"/>'
            '<path d="M-40,6 q-14,30 -30,42 M-14,10 q-6,34 -14,48 M14,10 q6,34 16,46 M40,6 q16,28 30,40" '
            'stroke="%s" stroke-width="14" fill="none" stroke-linecap="round"/></g>' % (x, y, s, color, color))


def turtle(x, y, color, s=1.0):
    return ('<g transform="translate(%s,%s) scale(%s)">'
            '<ellipse cx="0" cy="0" rx="66" ry="48" fill="%s"/>'
            '<path d="M-40,-18 q40,-18 80,0 q-40,14 -80,0 Z" fill="#ffffff" opacity="0.3"/>'
            '<circle cx="-72" cy="6" r="20" fill="%s"/>'
            '<path d="M-88,16 q-16,12 -30,8" stroke="%s" stroke-width="10" fill="none" stroke-linecap="round"/>'
            '<ellipse cx="-60" cy="-34" rx="22" ry="11" fill="%s" transform="rotate(-24 -60 -34)"/>'
            '<ellipse cx="50" cy="36" rx="20" ry="10" fill="%s" transform="rotate(24 50 36)"/>'
            '<circle cx="-80" cy="2" r="4" fill="#20303c"/></g>'
            % (x, y, s, color, color, color, color, color))


def jellyfish(x, y, color, s=1.0):
    return ('<g transform="translate(%s,%s) scale(%s)">'
            '<path d="M-52,10 q52,-72 104,0 Z" fill="%s"/>'
            '<path d="M-34,12 q-8,36 -16,52 M-8,14 q-4,40 -8,56 M18,14 q4,40 10,54 M40,10 q10,32 20,48" '
            'stroke="%s" stroke-width="10" fill="none" stroke-linecap="round"/>'
            '<circle cx="-18" cy="-14" r="6" fill="#ffffff" opacity="0.5"/>'
            '<circle cx="16" cy="-20" r="5" fill="#ffffff" opacity="0.5"/></g>' % (x, y, s, color, color))


def starfish(x, y, color, s=1.0):
    return ('<g transform="translate(%s,%s) scale(%s)">'
            '<path d="M0,-52 L16,-16 L54,-14 L24,10 L34,48 L0,26 L-34,48 L-24,10 L-54,-14 L-16,-16 Z" fill="%s"/>'
            '<circle cx="0" cy="0" r="9" fill="#ffffff" opacity="0.55"/></g>' % (x, y, s, color))


def crab(x, y, color, s=1.0):
    return ('<g transform="translate(%s,%s) scale(%s)">'
            '<ellipse cx="0" cy="0" rx="52" ry="38" fill="%s"/>'
            '<circle cx="-20" cy="-24" r="7" fill="#20303c"/><circle cx="20" cy="-24" r="7" fill="#20303c"/>'
            '<path d="M-48,-8 q-26,-16 -34,-34 M48,-8 q26,-16 34,-34" stroke="%s" stroke-width="12" fill="none" '
            'stroke-linecap="round"/>'
            '<path d="M-40,26 l-22,20 M-14,34 l-10,24 M14,34 l10,24 M40,26 l22,20" stroke="%s" '
            'stroke-width="10" fill="none" stroke-linecap="round"/></g>' % (x, y, s, color, color, color))


def seahorse(x, y, color, s=1.0):
    return ('<g transform="translate(%s,%s) scale(%s)">'
            '<path d="M0,-60 q34,10 22,44 q-8,24 -22,30 q22,6 18,30 q-22,-8 -34,-26 q-18,-26 0,-48 '
            'q10,-14 16,-30 Z" fill="%s"/>'
            '<circle cx="8" cy="-52" r="5" fill="#20303c"/>'
            '<path d="M-6,-66 q6,-14 18,-16" stroke="%s" stroke-width="7" fill="none" stroke-linecap="round"/>'
            '</g>' % (x, y, s, color, color))


def coral(x, base, color, s=1.0):
    return ('<g transform="translate(%s,%s) scale(%s)">'
            '<path d="M-34,0 q-6,-46 6,-72 q14,22 4,72 Z" fill="%s"/>'
            '<path d="M-2,0 q-4,-56 12,-88 q18,26 6,88 Z" fill="%s"/>'
            '<path d="M30,0 q0,-40 16,-62 q16,22 6,62 Z" fill="%s"/></g>'
            % (x, base, s, color, color, color))


def seaweed(x, base, color='#4fb84f', s=1.0):
    out = ''
    for i, dx in enumerate((-26, 0, 26)):
        out += ('<path d="M%s,%s q%g,-60 %g,-112 q%g,52 %g,112 Z" fill="%s"/>'
                % (x + dx, base, 16 - i * 6, 8, -14 + i * 8, 10, color))
    return out


def shell(x, y, color, s=1.0):
    return ('<g transform="translate(%s,%s) scale(%s)">'
            '<path d="M-40,0 a40,34 0 0 1 80,0 Z" fill="%s"/>'
            '<path d="M-40,0 a40,34 0 0 1 80,0" fill="none" stroke="#ffffff" stroke-width="4" opacity="0.6"/>'
            '<path d="M-20,0 q20,-34 40,0" fill="none" stroke="#ffffff" stroke-width="3" opacity="0.6"/>'
            '</g>' % (x, y, s, color))


def treasure(x, base):
    """宝箱（有无是差异）"""
    return ('<rect x="%s" y="%s" width="150" height="86" rx="10" fill="#a8762a"/>'
            '<path d="M%s,%s a75,44 0 0 1 150,0 Z" fill="#c9a06a"/>'
            '<rect x="%s" y="%s" width="150" height="14" fill="#8b5a2b"/>'
            '<rect x="%s" y="%s" width="26" height="30" rx="6" fill="#ffd93b"/>'
            % (x - 75, base - 86, x - 75, base - 86, x - 75, base - 46, x - 13, base - 60))


def anchor(x, y, color='#8b949e', s=1.0):
    return ('<g transform="translate(%s,%s) scale(%s)">'
            '<circle cx="0" cy="-70" r="15" fill="none" stroke="%s" stroke-width="11"/>'
            '<rect x="-7" y="-58" width="14" height="92" fill="%s"/>'
            '<rect x="-34" y="-42" width="68" height="12" rx="6" fill="%s"/>'
            '<path d="M-46,10 q0,42 46,42 q46,0 46,-42" fill="none" stroke="%s" stroke-width="13" '
            'stroke-linecap="round"/></g>' % (x, y, s, color, color, color, color))


def submarine(x, y, color, d=None, s=1.0):
    """潜水艇；舷窗里的数字是差异（d=None 就不写数字）"""
    out = ('<g transform="translate(%s,%s) scale(%s)">'
           '<ellipse cx="0" cy="0" rx="150" ry="62" fill="%s"/>'
           '<path d="M-20,-62 L10,-110 L40,-62 Z" fill="%s"/>'
           '<rect x="118" y="-16" width="52" height="32" rx="10" fill="%s"/>'
           '<path d="M-150,-10 L-196,-38 L-196,18 L-150,10 Z" fill="%s"/>'
           '<circle cx="-70" cy="4" r="26" fill="#cdeafc" stroke="#ffffff" stroke-width="6"/>'
           '<circle cx="0" cy="4" r="26" fill="#cdeafc" stroke="#ffffff" stroke-width="6"/>'
           '<circle cx="70" cy="4" r="26" fill="#cdeafc" stroke="#ffffff" stroke-width="6"/>'
           '</g>' % (x, y, s, color, color, color, color))
    out += ('<circle cx="%s" cy="%s" r="30" fill="#cdeafc" stroke="#ffffff" stroke-width="6"/>' % (x, y + 4 * s))
    if d is not None:
        out += digit(x - 22 * s, y - 20 * s, d, 42 * s, '#1f4f6b')
    return out


def bubble_num(cx, cy, d, s=0.9):
    """数字泡泡：泡泡本身两版一致，只有里面的数字是差异"""
    h = 92 * s
    w = h * 0.72
    total = len(str(d)) * w + (len(str(d)) - 1) * h * 0.16
    return ('<circle cx="%s" cy="%s" r="%s" fill="#dff4ff" opacity="0.92" stroke="#ffffff" stroke-width="5"/>'
            % (cx, cy, max(78 * s, total * 0.62 + 40 * s))
            + digit(cx - total / 2, cy - h / 2, d, h, '#1f5f86'))


def dolphin(x, y, color, s=1.0, mirror=False):
    f = ' scale(-1,1)' if mirror else ''
    return ('<g transform="translate(%s,%s) scale(%s)%s">'
            '<path d="M-90,10 q60,-70 150,-30 q-40,74 -150,30 Z" fill="%s"/>'
            '<path d="M20,-56 q18,-30 40,-14 q-14,20 -30,20 Z" fill="%s"/>'
            '<path d="M-90,10 q-30,-6 -46,-28 q34,4 50,20 Z" fill="%s"/>'
            '<circle cx="-34" cy="-12" r="6" fill="#20303c"/></g>' % (x, y, s, f, color, color, color))


def sea_levels():
    spec = []
    # hard-19 珊瑚礁
    spec.append(dict(key='hard-19', name='珊瑚礁', theme='sea',
        deco=[('coral', (620, 560, '#ff6fa5')), ('seaweed', (740, 580, '#4fb84f')),
              ('sand_patch', (200, 570)), ('shell', (100, 560, '#f2c9a0')), ('bubbles', (700, 250, 4))],
        diffs=[('show', 'fish', (150, 130, '#ffd93b', 1.0)),
               ('show', 'fish', (400, 140, '#ffd93b', 0.8)),
               ('show', 'jellyfish', (650, 130, '#b06ff0', 1.0)),
               ('swap', 'starfish', (110, 505, '#ff8f3c', 0.8), (110, 505, '#ff6fa5', 0.8)),
               ('show', 'turtle', (400, 505, '#ff8f3c', 0.62)),
               ('swap', 'coral', (620, 560, '#ff6fa5'), (620, 560, '#ffd93b'))],
        extra=('fish', (400, 300, '#ff6fa5', 0.7))))
    # hard-20 沉船宝藏
    spec.append(dict(key='hard-20', name='沉船宝藏', theme='sea',
        deco=[('treasure', (620, 560)), ('seaweed', (120, 580, '#4fb84f')), ('anchor', (740, 500)),
              ('sand_patch', (300, 575)), ('bubbles', (200, 300, 5))],
        diffs=[('show', 'fish', (150, 130, '#ff6fa5', 1.0)),
               ('show', 'clownfish', (400, 130, 1.0)),
               ('show', 'jellyfish', (650, 130, '#ffd93b', 1.0)),
               ('show', 'shell', (128, 505, '#8b5cf6', 0.9)),
               ('swap', 'octopus', (370, 505, '#b06ff0'), (370, 505, '#ff8f3c')),
               ('swap', 'seaweed', (620, 560, '#4fb84f'), (620, 560, '#ff6fa5'))],
        extra=('fish', (400, 300, '#ffd93b', 0.7))))
    # hard-21 潜水艇
    spec.append(dict(key='hard-21', name='潜水艇', theme='sea',
        deco=[('submarine', (420, 330, '#e8b53c')), ('coral', (120, 570, '#ff8f3c')),
              ('seaweed', (740, 580, '#4fb84f')), ('bubbles', (640, 200, 4)), ('sand_patch', (400, 580))],
        diffs=[('show', 'fish', (150, 120, '#ffd93b', 0.9)),
               ('show', 'jellyfish', (650, 120, '#ff6fa5', 0.9)),
               ('show', 'bubble_num', (400, 140, '4', 1.0)),
               ('swap', 'starfish', (150, 505, '#b06ff0', 0.8), (150, 505, '#7fce8a', 0.8)),
               ('show', 'crab', (370, 505, '#ff8f3c', 1.0)),
               ('show', 'shell', (600, 545, '#8b5cf6', 1.2))],
        extra=('fish', (400, 300, '#ff8f3c', 1.0))))
    # hard-22 海豚湾
    spec.append(dict(key='hard-22', name='海豚湾', theme='sea',
        deco=[('dolphin', (600, 330, '#5aa9e6')), ('seaweed', (140, 580, '#4fb84f')),
              ('sand_patch', (300, 575)), ('shell', (500, 565, '#f2c9a0')), ('bubbles', (720, 260, 4))],
        diffs=[('show', 'fish', (150, 130, '#ff8f3c', 1.0)),
               ('show', 'clownfish', (400, 130, 1.0)),
               ('swap', 'jellyfish', (650, 130, '#ff6fa5'), (650, 130, '#7fce8a')),
               ('show', 'shell', (128, 505, '#8b5cf6', 0.9)),
               ('swap', 'turtle', (400, 505, '#7fce8a', 0.62), (400, 505, '#8b5cf6', 0.62)),
               ('show', 'starfish', (620, 520, '#8b5cf6', 1.0))],
        extra=('fish', (400, 300, '#ff6fa5', 0.7))))
    # hard-23 深海奇观
    spec.append(dict(key='hard-23', name='深海奇观', theme='sea',
        deco=[('fish', (620, 300, '#5aa9e6', 0.9)), ('seaweed', (120, 580, '#4fb84f')),
              ('coral', (720, 570, '#b06ff0')), ('sand_patch', (400, 580)), ('bubbles', (300, 220, 5))],
        diffs=[('show', 'jellyfish', (150, 130, '#ff6fa5', 1.0)),
               ('show', 'fish', (400, 140, '#ffd93b', 0.8)),
               ('swap', 'fish', (650, 130, '#ffd93b', 1.0), (650, 130, '#7fce8a', 1.0)),
               ('show', 'octopus', (150, 505, '#ff8f3c', 1.0)),
               ('swap', 'starfish', (440, 505, '#ff8f3c', 0.8), (440, 505, '#8b5cf6', 0.8)),
               ('show', 'shell', (640, 545, '#8b5cf6', 1.2))],
        extra=('fish', (300, 330, '#ff6fa5', 1.0))))
    # hard-24 海底花园
    spec.append(dict(key='hard-24', name='海底花园', theme='sea',
        deco=[('coral', (620, 570, '#ff8f3c')), ('seaweed', (740, 580, '#4fb84f')),
              ('sand_patch', (300, 578)), ('bubbles', (200, 260, 4)), ('shell', (480, 570, '#f2c9a0'))],
        diffs=[('show', 'clownfish', (150, 130, 1.0)),
               ('show', 'fish', (400, 130, '#ffd93b', 1.0)),
               ('show', 'seahorse', (650, 130, '#ff6fa5', 1.0)),
               ('show', 'shell', (128, 505, '#8b5cf6', 0.9)),
               ('swap', 'coral', (620, 570, '#ff8f3c'), (620, 570, '#7fce8a')),
               ('show', 'crab', (370, 505, '#b06ff0', 1.0))],
        extra=('fish', (400, 300, '#ff8f3c', 1.0))))
    return spec

# ==========================================================================
# 游乐园
# ==========================================================================
def fun_bg(pal, v, hell):
    o = [sky(), hills((180, 420, 260, 90, pal['hill1']), (620, 430, 300, 100, pal['hill2'])), grass(400)]
    o.append('<path d="M0,430 q200,-24 400,-4 q200,20 400,-8 L800,470 L0,470 Z" fill="#f3e3bb"/>')
    for i, fx in enumerate(range(40, 780, 74)):
        o.append(flag(fx, 250 + (i % 2) * 26, '#ff6fa5' if i % 3 == 0 else ('#ffd93b' if i % 3 == 1 else '#4aa8e0'), 0.5))
    for tx in (80, 160, 250, 350, 450, 540, 630, 720, 780):
        o.append(tuft(tx, 588))
    for bx, by in ((120, 520), (330, 545), (520, 520), (700, 548)):
        o.append(ball(bx, by, 20, '#4aa8e0' if bx % 2 else '#ff6fa5'))
    return o


def tent(x, base, color, s=1.0, stripe='#ffffff'):
    """条纹帐篷：帐篷布颜色是差异"""
    w, h = 300 * s, 230 * s
    out = ('<path d="M%.0f,%.0f L%.0f,%.0f L%.0f,%.0f Z" fill="%s"/>'
           % (x, base - h, x - w / 2, base, x + w / 2, base, color))
    for i, k in enumerate((0.30, 0.58)):
        out += ('<path d="M%.0f,%.0f L%.0f,%.0f L%.0f,%.0f Z" fill="%s"/>'
                % (x - w * k * 0.5 - 26 * s, base - h * 0.62, x - w * k * 0.5 + 26 * s, base,
                   x - w * k * 0.5 - 30 * s, base, stripe))
    out += ('<path d="M%.0f,%.0f L%.0f,%.0f L%.0f,%.0f Z" fill="#3f4a5a"/>'
            % (x - 44 * s, base, x + 44 * s, base, x, base - 96 * s))
    return out


def flag(x, y, color, s=1.0):
    return ('<rect x="%s" y="%s" width="10" height="%s" fill="#9aa3ad"/>'
            '<path d="M%s,%s L%s,%s L%s,%s Z" fill="%s"/>'
            % (x - 5, y, 160 * s, x + 5, y + 6, x + 92 * s, y + 30 * s, x + 5, y + 58 * s, color))


def ferris(x, y, color, gondola, s=1.0):
    """摩天轮：45° 那个吊舱的颜色是差异"""
    r = 170 * s
    out = '<circle cx="%s" cy="%s" r="%.0f" fill="none" stroke="%s" stroke-width="13"/>' % (x, y, r, color)
    for k in range(8):
        a = math.radians(k * 22.5)
        out += ('<line x1="%s" y1="%s" x2="%.0f" y2="%.0f" stroke="%s" stroke-width="6"/>'
                % (x, y, x + r * math.cos(a), y + r * math.sin(a), color))
    out += ('<circle cx="%s" cy="%s" r="20" fill="%s"/>'
            '<path d="M%s,%.0f L%s,520 M%s,%.0f L%s,520" stroke="#8b949e" stroke-width="12"/>'
            % (x, y, color, x - 62, y + r, x - 46, x + 62, y + r, x + 46))
    for k in range(8):
        a = math.radians(k * 45)
        gx, gy = x + r * math.cos(a), y + r * math.sin(a)
        c = gondola if k == 1 else '#ffd93b'
        out += '<rect x="%.0f" y="%.0f" width="46" height="34" rx="9" fill="%s"/>' % (gx - 23, gy + 6, c)
    return out


def carousel(x, y, color, horse, s=1.0):
    """旋转木马：正前方那匹马的颜色是差异"""
    out = ('<ellipse cx="%s" cy="%s" rx="150" ry="34" fill="#c9a06a"/>'
           '<rect x="%s" y="%s" width="16" height="86" fill="#8b949e"/>'
           '<path d="M%s,%s L%s,%s L%s,%s Z" fill="%s"/>'
           % (x, y + 40, x - 8, y - 46, x, y - 150, x - 160, y - 46, x + 160, y - 46, color))
    for k in range(4):
        hx = x - 120 + k * 80
        c = horse if k == 2 else '#ffffff'
        out += ('<rect x="%s" y="%s" width="10" height="96" fill="#e8c07a"/>'
                '<ellipse cx="%s" cy="%s" rx="42" ry="24" fill="%s"/>'
                '<circle cx="%s" cy="%s" r="14" fill="%s"/>'
                '<rect x="%s" y="%s" width="12" height="30" rx="5" fill="%s"/>'
                % (hx, y - 60, hx, y - 10, c, hx - 34, y - 24, c, hx - 44, y - 46, c))
    return out


def coaster(x, y, color, cart, s=1.0):
    """过山车：小车颜色是差异"""
    return ('<path d="M%s,%s q90,-160 190,-40 q80,96 170,-10 q70,-80 150,20" fill="none" '
            'stroke="%s" stroke-width="16" stroke-linecap="round"/>'
            '<path d="M%s,%s L%s,%s M%s,%s L%s,%s M%s,%s L%s,%s" stroke="#9aa3ad" stroke-width="10"/>'
            % (x - 300, y, color,
               x - 240, y + 20, x - 250, y + 90, x - 60, y - 40, x - 70, y + 70,
               x + 120, y - 30, x + 110, y + 80)
            + '<rect x="%s" y="%s" width="96" height="46" rx="12" fill="%s"/>' % (x + 60, y - 76, cart)
            + '<rect x="%s" y="%s" width="20" height="20" rx="6" fill="#3f4a5a"/>' % (x + 96, y - 96))


def bumper(x, y, color, s=1.0):
    """碰碰车：车身颜色是差异"""
    return ('<ellipse cx="%s" cy="%s" rx="86" ry="26" fill="#8b949e" opacity="0.5"/>'
            '<rect x="%s" y="%s" width="150" height="52" rx="18" fill="%s"/>'
            '<rect x="%s" y="%s" width="86" height="46" rx="14" fill="%s"/>'
            '<rect x="%s" y="%s" width="64" height="34" rx="8" fill="#cdeafc"/>'
            '<circle cx="%s" cy="%s" r="20" fill="#3f4a5a"/><circle cx="%s" cy="%s" r="26" fill="#3f4a5a"/>'
            % (x, y, x - 78, y - 46, color, x - 60, y - 92, color, x - 50, y - 82, x + 40, y - 40, x - 40, y - 20))


def target(x, y, color, s=1.0):
    """靶心：最里圈颜色是差异"""
    return ('<circle cx="%s" cy="%s" r="86" fill="#ffffff" stroke="#c9a06a" stroke-width="8"/>'
            '<circle cx="%s" cy="%s" r="62" fill="#ff6fa5"/>'
            '<circle cx="%s" cy="%s" r="38" fill="#ffffff"/>'
            '<circle cx="%s" cy="%s" r="27" fill="%s"/>' % (x, y, x, y, x, y, x, y, color)
            + '<rect x="%s" y="%s" width="14" height="%s" fill="#c9a06a"/>' % (x - 7, y + 80, 90))


def candy(x, y, color='#ff6fa5', s=1.0):
    """棉花糖（有无是差异）"""
    return ('<rect x="%s" y="%s" width="12" height="%s" rx="6" fill="#e8c07a"/>'
            '<circle cx="%s" cy="%s" r="%s" fill="%s"/>'
            '<circle cx="%s" cy="%s" r="%s" fill="#ffffff" opacity="0.25"/>'
            % (x - 6, y - 96, 96, x, y - 116, 46 * s, color, x - 14 * s, y - 128 * s, 20 * s))


def fun_levels():
    spec = []
    # hard-25 游乐园大门
    spec.append(dict(key='hard-25', name='游乐园大门', theme='fun',
        puzzle_deco=[('candy', (560, 540, '#8b5cf6', 1.0))],
        deco=[('flag', (120, 300, '#ff6fa5')), ('flag', (700, 300, '#4aa8e0')),
              ('tent', (300, 560, '#ff8f3c')), ('tuft', (200, 580)), ('stone', (620, 575, 22, 13))],
        diffs=[('show', 'cloud', (150, 130)),
               ('show', 'balloon', (400, 110, '#ffd93b')),
               ('swap', 'sun', (650, 130, 34, (46, 66, 14)), (650, 130, 34, None)),
               ('show', 'candy', (128, 505, '#ff8f3c', 0.9)),
               ('show', 'candy', (370, 505, '#ff6fa5', 1.0)),
               ('swap', 'tent', (680, 545, '#ff8f3c', 0.45), (680, 545, '#8b5cf6', 0.45))],
        extra=('bee', (150, 300, 1.0))))
    # hard-26 摩天轮
    spec.append(dict(key='hard-26', name='摩天轮', theme='fun',
        deco=[('ferris', (570, 320, '#ff6fa5', '#ff6fa5')), ('tuft', (120, 580)), ('stone', (240, 575, 22, 13))],
        diffs=[('show', 'cloud', (150, 130)),
               ('show', 'kite', (400, 110, '#ff6fa5', False)),
               ('swap', 'sun', (650, 130, 30), (650, 130, 44)),
               ('show', 'candy', (128, 505, '#ff8f3c', 0.9)),
               ('show', 'candy', (370, 505, '#ffd93b', 1.0)),
               ('swap', 'ferris', (570, 320, '#ff6fa5', '#ff6fa5'), (570, 320, '#ff6fa5', '#4aa8e0'))],
        extra=('bee', (150, 300, 1.0))))
    # hard-27 旋转木马
    spec.append(dict(key='hard-27', name='旋转木马', theme='fun',
        deco=[('carousel', (560, 470, '#ff8f3c', '#ffffff')), ('tuft', (140, 580)),
              ('stone', (250, 575, 22, 13)), ('bush', (740, 540, 0.8))],
        diffs=[('show', 'cloud', (150, 130)),
               ('show', 'balloon', (400, 110, '#ffd93b')),
               ('swap', 'sun', (650, 130, 34, (46, 66, 14)), (650, 130, 34, None)),
               ('show', 'candy', (128, 505, '#ff8f3c', 0.9)),
               ('show', 'candy', (370, 505, '#ff8f3c', 1.0)),
               ('swap', 'carousel', (560, 470, '#ff8f3c', '#ffffff'), (560, 470, '#ff8f3c', '#4aa8e0'))],
        extra=('bee', (150, 300, 1.0))))
    # hard-28 过山车
    spec.append(dict(key='hard-28', name='过山车', theme='fun',
        deco=[('coaster', (620, 470, '#4aa8e0', '#ffd93b')), ('tuft', (120, 580)),
              ('stone', (250, 575, 22, 13)), ('flag', (740, 300, '#ff6fa5'))],
        diffs=[('show', 'cloud', (150, 130)),
               ('show', 'balloon', (400, 110, '#ffd93b')),
               ('swap', 'sun', (650, 130, 34, (46, 66, 14)), (650, 130, 34, None)),
               ('show', 'candy', (128, 505, '#ff8f3c', 0.9)),
               ('show', 'candy', (370, 505, '#ff8f3c', 1.0)),
               ('swap', 'coaster', (620, 470, '#4aa8e0', '#ffd93b'), (620, 470, '#4aa8e0', '#ff6fa5'))],
        extra=('bee', (150, 300, 1.0))))
    # hard-29 碰碰车
    spec.append(dict(key='hard-29', name='碰碰车', theme='fun',
        deco=[('bumper', (660, 470, '#4aa8e0')), ('tuft', (120, 580)), ('stone', (250, 575, 22, 13)),
              ('flag', (700, 300, '#ffd93b'))],
        diffs=[('show', 'cloud', (150, 130)),
               ('show', 'kite', (400, 110, '#ff6fa5', False)),
               ('swap', 'sun', (650, 130, 30), (650, 130, 44)),
               ('show', 'candy', (128, 505, '#ff8f3c', 0.9)),
               ('show', 'candy', (370, 505, '#b06ff0', 1.0)),
               ('swap', 'bumper', (660, 470, '#4aa8e0'), (660, 470, '#ff8f3c'))],
        extra=('bee', (150, 300, 1.0))))
    # hard-30 靶心游戏
    spec.append(dict(key='hard-30', name='靶心游戏', theme='fun',
        deco=[('target', (660, 470, '#ffd93b')), ('tuft', (120, 580)), ('stone', (250, 575, 22, 13)),
              ('ball', (200, 545, 30, '#4aa8e0'))],
        diffs=[('show', 'cloud', (150, 130)),
               ('show', 'balloon', (400, 110, '#ff6fa5')),
               ('swap', 'sun', (650, 130, 34, (46, 66, 14)), (650, 130, 34, None)),
               ('show', 'candy', (128, 505, '#ff8f3c', 0.9)),
               ('show', 'candy', (370, 505, '#ffd93b', 1.0)),
               ('swap', 'target', (660, 470, '#ffd93b'), (660, 470, '#4aa8e0'))],
        extra=('bee', (150, 300, 1.0))))
    return spec

# ==========================================================================
# 工地（第二批：塔吊/脚手架/建材/泵车/叉车/装载机）
# ==========================================================================
def site_bg(pal, v, hell):
    # 工地背景：整排锥筒/成对水泥管/草丛/石子（两版一致，差异藏在同类里）
    o = [sky(), hills((180, 420, 260, 90, pal['hill1']), (620, 430, 300, 100, pal['hill2'])),
         grass(400), dirt(500, 520, 360, 96)]
    for cx in (60, 130, 200, 270, 620, 690, 760):
        o.append(cone(cx, 588))
    for px in (90, 190, 700):
        o.append(pipes(px, 556, 2))
    for tx in (230, 310, 390, 470, 550, 640, 730):
        o.append(tuft(tx, 596))
    for sx, sy, sr in ((150, 545, 14), (330, 560, 12), (500, 542, 15), (660, 558, 12)):
        o.append(stone(sx, sy, sr, round(sr * 0.6)))
    o.append(cloud(280, 95) + cloud(600, 80))
    return o


def cement_bags(x, base, color='#e8e4da', n=3):
    """水泥袋堆（有无是差异）"""
    out = ''
    for r in range(n):
        for c in range(n - r):
            bx = x - (n - r) * 36 + c * 74
            out += (f'<rect x="{bx}" y="{base - 34 * (r + 1)}" width="72" height="34" rx="10" fill="{color}"/>'
                    f'<rect x="{bx + 10}" y="{base - 22 * (r + 1)}" width="52" height="6" rx="3" fill="#b39b74"/>')
    return out


def rebar(x, base, color='#9aa3ad'):
    """钢筋捆（颜色是差异）"""
    out = ''
    for i in range(4):
        out += f'<rect x="{x - 75}" y="{base - 16 - i * 16}" width="150" height="17" rx="6" fill="{color}"/>'
    return out


def tower_crane(x, base, color, part):
    """塔吊：配重块颜色是差异"""
    out = (
        f'<rect x="{x - 19}" y="{base - 300}" width="38" height="300" fill="{color}"/>'
        f'<rect x="{x - 210}" y="{base - 318}" width="420" height="24" rx="6" fill="{color}"/>'
        f'<path d="M{x - 200},{base - 296} L{x - 140},{base - 296} L{x - 140},{base - 356} '
        f'L{x - 200},{base - 356} Z" fill="{part}"/>'
        f'<rect x="{x - 70}" y="{base - 280}" width="60" height="56" rx="8" fill="{color}"/>'
        f'<rect x="{x - 62}" y="{base - 270}" width="56" height="42" rx="6" fill="#8fd9f5"/>'
        f'<path d="M{x + 60},{base - 312} L{x + 60},{base - 210}" stroke="#6b7280" stroke-width="6"/>'
        f'<rect x="{x + 38}" y="{base - 210}" width="46" height="30" rx="6" fill="#8b6b4a"/>')
    return out


def scaffold(x, base, color, plank):
    """脚手架：最上层踏板颜色是差异"""
    out = ''
    for dx in (-120, 0, 120):
        out += f'<rect x="{x + dx - 7}" y="{base - 170}" width="14" height="170" fill="{color}"/>'
    for y in (base - 170, base - 110, base - 50):
        out += f'<rect x="{x - 130}" y="{y}" width="260" height="12" rx="5" fill="{color}"/>'
    return out + f'<rect x="{x - 133}" y="{base - 186}" width="266" height="20" rx="6" fill="{plank}"/>'


def pump_truck(x, y, color, boom):
    """水泥泵车：泵管颜色是差异"""
    return (_wheel(x - 100, y - 28) + _wheel(x + 30, y - 28) + _wheel(x + 116, y - 28)
            + f'<rect x="{x - 150}" y="{y - 92}" width="300" height="48" rx="12" fill="#6b7280"/>'
            + f'<rect x="{x - 156}" y="{y - 170}" width="96" height="78" rx="12" fill="{color}"/>'
            + f'<rect x="{x - 144}" y="{y - 160}" width="66" height="44" rx="7" fill="#8fd9f5"/>'
            + f'<path d="M{x - 30},{y - 104} L{x + 120},{y - 196} L{x + 134},{y - 166} '
              f'L{x - 16},{y - 74} Z" fill="{boom}"/>'
            + f'<path d="M{x + 128},{y - 182} q26,34 6,64" fill="none" stroke="{boom}" '
              f'stroke-width="20" stroke-linecap="round"/>')


def forklift(x, y, color, part):
    """叉车：货叉颜色是差异"""
    return (_wheel(x - 96, y - 34, 32) + _wheel(x + 6, y - 34, 32)
            + f'<rect x="{x - 150}" y="{y - 150}" width="190" height="86" rx="16" fill="{color}"/>'
            + f'<rect x="{x - 142}" y="{y - 230}" width="90" height="80" rx="12" fill="{color}"/>'
            + f'<rect x="{x - 128}" y="{y - 220}" width="62" height="46" rx="7" fill="#8fd9f5"/>'
            + f'<rect x="{x + 42}" y="{y - 240}" width="18" height="150" fill="#8b949e"/>'
            + f'<rect x="{x + 74}" y="{y - 240}" width="16" height="150" fill="#8b949e"/>'
            + f'<rect x="{x + 90}" y="{y - 30}" width="96" height="18" rx="4" fill="{part}"/>'
            + f'<rect x="{x + 90}" y="{y - 6}" width="96" height="14" rx="4" fill="{part}"/>')


def loader(x, y, color, part):
    """装载机：铲斗颜色是差异"""
    return (_wheel(x - 96, y - 40, 40) + _wheel(x + 26, y - 40, 40)
            + f'<rect x="{x - 150}" y="{y - 156}" width="200" height="92" rx="18" fill="{color}"/>'
            + f'<rect x="{x - 142}" y="{y - 240}" width="96" height="84" rx="12" fill="{color}"/>'
            + f'<rect x="{x - 128}" y="{y - 230}" width="66" height="48" rx="7" fill="#8fd9f5"/>'
            + f'<path d="M{x + 40},{y - 150} L{x + 150},{y - 110} L{x + 150},{y - 20} '
              f'L{x + 40},{y - 40} Z" fill="{part}"/>')


def site2_levels():
    spec = []
    # hard-31 塔吊作业
    spec.append(dict(key='hard-31', name='塔吊作业', theme='site',
        deco=[('building', (140, 400)), ('barrier', (600, 400, 3)), ('windsock', (260, 560)),
              ('tuft', (420, 580)), ('stone', (760, 560, 24, 14))],
        diffs=[('show', 'cloud', (150, 130)),
               ('swap', 'tower_crane', (640, 560, '#e8b53c', '#ff8f3c'), (640, 560, '#e8b53c', '#3f7ff0')),
               ('swap', 'sun', (325 * 2, 130, 34, (46, 66, 14)), (325 * 2, 130, 34, None)),
               ('show', 'cone', (140, 505)),
               ('show', 'cement_bags', (400, 505, '#e8e4da', 2)),
               ('show', 'rebar', (700, 520, '#9aa3ad'))],
        extra=('bee', (150, 300, 1.0))))
    # hard-32 脚手架
    spec.append(dict(key='hard-32', name='脚手架', theme='site',
        deco=[('building', (150, 400)), ('scaffold', (560, 560, '#9aa3ad', '#c98b52')),
              ('tuft', (240, 580)), ('stone', (760, 560, 24, 14))],
        diffs=[('show', 'cloud', (150, 130)),
               ('show', 'balloon', (400, 110, '#ffd93b')),
               ('swap', 'sun', (650, 130, 34, (46, 66, 14)), (650, 130, 34, None)),
               ('show', 'cone', (140, 505)),
               ('show', 'cement_bags', (400, 505, '#e8e4da', 2)),
               ('show', 'cone', (700, 520))],
        extra=('bee', (150, 300, 1.0))))
    # hard-33 建材堆场
    spec.append(dict(key='hard-33', name='建材堆场', theme='site',
        deco=[('building', (140, 400)), ('barrier', (620, 400, 3)), ('stone', (560, 565, 26, 15)),
              ('tuft', (240, 580)), ('stone', (760, 560, 24, 14))],
        diffs=[('show', 'cloud', (150, 130)),
               ('show', 'balloon', (400, 110, '#ffd93b')),
               ('swap', 'sun', (650, 130, 34, (46, 66, 14)), (650, 130, 34, None)),
               ('show', 'cone', (140, 505)),
               ('show', 'cement_bags', (400, 505, '#e8e4da', 2)),
               ('show', 'cone', (700, 545))],
        extra=('bee', (150, 300, 1.0))))
    # hard-34 水泥泵车
    spec.append(dict(key='hard-34', name='水泥泵车', theme='site',
        deco=[('building', (140, 400)), ('barrier', (620, 400, 3)), ('rebar', (250, 570, '#9aa3ad')),
              ('tuft', (760, 580)), ('stone', (760, 600, 22, 13))],
        diffs=[('show', 'cloud', (150, 130)),
               ('show', 'balloon', (400, 110, '#ffd93b')),
               ('swap', 'sun', (650, 130, 34, (46, 66, 14)), (650, 130, 34, None)),
               ('show', 'cone', (140, 505)),
               ('show', 'cement_bags', (400, 505, '#e8e4da', 2)),
               ('swap', 'pump_truck', (640, 520, '#2f7fd0', '#ffd93b'), (640, 520, '#2f7fd0', '#3f7ff0'))],
        extra=('bee', (150, 300, 1.0))))
    # hard-35 叉车搬运
    spec.append(dict(key='hard-35', name='叉车搬运', theme='site',
        deco=[('building', (140, 400)), ('barrier', (620, 400, 3)), ('cement_bags', (280, 570, '#e8e4da')),
              ('tuft', (760, 580)), ('stone', (200, 600, 22, 13))],
        diffs=[('show', 'cloud', (150, 130)),
               ('show', 'balloon', (400, 110, '#ffd93b')),
               ('swap', 'sun', (650, 130, 34, (46, 66, 14)), (650, 130, 34, None)),
               ('show', 'cone', (140, 505)),
               ('show', 'rebar', (370, 500, '#9aa3ad')),
               ('swap', 'forklift', (520, 520, '#e8b53c', '#ff8f3c'), (520, 520, '#e8b53c', '#3f7ff0'))],
        extra=('bee', (150, 300, 1.0))))
    # hard-36 装载机
    spec.append(dict(key='hard-36', name='装载机', theme='site',
        deco=[('building', (140, 400)), ('barrier', (620, 400, 3)), ('pipes', (250, 570, 3)),
              ('tuft', (760, 580)), ('stone', (200, 600, 22, 13))],
        diffs=[('show', 'cloud', (150, 130)),
               ('show', 'balloon', (400, 110, '#ffd93b')),
               ('swap', 'sun', (650, 130, 34, (46, 66, 14)), (650, 130, 34, None)),
               ('show', 'cone', (140, 505)),
               ('show', 'cement_bags', (400, 505, '#e8e4da', 2)),
               ('show', 'cone', (700, 520))],
        extra=('bee', (150, 300, 1.0))))
    return spec

# ==========================================================================
# 动物园数字关：每只动物身上挂一个号码牌，找不同的关键是比对号码
# ==========================================================================
def num_tag(x, y, d, h=34, face='#f8efdd', edge='#b99a6b', ink='#4a3a28', cord=False):
    """动物脖子上的小号码牌：h = 数字高度（按这只动物的体型给）；边框很淡，避免抢戏"""
    w = h * 0.72
    total = len(str(d)) * w + (len(str(d)) - 1) * h * 0.16
    pw, ph = max(h * 0.9, total + h * 0.44), h + h * 0.36
    rope = (f'<path d="M{x - pw * 0.26:.1f},{y - ph / 2:.1f} L{x - pw * 0.16:.1f},{y - ph * 1.7:.1f} '
            f'M{x + pw * 0.26:.1f},{y - ph / 2:.1f} L{x + pw * 0.16:.1f},{y - ph * 1.7:.1f}" '
            f'stroke="{edge}" stroke-width="{max(1.4, h * 0.05):.1f}" stroke-opacity="0.5" fill="none"/>'
            ) if cord else ''
    return (rope + f'<rect x="{x - pw / 2:.1f}" y="{y - ph / 2:.1f}" width="{pw:.1f}" height="{ph:.1f}" '
            f'rx="{h * 0.26:.0f}" fill="{face}" fill-opacity="0.94" stroke="{edge}" '
            f'stroke-width="{max(1, h * 0.045):.1f}" stroke-opacity="0.4"/>'
            + digit(x - total / 2, y - h / 2, d, h, ink))


# (动物 x, 动物地面 y, 号码牌 y) —— 两排各 5 只，号码 1~10 按顺序挂在身上
ZOO_GRID = ((95, 430, 350), (255, 430, 350), (415, 430, 350), (575, 430, 350), (735, 430, 350),
            (55, 580, 500), (215, 580, 500), (375, 580, 500), (535, 580, 500), (695, 580, 500))

ZOO_KINDS = ('rabbit', 'monkey', 'panda', 'penguin', 'duckling')


ZOO_ANIMAL_SCALE = {'rabbit': 1.4, 'monkey': 1.35, 'panda': 1.15, 'penguin': 1.1, 'duckling': 0.95}
# 各种动物的自然身高（s=1 时），用来算号码牌该多大：小鸭小牌、熊猫大牌
ZOO_ANIMAL_H = {'rabbit': 110, 'monkey': 112, 'panda': 130, 'penguin': 132, 'duckling': 100}


def _zoo_animal(kind, x, y):
    sc = ZOO_ANIMAL_SCALE[kind]
    if kind == 'rabbit':
        return ('rabbit', (x, y, sc))
    if kind == 'monkey':
        return ('monkey', (x, y, sc))
    if kind == 'panda':
        return ('panda', (x, y, sc))
    if kind == 'penguin':
        return ('penguin', (x, y, sc))
    return ('duckling', (x, y, False, '#fffdf5', sc))


def zoo_num_scene(v, pal, hell, plan):
    """动物园数字关：10 只动物 + 10 个号码牌；号码差异之外再放 4 处普通差异"""
    o = [defs(pal), sky(), hills((180, 420, 260, 90, pal['hill1']),
                                  (620, 430, 300, 100, pal['hill2'])), grass(400)]
    o.append('<ellipse cx="400" cy="512" rx="390" ry="118" fill="#e9dcb6"/>')          # 场地
    o.append(fence(30, 430, 9, step=88, bw=22, bh=72))                                # 后围栏
    for tx in (60, 180, 300, 420, 540, 660, 780):
        o.append(tuft(tx, 592))
    changed = {(i,): (a, b) for i, a, b in plan['nums']}
    order = plan['order']
    num_slots = plan['numbered'] if isinstance(plan['numbered'], (list, tuple)) else [plan['numbered']]
    for i, (ax, ay, _t) in enumerate(ZOO_GRID):
        kind = ZOO_KINDS[(i + order) % len(ZOO_KINDS)]
        nm, args = _zoo_animal(kind, ax, ay)
        if i in num_slots:                                     # 挂牌的动物画大一点
            args = tuple(list(args[:-1]) + [args[-1] * 1.15])
        o.append(KIT[nm](*args))
    for i in num_slots:
        ax, ay, _t = ZOO_GRID[i]
        kind = ZOO_KINDS[(i + order) % len(ZOO_KINDS)]
        hgt = ZOO_ANIMAL_H[kind] * ZOO_ANIMAL_SCALE[kind] * 1.15            # 这只动物实际身高
        d = changed[(i,)]
        num = d[0] if v == 'A' else d[1]
        o.append(num_tag(ax, ay - hgt * 0.28, num, h=min(28.0, max(26.0, hgt * 0.155))))
    k = DIFF_SCALE.get('zoo', 1.0)
    for item in plan['diffs']:
        if item[0] == 'show':
            if v == 'A':
                a = item[2]
                o.append(_shrink(KIT[item[1]](*a), a[0], a[1], k))
        elif item[0] == 'swap':
            a = item[2] if v == 'A' else item[3]
            o.append(_shrink(KIT[item[1]](*a), a[0], a[1], k))
    return o


def zoo_num_builder(plan):
    return lambda v, pal, hell: zoo_num_scene(v, pal, hell, plan)


def zoo_num_levels():
    """6 关：号码变化的位置/数字对、动物顺序、其余 4 处普通差异各不相同"""
    d_cloud = ('show', 'cloud', (150, 130))
    ray_on = ('swap', 'sun', (650, 130, 34, (46, 66, 14)), (650, 130, 34, None))
    ray_big = ('swap', 'sun', (650, 130, 30), (650, 130, 44))
    balloon = ('show', 'balloon', (400, 110, '#ffd93b'))
    kite = ('show', 'kite', (400, 110, '#ff6fa5', False))
    stone = ('show', 'ball', (730, 520, 22, '#ff8f3c'))
    flower = ('swap', 'flower', (70, 545, '#ff6fa5', 1.1), (70, 545, '#ff8f3c', 1.1))
    bushc = ('swap', 'bush', (762, 528, 0.7, '#4fb84f'), (762, 528, 0.7, '#c9d94a'))
    plans = [
        dict(key='hard-37', name='动物园·号码牌 1', order=0, numbered=[0, 6], nums=[(0, '1', '8'), (6, '7', '8')],
             diffs=[d_cloud, ray_on, balloon, stone]),
        dict(key='hard-38', name='动物园·号码牌 2', order=1, numbered=[0, 6], nums=[(0, '1', '8'), (6, '7', '8')],
             diffs=[d_cloud, ray_big, kite, flower]),
        dict(key='hard-39', name='动物园·号码牌 3', order=2, numbered=[1, 6], nums=[(1, '1', '8'), (6, '7', '8')],
             diffs=[d_cloud, ray_on, balloon, bushc]),
        dict(key='hard-40', name='动物园·号码牌 4', order=3, numbered=[0, 6], nums=[(0, '1', '8'), (6, '7', '8')],
             diffs=[d_cloud, ray_big, kite, stone]),
        dict(key='hard-41', name='动物园·号码牌 5', order=4, numbered=[1, 6], nums=[(1, '1', '8'), (6, '7', '8')],
             diffs=[d_cloud, ray_on, balloon, flower]),
        dict(key='hard-42', name='动物园·号码牌 6', order=0, numbered=[0, 6], nums=[(0, '7', '8'), (6, '1', '8')],
             diffs=[d_cloud, ray_big, kite, bushc]),
    ]
    return plans


# 数据化关卡用的元素表：关卡的 deco/diffs 里只写名字，这里解析成真正画图函数
KIT = dict((n, globals()[n]) for n in (
    'cloud sun birds tree bush flower tuft stone fence bench lamp pond path_center duck swan lily '
    'dragonfly butterfly bee balloon gate booth slide swing seesaw ball kite arch watering basket '
    'blanket mushroom squirrel boat reeds dirt cone pipes bricks windsock barrier building excavator '
    'bulldozer mixer_truck dump_truck road_roller crane_truck pen elephant giraffe monkey rabbit '
    'penguin panda bamboo bowl banana hillrock digit plaque sand_patch bubbles fish clownfish butterfly_fish octopus turtle jellyfish starfish crab seahorse coral seaweed shell treasure anchor submarine bubble_num dolphin tent flag ferris carousel coaster bumper target candy cement_bags rebar tower_crane scaffold pump_truck forklift loader duckling').split())
THEME_BG = {'zoo': zoo_bg, 'sea': sea_bg, 'fun': fun_bg, 'site': site_bg}

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
# 动物园 6 关：用数据表描述，A/B 只差在 diffs 上
def _spec_names(spec):
    out = []
    for item in spec['diffs']:
        if item[0] == 'show':
            out.append('%s（有无）' % item[1])
        elif item[0] == 'swap':
            out.append('%s（颜色/大小）' % item[1])
        else:
            out.append('数字 %s→%s' % (item[3], item[4]))
    return out


for _sp in zoo_levels() + sea_levels() + fun_levels() + site2_levels():
    _HARD.append((_sp['key'], _sp['name'], spec_builder(_sp), _spec_names(_sp), _sp['extra'][0]))

_HELL_SUFFIX = ['黄昏', '清晨', '午后', '春日', '秋日', '夜晚']
for _k, (_key, _name, _build, _diffs, _extra) in enumerate(_HARD):
    SCENES.append(dict(key=_key, name=_name, build=_build, pal=PAL_DAY, hell=False,
                       diffs=_diffs, want=6))
    SCENES.append(dict(key='hell-%d' % (_k + 1), name=_name + '·' + _HELL_SUFFIX[_k % len(_HELL_SUFFIX)],
                       build=_build, pal=PALETTES[_k % len(PALETTES)], hell=True,
                       diffs=_diffs + [_extra], want=7))


# 动物园数字关：只加困难模式（不派生地狱版）
for _plan in zoo_num_levels():
    SCENES.append(dict(key=_plan['key'], name=_plan['name'], build=zoo_num_builder(_plan),
                       pal=PAL_DAY, hell=False, min_px=60,
                       diffs=['号码牌数字（比对每只动物身上的数字）'] + [d[1] for d in _plan['diffs']],
                       want=6))


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
                % (os.path.basename(svg_path), SHOT[0], SHOT[1]))
    png = svg_path[:-4] + '.png'
    try:
        chrome_shot(html, png, SHOT, tag)
        Image.open(png).convert('RGB').save(
            jpg_path, 'JPEG', quality=90, subsampling=0, optimize=True, progressive=True)
    finally:
        for p in (html, png):
            if os.path.exists(p):
                os.remove(p)
    return os.path.getsize(jpg_path)


# ---- 差异检测：复刻 index.html 的 computePhotoDiffs() ----
def detect(a_path, b_path):
    ia = Image.open(a_path).convert('RGB').resize(GAME, Image.BILINEAR)
    ib = Image.open(b_path).convert('RGB').resize(GAME, Image.BILINEAR)
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
    min_px = sc.get('min_px', MIN_DIFF_PX)
    a = os.path.join(IMG_DIR, '%s-A.jpg' % sc['key'])
    b = os.path.join(IMG_DIR, '%s-B.jpg' % sc['key'])
    ds = detect(a, b)
    problems = []
    if len(ds) != sc['want']:
        problems.append('识别到 %d 处差异（应为 %d 处）' % (len(ds), sc['want']))
    for i, d in enumerate(ds, 1):
        if d['area'] < min_px:
            problems.append('第 %d 处只有 %d 像素（< %d）' % (i, d['area'], min_px))
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
