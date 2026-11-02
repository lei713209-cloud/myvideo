#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright 2026 lei713209-cloud
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""subtitles — PIL 渲染竖/横屏字幕条 PNG（白字黑描边 + 可选高亮词）。

不依赖 ffmpeg 滤镜（drawtext/subtitles 在精简构建上常缺失）。
生成与视频同画布的透明 PNG，配 post/assemble.py 的 overlay 时间窗使用。
生成的 PNG 一律为整幅画布尺寸，文字居中于底部安全区。

用法：
    python3 subtitles.py --texts lines.txt --outdir subs/ --width 768 --height 1344 \
        --font /path/to/CJK.ttc --highlight 关键词1,关键词2
lines.txt: 每行一句字幕；输出 L1.png L2.png ...（顺序对应）。
"""
import argparse
import os

from PIL import Image, ImageDraw, ImageFont


def fit_font(text: str, font_path: str, start: int, floor: int, max_width: int) -> ImageFont.FreeTypeFont:
    size = start
    while size > floor:
        font = ImageFont.truetype(font_path, size)
        bbox = font.getbbox(text)
        if bbox[2] - bbox[0] <= max_width:
            return font
        size -= 2
    return ImageFont.truetype(font_path, floor)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--texts", required=True, help="UTF-8 file, one caption per line")
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--width", type=int, default=768)
    ap.add_argument("--height", type=int, default=1344)
    ap.add_argument("--font", default="/System/Library/Fonts/STHeiti Medium.ttc")
    ap.add_argument("--fontsize", type=int, default=52)
    ap.add_argument("--min-fontsize", type=int, default=24)
    # 安全宽度收紧：交付如做 cover 上变换（放大后居中裁），左右边缘会被裁掉几个像素，
    # 故默认预留 80px/侧，避免"贴边"在成片里变成"裁字"。
    ap.add_argument("--side-margin", type=int, default=80)
    ap.add_argument("--baseline", type=float, default=0.88, help="text baseline, fraction of height")
    ap.add_argument("--outline", type=int, default=4)
    ap.add_argument("--color", default="#FFFFFF")
    ap.add_argument("--highlight-color", default="#FFD66E")
    ap.add_argument("--highlight", default="", help="comma-separated substrings to recolor")
    args = ap.parse_args()

    lines = [l.strip() for l in open(args.texts, encoding="utf-8") if l.strip()]
    os.makedirs(args.outdir, exist_ok=True)
    max_w = args.width - 2 * args.side_margin

    for i, line in enumerate(lines, 1):
        font = fit_font(line, args.font, args.fontsize, args.min_fontsize, max_w)
        img = Image.new("RGBA", (args.width, args.height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        bbox = draw.textbbox((0, 0), line, font=font)
        tw = bbox[2] - bbox[0]
        x = (args.width - tw) // 2
        y = int(args.height * args.baseline)
        # 先整体描边
        draw.text((x, y), line, font=font, fill=(*Image.new("RGB", (1, 1), args.color).getpixel((0, 0)), 255),
                 stroke_width=args.outline, stroke_fill=(0, 0, 0, 230))
        # 再按高亮词逐段上色（简单顺序扫描，允许重叠词取先命中的）
        if args.highlight:
            words = [w for w in args.highlight.split(",") if w and w in line]
            cx = x
            rest = line
            while rest:
                hits = [(rest.find(w), w) for w in words if w in rest]
                if not hits:
                    break
                pos, w = min(hits, key=lambda t: t[0])
                pre = rest[:pos]
                cx += draw.textlength(pre, font=font)
                draw.text((cx, y), w, font=font, fill=(*Image.new("RGB", (1, 1), args.highlight_color).getpixel((0, 0)), 255),
                         stroke_width=args.outline, stroke_fill=(0, 0, 0, 230))
                cx += draw.textlength(w, font=font)
                rest = rest[pos + len(w):]
        out = os.path.join(args.outdir, f"L{i}.png")
        img.save(out)
        print("wrote", out)


if __name__ == "__main__":
    main()
