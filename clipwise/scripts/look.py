#!/usr/bin/env python3
"""look.py CLIP.mp4 [-o SHEET.png] [--tiles 5x2] -> contact sheet PNG

Frames sampled evenly across the clip, each stamped with its timecode, with a red
line at y=1620 (the Shorts/Reels/TikTok UI covers everything below it) so caption
safety is visible, not arithmetic. Open the PNG (vision) and check: face in frame,
captions readable, captions not on the face, nothing important under the red line.
"""
import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import FFMPEG, UI_SAFE_Y, need_ffmpeg, run, summarize  # noqa: E402


def bold_font_file():
    if shutil.which("fc-match"):
        p = subprocess.run(["fc-match", "-f", "%{file}", "DejaVu Sans:bold"], capture_output=True, text=True)
        if p.returncode == 0 and os.path.exists(p.stdout.strip()):
            return p.stdout.strip()
    for c in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/Library/Fonts/Arial Bold.ttf",
              "/System/Library/Fonts/Supplemental/Arial Bold.ttf", "C:/Windows/Fonts/arialbd.ttf"):
        if os.path.exists(c):
            return c
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input")
    ap.add_argument("-o", "--output")
    ap.add_argument("--tiles", default="5x2")
    ap.add_argument("--tile-width", type=int, default=360)
    args = ap.parse_args()
    need_ffmpeg()
    meta = summarize(args.input)
    cols, rows = (int(x) for x in args.tiles.split("x"))
    n = cols * rows
    dur = meta["duration"]
    out = args.output or str(Path(args.input).with_name(Path(args.input).stem + "_sheet.png"))
    h = meta["height"]
    line_y = int(UI_SAFE_Y * h / 1920)
    font = bold_font_file()
    stamp = ""
    if font:
        ff = font.replace("\\", "/").replace(":", "\\:")
        stamp = (f"drawtext=fontfile='{ff}':text='%{{pts\\:hms}}':x=12:y=12:fontsize=h/24:fontcolor=white:"
                 "box=1:boxcolor=black@0.6,")
    nf = max(n, int(round(dur * (meta.get("fps") or 30))))
    step = max(1, nf // n)  # pick frames step/2, 3*step/2, ... so exactly n tiles get filled
    vf = (f"select='not(mod(n+{step - step // 2},{step}))',drawbox=x=0:y={line_y}:w=iw:h={max(3, h // 320)}:color=red@0.9:t=fill,{stamp}"
          f"scale={args.tile_width}:-2,tile={cols}x{rows}:padding=4:margin=4:color=0x202020")
    run([FFMPEG, "-y", "-i", args.input, "-vf", vf, "-fps_mode", "vfr", "-frames:v", "1", "-update", "1", out])
    print(out)


if __name__ == "__main__":
    main()
