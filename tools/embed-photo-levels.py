#!/usr/bin/env python3
"""把找不同的 A/B 图片打包成 spot-difference/photos-data.js

为什么要把图片内嵌成 base64，而不是直接放 images/*.jpg？
  游戏要"自己比对两张图"来找出差异，这需要读画布像素（getImageData）。
  但直接双击 index.html 用 file:// 打开时，Chrome 会把本地图片当成跨域资源，
  一旦读像素就抛 SecurityError（之前踩过：右图空白、圈圈不出、提示不亮）。
  data URI 内嵌的图片不会污染画布，file:// 下也能正常比对。
  另外做成独立的 .js 而不是塞进 index.html，是为了让 HTML 保持可读可改。

产物：spot-difference/photos-data.js
  window.PHOTO_DATA = {
    'hard-1': { name: '塔吊与脚手架', A: 'data:image/jpeg;base64,...', B: '...' },
    ...
  }
"""
import base64, os, sys

# 注意：images/ 里的 400x300 jpg 是构建产物，没有入库（客户端只用内嵌后的 photos-data.js）。
# 新克隆的仓库里如果 images/ 是空的，先跑 python3 tools/gen-svg-park-levels.py 生成它们。
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMG = os.path.join(ROOT, 'spot-difference', 'images')
OUT = os.path.join(ROOT, 'spot-difference', 'photos-data.js')

# 关卡顺序与显示名（图片由 tools/gen-svg-park-levels.py 生成）
LEVELS = [
    ('hard-1', '公园大门'),
    ('hard-2', '池塘边'),
    ('hard-3', '儿童乐园'),
    ('hard-4', '花园花圃'),
    ('hard-5', '树林野餐'),
    ('hard-6', '湖边散步'),
    ('hard-7', '挖掘机作业'),
    ('hard-8', '推土机平整'),
    ('hard-9', '水泥搅拌车'),
    ('hard-10', '自卸卡车'),
    ('hard-11', '压路机'),
    ('hard-12', '汽车吊'),
    ('hard-13', '动物园大门'),
    ('hard-14', '大象馆'),
    ('hard-15', '长颈鹿'),
    ('hard-16', '猴山'),
    ('hard-17', '熊猫馆'),
    ('hard-18', '百鸟园'),
    ('hard-19', '珊瑚礁'),
    ('hard-20', '沉船宝藏'),
    ('hard-21', '潜水艇'),
    ('hard-22', '海豚湾'),
    ('hard-23', '深海奇观'),
    ('hard-24', '海底花园'),
    ('hard-25', '游乐园大门'),
    ('hard-26', '摩天轮'),
    ('hard-27', '旋转木马'),
    ('hard-28', '过山车'),
    ('hard-29', '碰碰车'),
    ('hard-30', '靶心游戏'),
    ('hard-31', '塔吊作业'),
    ('hard-32', '脚手架'),
    ('hard-33', '建材堆场'),
    ('hard-34', '水泥泵车'),
    ('hard-35', '叉车搬运'),
    ('hard-36', '装载机'),
    ('hard-37', '动物园·号码牌 1'),
    ('hard-38', '动物园·号码牌 2'),
    ('hard-39', '动物园·号码牌 3'),
    ('hard-40', '动物园·号码牌 4'),
    ('hard-41', '动物园·号码牌 5'),
    ('hard-42', '动物园·号码牌 6'),
    ('hell-1', '公园大门·黄昏'),
    ('hell-2', '池塘边·清晨'),
    ('hell-3', '儿童乐园·午后'),
    ('hell-4', '花园花圃·春日'),
    ('hell-5', '树林野餐·秋日'),
    ('hell-6', '湖边散步·夜晚'),
    ('hell-7', '挖掘机作业·黄昏'),
    ('hell-8', '推土机平整·清晨'),
    ('hell-9', '水泥搅拌车·午后'),
    ('hell-10', '自卸卡车·春日'),
    ('hell-11', '压路机·秋日'),
    ('hell-12', '汽车吊·夜晚'),
    ('hell-13', '动物园大门·黄昏'),
    ('hell-14', '大象馆·清晨'),
    ('hell-15', '长颈鹿·午后'),
    ('hell-16', '猴山·春日'),
    ('hell-17', '熊猫馆·秋日'),
    ('hell-18', '百鸟园·夜晚'),
    ('hell-19', '珊瑚礁·黄昏'),
    ('hell-20', '沉船宝藏·清晨'),
    ('hell-21', '潜水艇·午后'),
    ('hell-22', '海豚湾·春日'),
    ('hell-23', '深海奇观·秋日'),
    ('hell-24', '海底花园·夜晚'),
    ('hell-25', '游乐园大门·黄昏'),
    ('hell-26', '摩天轮·清晨'),
    ('hell-27', '旋转木马·午后'),
    ('hell-28', '过山车·春日'),
    ('hell-29', '碰碰车·秋日'),
    ('hell-30', '靶心游戏·夜晚'),
    ('hell-31', '塔吊作业·黄昏'),
    ('hell-32', '脚手架·清晨'),
    ('hell-33', '建材堆场·午后'),
    ('hell-34', '水泥泵车·春日'),
    ('hell-35', '叉车搬运·秋日'),
    ('hell-36', '装载机·夜晚'),
]


def main():
    missing, entries = [], []
    for key, name in LEVELS:
        pa = os.path.join(IMG, key + '-A.jpg')
        pb = os.path.join(IMG, key + '-B.jpg')
        if not (os.path.exists(pa) and os.path.exists(pb)):
            missing.append(key)
            continue
        with open(pa, 'rb') as f:
            a = base64.b64encode(f.read()).decode()
        with open(pb, 'rb') as f:
            b = base64.b64encode(f.read()).decode()
        entries.append(
            "  '%s': {\n    name: '%s',\n    A: 'data:image/jpeg;base64,%s',\n"
            "    B: 'data:image/jpeg;base64,%s'\n  }," % (key, name, a, b))
    if missing:
        print('✗ 缺少这些关卡的图：%s' % ', '.join(missing))
        sys.exit(1)

    js = ("/* 找不同·真图关卡的图片数据（由 tools/embed-photo-levels.py 生成，请勿手改）\n"
          " *\n"
          " * 图片以 base64 内嵌，而不是放在 images/ 下按相对路径引用，原因：\n"
          " * 游戏需要读画布像素来自己比对 A/B 两张图（找出差异），\n"
          " * 而 file:// 下按路径加载的本地图片会污染画布，读像素会抛 SecurityError；\n"
          " * data URI 不会污染画布，双击打开也能正常比对。\n"
          " *\n"
          " * 想换图：把新图覆盖到 spot-difference/images/<关卡>-A.jpg / -B.jpg，\n"
          " * 然后重新运行 python3 tools/embed-photo-levels.py 即可。\n"
          " */\n"
          "window.PHOTO_DATA = {\n" + '\n'.join(entries) + "\n};\n")
    with open(OUT, 'w', encoding='utf-8') as f:
        f.write(js)
    print('已生成 %s（%d 关，%.0f KB）'
          % (os.path.relpath(OUT, ROOT), len(entries), os.path.getsize(OUT) / 1024))
    for key, name in LEVELS:
        sa = os.path.getsize(os.path.join(IMG, key + '-A.jpg'))
        sb = os.path.getsize(os.path.join(IMG, key + '-B.jpg'))
        print('  %-8s %-8s A=%2dKB B=%2dKB' % (key, name, sa // 1024, sb // 1024))


if __name__ == '__main__':
    main()
