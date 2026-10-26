#!/usr/bin/env python3
"""run.py - two-stage wrapper around fetch/transcribe/render. Jobs live in $CLIPWISE_WORK/<job>/
(default ~/clipwise-work/<job>/), or wherever --workdir points.

  run.py check
      verify the install (ffmpeg + libass, venv, python modules, face model, caption font)
  run.py prep  INPUT --job NAME [--model large-v3-turbo] [--cookies c.txt] [--start S] [--max-seconds N]
      fetch -> source.mp4, transcribe -> transcript.txt/json, words.tsv. Then the AGENT reads
      transcript.txt, writes picks.json with references/viral-rubric.md, and gets the user's OK.
  run.py render --job NAME [--picks picks.json] [render.py flags, e.g. --fast --only 2 --music bed.mp3]
      render every pick -> out/clip_<id>.mp4 + .txt + .json + _sheet.png
  run.py clean --job NAME
      delete source.mp4 and tmp dirs, keep out/ and the json/txt files
"""
import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from _common import FFMPEG, FONTS_DIR, HOME, SETUP_HINT, VENV, WORK_ROOT, YUNET, venv_python  # noqa: E402


def sh(cmd):
    print("+", " ".join(map(str, cmd)), flush=True)
    t = time.time()
    r = subprocess.run([str(c) for c in cmd])
    if r.returncode != 0:
        sys.exit(r.returncode)
    return time.time() - t


def check():
    ok = True

    def line(good, what, hint=""):
        nonlocal ok
        ok &= bool(good)
        print(f"  [{'ok' if good else 'MISSING'}] {what}" + ("" if good or not hint else f"  -> {hint}"))

    print(f"CLIPWISE_HOME={HOME}")
    ff = shutil.which(FFMPEG)
    line(ff, f"ffmpeg ({ff})", "install ffmpeg (see setup.sh output)")
    if ff:
        filt = subprocess.run([ff, "-hide_banner", "-filters"], capture_output=True, text=True).stdout
        line(" ass " in filt, "ffmpeg built with libass (captions)", "install an ffmpeg build with libass")
        line("sidechaincompress" in filt and "loudnorm" in filt, "ffmpeg audio filters (loudnorm, sidechaincompress)")
    vp = venv_python()
    line(vp, f"venv python ({VENV})", "bash setup.sh")
    if vp:
        p = subprocess.run([vp, "-c", "import numpy, cv2, faster_whisper; cv2.FaceDetectorYN; "
                                      "print(cv2.__version__, faster_whisper.__version__)"],
                           capture_output=True, text=True)
        line(p.returncode == 0, "numpy, opencv (FaceDetectorYN), faster-whisper " + p.stdout.strip(),
             (p.stderr.strip().splitlines() or [""])[-1])
    line(os.path.exists(YUNET), f"YuNet face model ({YUNET})", "bash setup.sh")
    font = (FONTS_DIR / "Lato-Black.ttf").exists()
    if not font and shutil.which("fc-list"):
        font = "lato black" in subprocess.run(["fc-list", ":", "family"], capture_output=True,
                                              text=True).stdout.lower()
    print(f"  [{'ok' if font else 'warn'}] caption font Lato Black"
          + ("" if font else " (optional; falls back to DejaVu Sans)"))
    print("ready" if ok else "NOT READY. " + SETUP_HINT)
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("stage", choices=["check", "prep", "render", "clean"])
    if len(sys.argv) > 1 and sys.argv[1] == "prep":  # only prep takes INPUT; render passes the rest through
        ap.add_argument("input", nargs="?")
    ap.add_argument("--job", help="job name (folder under $CLIPWISE_WORK)")
    ap.add_argument("--workdir", help="explicit job folder (overrides --job)")
    ap.add_argument("--model", default=os.environ.get("CLIPWISE_MODEL", "large-v3-turbo"))
    ap.add_argument("--language")
    ap.add_argument("--cookies")
    ap.add_argument("--start", type=float)
    ap.add_argument("--max-seconds", type=float)
    ap.add_argument("--picks")
    args, extra = ap.parse_known_args()
    if args.stage == "check":
        sys.exit(check())
    if not (args.workdir or args.job):
        sys.exit("need --job NAME or --workdir DIR")
    wd = Path(os.path.expanduser(args.workdir)) if args.workdir else WORK_ROOT / args.job
    wd.mkdir(parents=True, exist_ok=True)
    py = sys.executable
    if args.stage == "prep":
        if not getattr(args, "input", None):
            sys.exit("prep needs INPUT (file path or link)")
        cmd = [py, HERE / "fetch.py", args.input, wd]
        if args.cookies:
            cmd += ["--cookies", args.cookies]
        if args.start:
            cmd += ["--start", args.start]
        if args.max_seconds:
            cmd += ["--max-seconds", args.max_seconds]
        tf = sh(cmd)
        tcmd = [py, HERE / "transcribe.py", wd, "--model", args.model]
        if args.language:
            tcmd += ["--language", args.language]
        ta = sh(tcmd)
        print(f"\nprep done: fetch {tf:.0f}s, transcribe {ta:.0f}s.\nNext: read {wd}/transcript.txt, write "
              f"{wd}/picks.json (references/viral-rubric.md), show the user the list, then:\n"
              f"  python3 {HERE / 'run.py'} render --workdir {wd}")
    elif args.stage == "render":
        picks = args.picks or str(wd / "picks.json")
        tr = sh([py, HERE / "render.py", wd, picks, *extra])
        print(f"\nrender done in {tr:.0f}s -> {wd}/out/  (look at every *_sheet.png before delivering)")
    else:
        for p in [wd / "source.mp4", *wd.glob("tmp_*")]:
            if p.is_dir():
                shutil.rmtree(p)
            elif p.exists():
                p.unlink()
        print("cleaned", wd)


if __name__ == "__main__":
    main()
