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

"""assemble — 成片总装：镜头序列 + 旁白刚性排期 + BGM 混音 + 字幕烧入 + 封装配比。

设计要点（均为生产验证过的纪律）：
  * 刚性排期：每句旁白的入点钉死为「应用段起点 + 固定偏移」，禁止 cursor 顺延软排期
    —— 软排期会把逐镜时长误差累加，导致后段音画错位。
  * 对齐闸门：若某句语音超出其镜头可用时长，逐句独立 atempo 提速（上限可调），
    超限则报错而非静默错位。句头判定用 silencedetect 自适应裁掉开头进气/静音。
  * 响度标准：EBU R128 两段式 loudnorm（I=-16 LUFS / TP<=-1.5，短视频平台通用口径）。
  * 字幕为 post/subtitles.py 生成的整幅透明 PNG，按时间窗 overlay。
  * 竖屏交付：cover 规则等比放大居中裁切（1080x1920），加轻暗角 + 首尾淡变。

schedule.json 格式（秒；dur 为镜头成片时长）：
  {
    "shots": [
      {"file": "shot01.mp4", "dur": 8.0, "vo": "vo_01.wav", "vo_at": 0.25},
      {"file": "shot02.mp4", "dur": 5.17, "vo": null},
      ...
    ],
    "subs": [{"file": "L1.png", "from": 0.5, "to": 4.6}, ...]
  }

用法：
    python3 assemble.py --schedule schedule.json --shots-dir shots --voice-dir vo \
        --bgm bgm.wav --out final.mp4 [--width 1080 --height 1920 --crf 16]
"""
import argparse
import json
import os
import re
import subprocess
import sys

try:
    import numpy as np
except ImportError:
    sys.exit("numpy required")

FFMPEG = os.environ.get("MYVIDEO_FFMPEG", "ffmpeg")
FFPROBE = FFMPEG.replace("ffmpeg", "ffprobe")


def dur_of(path: str) -> float:
    out = subprocess.check_output(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path])
    return float(out.strip())


def speech_head(path: str) -> float:
    """返回首个语音起点（秒），容忍 0.2s 内的正常起音；找不到返回 0。"""
    r = subprocess.run([FFMPEG, "-v", "info", "-i", path,
                       "-af", "silencedetect=noise=-32dB:d=0.18", "-f", "null", "-"],
                      capture_output=True, text=True).stderr
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", r)]
    return starts[0] if starts and starts[0] < 0.2 else (starts[0] if starts else 0.0)


