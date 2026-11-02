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

"""silence_squash — 语音静音压缩：把句内长停顿压到固定间隙，语音段零损失拼接。

用途：TTS 干声常带 0.3~1s 的过度停顿，直接入轨会拖垮旁白-画面排期。
本脚本用 silencedetect 找出静音区，语音段之间改插固定短间隙。
注意：多数 TTS 输出为 float32 WAV，先由 ffmpeg 转 pcm_s16 再处理。

用法：
    python3 silence_squash.py input.wav output.wav [--gap 0.12] [--noise -30dB] [--mindur 0.12]
"""
import argparse
import os
import re
import subprocess
import sys
import tempfile
import wave

try:
    import numpy as np
except ImportError:
    sys.exit("numpy required")

FFMPEG = os.environ.get("MYVIDEO_FFMPEG", "ffmpeg")


def probe_dur(path: str) -> float:
    out = subprocess.check_output(
        [FFMPEG.replace("ffmpeg", "ffprobe"), "-v", "error",
         "-show_entries", "format=duration", "-of", "csv=p=0", path])
    return float(out.strip())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--gap", type=float, default=0.12, help="fixed inter-speech gap seconds")
    ap.add_argument("--noise", default="-30dB", help="silencedetect noise threshold")
    ap.add_argument("--mindur", type=float, default=0.12, help="min silence duration to count")
    ap.add_argument("--rate", type=int, default=24000)
    args = ap.parse_args()

    det = subprocess.run(
        [FFMPEG, "-v", "info", "-i", args.src,
         "-af", f"silencedetect=noise={args.noise}:d={args.mindur}", "-f", "null", "-"],
        capture_output=True, text=True).stderr
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", det)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", det)]

    fd, pcm = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        subprocess.run([FFMPEG, "-y", "-v", "error", "-i", args.src,
                        "-ar", str(args.rate), "-ac", "1", "-c:a", "pcm_s16le", pcm], check=True)
        with wave.open(pcm) as w:
            sr = w.getframerate()
            raw = w.readframes(w.getnframes())
    finally:
        os.remove(pcm)

    audio = np.frombuffer(raw, dtype=np.int16)
    dur = len(audio) / sr
    silences = [(max(s, 0.0), min(e, dur)) for s, e in zip(starts, ends)]

    voiced, cursor = [], 0.0
    for s, e in silences:
        if s - cursor > 0.05:
            voiced.append((cursor, s))
        cursor = max(cursor, e)
    if dur - cursor > 0.05:
        voiced.append((cursor, dur))
    if not voiced:
        sys.exit("no voiced segment found")

    gap = np.zeros(int(args.gap * sr), dtype=np.int16)
    parts = []
    for s, e in voiced:
        a, b = int(s * sr), min(int(e * sr), len(audio))
        if b - a > int(0.05 * sr):
            parts.append(audio[a:b])
    out = parts[0]
    for p in parts[1:]:
        out = np.concatenate([out, gap, p])

    os.makedirs(os.path.dirname(os.path.abspath(args.dst)), exist_ok=True)
    with wave.open(args.dst, "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(out.tobytes())
    print(f"{os.path.basename(args.dst)}: {len(out)/sr:.2f}s (was {dur:.2f}s)")


if __name__ == "__main__":
    main()
