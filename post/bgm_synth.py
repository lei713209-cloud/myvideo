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

"""bgm_synth — 零依赖（numpy）合成免版权钢琴垫 BGM。

用途：口播/科普短视频的背景音乐床。纯合成 = 无版权风险、零下载。
结构为通用叙事弧：sparse pad -> 柱式和声 -> 琶音推进 -> 解决收束淡出。
和弦进行、时长、段落切点全部可参数化，默认走经典 Am-F-C-G。

用法：
    python3 bgm_synth.py --out bgm.wav --duration 75 \
        --progression Am7,Fmaj7,Cmaj7,Gadd9 --arc 8,24,65
arc = 三个切点秒数：intro结束 / 推进段开始 / 解决段开始
"""
import argparse
import os
import wave

import numpy as np

SR = 48000

CHORDS = {
    "Am7":   [45, 57, 60, 64, 67, 72],
    "Fmaj7": [41, 57, 60, 65, 69, 72],
    "Cmaj7": [48, 55, 64, 67, 71, 76],
    "Gadd9": [43, 55, 59, 62, 66, 74],
    "Dm7":   [50, 57, 62, 65, 69, 74],
    "Em7":   [52, 59, 64, 67, 71, 76],
    "Fmaj":  [41, 57, 60, 65, 69],
    "G":     [43, 55, 59, 62, 67],
    "C":     [48, 55, 64, 67, 72, 76],
    "Am":    [45, 57, 60, 64, 69],
}


def midi_hz(m: float) -> float:
    return 440.0 * 2 ** ((m - 69) / 12.0)


def piano(midi: float, dur: float, amp: float, tau: float = 2.6) -> np.ndarray:
    n = int(SR * dur)
    t = np.arange(n) / SR
    f = midi_hz(midi)
    y = np.zeros(n)
    for h, a in zip((1, 2, 3, 4, 5, 6), (1, .42, .22, .10, .05, .03)):
        for detune in (0.9983, 1.0017):   # 双声微 detune 增宽度
            y += a * np.sin(2 * np.pi * f * h * detune * t + h)
    env = np.exp(-t / tau) * np.minimum(t / 0.008, 1.0)
    return y * env * amp / 3.0


def pad(t0: float, t1: float, chord: str, amp: float = 0.30) -> np.ndarray:
    n = int(SR * (t1 - t0))
    t = np.arange(n) / SR
    y = np.zeros(n)
    for m in CHORDS[chord]:
        trem = 1 + 0.12 * np.sin(2 * np.pi * 0.18 * t + m)
        y += np.sin(2 * np.pi * midi_hz(m) * t) * trem
    return y * (np.hanning(n) ** 1.5) * amp / len(CHORDS[chord])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--duration", type=float, default=75.0)
    ap.add_argument("--progression", default="Am7,Fmaj7,Cmaj7,Gadd9")
    ap.add_argument("--arc", default="", help="three cut seconds: intro_end, motion_start, resolve_start")
    args = ap.parse_args()

    total = args.duration
    prog = [c for c in args.progression.split(",") if c in CHORDS] or ["Am7", "Fmaj7", "Cmaj7", "Gadd9"]
    cuts = [float(x) for x in args.arc.split(",")] if args.arc else \
        [round(total * 0.11, 1), round(total * 0.32, 1), round(total * 0.86, 1)]
    intro_end, motion_start, resolve_start = cuts

    buf = np.zeros(int(SR * total))

    def mix_at(sig: np.ndarray, t0: float) -> None:
        i = int(t0 * SR)
        j = min(len(buf), i + len(sig))
        if j > i:
            buf[i:j] += sig[: j - i]

    # 1) intro：稀疏 pad + 零星高音点缀
    mix_at(pad(0, intro_end + 0.4, prog[0], 0.34), 0)
    mix_at(piano(CHORDS[prog[0]][-1], 4.0, 0.10), 1.5)
    # 2) 讲解段：4s 一换的和弦柱式 + pad
    t = intro_end
    i = 0
    while t < motion_start - 0.01:
        ch = prog[i % len(prog)]
        mix_at(pad(0, 4.6, ch, 0.26), t)
        for m in CHORDS[ch][1:]:
            mix_at(piano(m, 3.4, 0.085), t + 0.05)
        t += 4.0
        i += 1
    # 3) 推进段：八分琶音 + 旋律锚点
    t = motion_start
    i = 0
    while t < resolve_start - 0.01:
        ch = prog[i % len(prog)]
        mix_at(pad(0, 4.6, ch, 0.22), t)
        notes = CHORDS[ch][1:]
        k = 0
        while t + k * 0.5 < min(t + 4.0, resolve_start):
            mix_at(piano(notes[k % len(notes)], 1.6, 0.075, tau=1.1), t + k * 0.5)
            k += 1
        mix_at(piano(notes[(i * 2) % len(notes)] + 12, 3.0, 0.06, tau=2.0), t + 1.0)
        t += 4.0
        i += 1
    # 4) 解决段：落回主和弦，渐停淡出
    tail = total - resolve_start
    mix_at(pad(0, tail + 0.4, prog[0], 0.30), resolve_start)
    mix_at(piano(CHORDS[prog[0]][-1] - 12, min(3.0, tail), 0.10), resolve_start + 1.0)

    n_in, n_out = int(0.4 * SR), int(1.0 * SR)
    buf[:n_in] *= np.linspace(0, 1, n_in)
    buf[-n_out:] *= np.linspace(1, 0, n_out) ** 1.2
    peak = np.max(np.abs(buf)) or 1.0
    buf = buf / peak * 0.55   # 峰值约 -5.2 dBFS；入混音时通常还要再压低

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with wave.open(args.out, "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((buf * 32767).astype(np.int16).tobytes())
    print("bgm:", args.out, f"{total:.1f}s")


if __name__ == "__main__":
    main()