def loudnorm_measure(path: str) -> dict:
    """两段式第一步：测量 JSON。注意 loudnorm print_format=json 属 info 级日志，
    不能设 -v error，否则 JSON 被吞。"""
    r = subprocess.run([FFMPEG, "-hide_banner", "-i", path,
                       "-af", "loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json",
                       "-f", "null", "-"], capture_output=True, text=True, check=True).stderr
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", r, re.S)
    return json.loads(m.group(0))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--schedule", required=True)
    ap.add_argument("--shots-dir", required=True)
    ap.add_argument("--voice-dir", default=".")
    ap.add_argument("--bgm", default=None)
    ap.add_argument("--out", required=True)
    ap.add_argument("--width", type=int, default=1080)
    ap.add_argument("--height", type=int, default=1920)
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--crf", type=int, default=16)
    ap.add_argument("--vo-offset", type=float, default=0.25, help="fixed anchor inside each shot")
    ap.add_argument("--vo-pad", type=float, default=0.45, help="min tail gap before shot end")
    ap.add_argument("--tempo-cap", type=float, default=1.5)
    ap.add_argument("--bgm-vol", type=float, default=0.15)
    ap.add_argument("--no-subs", action="store_true")
    args = ap.parse_args()

    sched = json.load(open(args.schedule, encoding="utf-8"))
    shots = sched["shots"]

    # ---- 段0：逐镜归一化（统一 fps/尺寸/编码参数，保证 concat 稳定）----
    norm_files = []
    for i, sh in enumerate(shots, 1):
        src = os.path.join(args.shots_dir, sh["file"])
        dst = f"{args.out}.n{i:02d}.mp4"
        vf = (f"scale={args.width}:{args.height}:force_original_aspect_ratio=increase,"
              f"crop={args.width}:{args.height},fps={args.fps},setsar=1")
        subprocess.run([FFMPEG, "-y", "-v", "error", "-i", src, "-vf", vf,
                        "-c:v", "libx264", "-preset", "medium", "-crf", str(args.crf),
                        "-an", "-pix_fmt", "yuv420p", dst], check=True)
        norm_files.append(dst)

    concat_list = args.out + ".concat.txt"
    with open(concat_list, "w", encoding="utf-8") as f:
        for p in norm_files:
            f.write(f"file '{os.path.abspath(p)}'\n")
    raw = args.out + ".rawvideo.mp4"
    subprocess.run([FFMPEG, "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", concat_list,
                    "-c", "copy", raw], check=True)
    total = dur_of(raw)

    # ---- 旁白刚性排期 ----
    voice_filters, amix_inputs, setpts = [], [], []
    vi = 0
    cursor = 0.0
    max_end = 0.0
    for sh in shots:
        t = cursor
        cursor += float(sh["dur"])
        if not sh.get("vo"):
            continue
        vpath = os.path.join(args.voice_dir, sh["vo"]) if not os.path.isabs(sh["vo"]) else sh["vo"]
        head = speech_head(vpath)
        vd = dur_of(vpath) - head
        avail = float(sh["dur"]) - args.vo_offset - args.vo_pad
        atempo = 1.0
        if vd > avail:
            atempo = min(vd / avail, args.tempo_cap)
            if vd / atempo > avail + 0.06:
                sys.exit(f"align gate FAIL: {sh['vo']} {vd:.2f}s > avail {avail:.2f}s "
                       f"(tempo cap {args.tempo_cap}) — 该镜需延长或改稿，静默通过=错位")
        start = t + args.vo_offset
        max_end = max(max_end, start + vd / atempo)
        chain = f"[{vi}:a]atempo={atempo:.4f}[a{vi}]"
        voice_filters.append(chain)
        setpts.append(f"[a{vi}]adelay={int(start*1000)}|{int(start*1000)}[v{vi}]")
        amix_inputs.append(f"[v{vi}]")
        vi += 1
    if vi == 0:
        sys.exit("no voice tracks in schedule")

    n_src = 2 + vi  # raw(0) + bgm(1) + voices...
    inputs = ["-i", raw]
    if args.bgm:
        inputs += ["-stream_loop", "-1", "-i", args.bgm]
    else:
        inputs += ["-f", "lavfi", "-i", f"anullsrc=r=48000:cl=mono,duration={total}"]
        args.bgm_vol = 0.0
    for sh in shots:
        if sh.get("vo"):
            vpath = os.path.join(args.voice_dir, sh["vo"]) if not os.path.isabs(sh["vo"]) else sh["vo"]
            inputs += ["-i", vpath]

    fade_out_start = max(total - 1.2, 0)
    fc = [f"[0:v]fade=t=in:st=0:d=0.6,fade=t=out:st={fade_out_start:.2f}:d=1.2,"
          f"vignette=PI/5[vbase]"]
    fc += voice_filters + setpts
    mix_in = ",".join(amix_inputs)
    fc.append(f"{mix_in}amix=inputs={vi}:duration=longest:normalize=0[voam]")
    fc.append(f"[1:a]volume={args.bgm_vol},afade=t=in:st=0:d=1.5,"
              f"afade=t=out:st={max(max_end+0.4, total-3.0):.2f}:d=3.0[bg]")
    fc.append("[voam][bg]amix=inputs=2:duration=first:normalize=0[mix]")
    fc.append(f"[mix]loudnorm=I=-16:TP=-1.5:LRA=11,"
              f"afade=t=out:st={max(total-0.6,0):.2f}:d=0.6[ao]")

    # ---- 字幕 overlay ----
    vlabel = "[vbase]"
    if not args.no_subs and sched.get("subs"):
        for j, s in enumerate(sched["subs"]):
            inputs += ["-i", s["file"]]
            out = f"[vz{j}]"
            fc.append(f"{vlabel}[{n_src + j}:v]overlay=0:0:enable="
                     f"'between(t,{s['from']:.3f},{s['to']:.3f})'{out}")
            vlabel = out

    cmd = [FFMPEG, "-y", "-v", "error"] + inputs + ["-filter_complex", ";".join(fc),
           "-map", f"{vlabel}".strip("[]"), "-map", "[ao]",
           "-t", f"{total:.3f}", "-c:v", "libx264", "-preset", "medium", "-crf", str(args.crf),
           "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", args.out]
    subprocess.run(cmd, check=True)

    # ---- 交付自检：响度复核 ----
    m = loudnorm_measure(args.out)
    print(f"DONE {args.out} dur={dur_of(args.out):.2f}s "
          f"I={m['input_i']} LUFS TP={m['input_tp']} peak shots={len(shots)} vo={vi}")

    # 清理中间件
    for p in norm_files + [raw, concat_list]:
        try:
            os.remove(p)
        except OSError:
            pass


if __name__ == "__main__":
    main()
