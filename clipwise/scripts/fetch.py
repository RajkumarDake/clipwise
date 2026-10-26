#!/usr/bin/env python3
"""fetch.py INPUT WORKDIR -> WORKDIR/source.mp4 (+ probe.json)

INPUT is a local file, a public Google Drive / Dropbox share link, a direct http(s)
media URL, or anything yt-dlp supports (YouTube, Vimeo, X, ...). The original is
never modified.

The result is always a clean, editable file: first video + first audio stream only
(phone data/metadata tracks dropped), SDR (HDR/HLG/Dolby Vision tone-mapped),
CFR 30 fps when it had to be re-encoded (VFR phone footage), short GOP for fast,
accurate seeks, AAC 48 kHz.

  fetch.py talk.mov ~/clipwise-work/job1
  fetch.py "https://drive.google.com/file/d/ID/view" ~/clipwise-work/job2
  fetch.py "https://youtu.be/XXXX" ~/clipwise-work/job3 --cookies cookies.txt
  fetch.py big.mp4 ~/clipwise-work/job4 --start 60 --max-seconds 300   # only that window

Exit codes: 0 ok, 2 bad input / private link, 3 YouTube bot wall (needs cookies or the file).
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import FFMPEG, VENV, log, need_ffmpeg, run, save_json, summarize  # noqa: E402

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
BOT_WALL = ("Sign in to confirm", "not a bot", "confirm you're not a bot", "cookies-from-browser")
MEDIA_EXT = r"\.(mp4|mov|m4v|webm|mkv|avi|ogv|mpg|mpeg)(\?|#|$)"


def ytdlp_bin():
    for c in (VENV / "bin" / "yt-dlp", VENV / "Scripts" / "yt-dlp.exe"):
        if c.exists():
            return str(c)
    return shutil.which("yt-dlp") or next((c for c in (os.path.expanduser("~/.local/bin/yt-dlp"),)
                                            if os.path.exists(c)), None)


def drive_id(url):
    m = re.search(r"/file/d/([A-Za-z0-9_-]{10,})", url) or re.search(r"[?&]id=([A-Za-z0-9_-]{10,})", url)
    return m.group(1) if m else None


def http_download(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        ctype = r.headers.get("Content-Type", "")
        if "text/html" in ctype:
            return False, ctype
        size = int(r.headers.get("Content-Length") or 0)
        log(f"downloading {size / 1e6:.0f} MB" if size else "downloading", "->", dest)
        with open(dest, "wb") as f:
            shutil.copyfileobj(r, f, length=1 << 20)
    return True, ctype


def ytdlp_download(url, workdir, cookies):
    exe = ytdlp_bin()
    if not exe:
        raise SystemExit("yt-dlp not found. Run setup.sh (installs it into the clipwise venv) "
                         "or pass a local file / direct media URL.")
    out_tmpl = str(Path(workdir) / "download.%(ext)s")
    cmd = [exe, "--no-playlist", "-f", "bv*[height<=1080]+ba/b[height<=1080]/b", "--merge-output-format", "mp4",
           "-o", out_tmpl, url]
    if cookies:
        cmd[1:1] = ["--cookies", cookies]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        err = p.stderr or ""
        if any(s in err for s in BOT_WALL):
            print("\nYOUTUBE BOT WALL: YouTube refused the download from this machine/IP "
                  "(\"Sign in to confirm you're not a bot\"). It is intermittent.\n"
                  "Ask the user to either (a) send the video file itself (or a Drive/Dropbox link), or\n"
                  "(b) export cookies.txt from a logged-in browser and re-run with  --cookies cookies.txt\n"
                  "Do not loop over player clients or proxies.", file=sys.stderr)
            sys.exit(3)
        raise SystemExit("yt-dlp failed:\n" + err[-2000:])
    files = sorted(Path(workdir).glob("download.*"), key=lambda p: p.stat().st_size, reverse=True)
    files = [f for f in files if f.suffix not in (".part", ".ytdl", ".json")]
    if not files:
        raise SystemExit("yt-dlp reported success but no file was written")
    return str(files[0])


def obtain(inp, workdir, cookies):
    if os.path.exists(os.path.expanduser(inp)):
        return os.path.abspath(os.path.expanduser(inp)), False
    if not re.match(r"https?://", inp):
        print(f"input not found and not a URL: {inp}", file=sys.stderr)
        sys.exit(2)
    did = drive_id(inp) if "drive.google.com" in inp or "docs.google.com" in inp else None
    if did:
        dest = str(Path(workdir) / "download.bin")
        ok, _ = http_download(f"https://drive.usercontent.google.com/download?id={did}&export=download&confirm=t",
                              dest)
        if not ok:
            print("Google Drive returned an HTML page: the file is private or needs a login. Ask the user to "
                  "share it as 'Anyone with the link', or download it and pass the local path.", file=sys.stderr)
            sys.exit(2)
        return dest, True
    if "dropbox.com" in inp:
        inp = re.sub(r"([?&])dl=0", r"\1dl=1", inp)
        if "dl=1" not in inp:
            inp += ("&" if "?" in inp else "?") + "dl=1"
        dest = str(Path(workdir) / "download.bin")
        ok, _ = http_download(inp, dest)
        if ok:
            return dest, True
    if re.search(MEDIA_EXT, inp, re.I):
        dest = str(Path(workdir) / ("download" + Path(inp.split("?")[0].split("#")[0]).suffix))
        ok, _ = http_download(inp, dest)
        if ok:
            return dest, True
    return ytdlp_download(inp, workdir, cookies), True


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input")
    ap.add_argument("workdir")
    ap.add_argument("--cookies", help="cookies.txt for yt-dlp (YouTube bot wall)")
    ap.add_argument("--start", type=float, default=0.0, help="keep from this second")
    ap.add_argument("--max-seconds", type=float, help="keep at most this many seconds (saves ASR/render time)")
    ap.add_argument("--force-encode", action="store_true",
                    help="always re-encode (default: stream-copy files that are already clean H.264/AAC SDR CFR)")
    args = ap.parse_args()
    need_ffmpeg()

    t0 = time.time()
    wd = Path(os.path.expanduser(args.workdir))
    wd.mkdir(parents=True, exist_ok=True)
    src, downloaded = obtain(args.input, wd, args.cookies)
    info = summarize(src)
    if "width" not in info:
        raise SystemExit(f"no video stream in {src}")
    if "acodec" not in info:
        raise SystemExit(f"no audio stream in {src} (the Shorts pipeline needs speech)")
    log("input:", {k: info.get(k) for k in ("duration", "width", "height", "fps", "r_fps", "vcodec", "pix_fmt",
                                            "hdr", "vfr_suspected", "acodec", "channels", "n_data_streams")})

    out = wd / "source.mp4"
    # stream copy only when nothing needs fixing; a non-zero --start forces a re-encode because
    # copy-seeking snaps video to a keyframe and would put audio and video out of step.
    clean = (info.get("vcodec") == "h264" and not info.get("hdr") and not info.get("vfr_suspected")
             and info.get("pix_fmt") == "yuv420p" and not info.get("rotation") and info.get("acodec") == "aac"
             and not args.start and not args.force_encode)
    cmd = [FFMPEG, "-y"]
    if args.start:
        cmd += ["-ss", f"{args.start:.3f}"]
    cmd += ["-i", src]
    if args.max_seconds:
        cmd += ["-t", f"{args.max_seconds:.3f}"]
    cmd += ["-map", "0:v:0", "-map", "0:a:0", "-dn", "-sn", "-map_metadata", "-1"]
    if clean:
        cmd += ["-c", "copy"]
        mode = "stream-copy"
    else:
        vf = []
        if info.get("hdr"):
            tin = info.get("color_transfer") or "arib-std-b67"
            pin = info.get("color_primaries") or "bt2020"
            vf.append(f"zscale=tin={tin}:pin={pin}:min=bt2020nc:t=linear:npl=203,format=gbrpf32le,"
                      "zscale=p=bt709,tonemap=tonemap=mobius:desat=0,zscale=t=bt709:m=bt709:r=tv")
        vf.append("fps=30")
        vf.append("scale=trunc(iw/2)*2:trunc(ih/2)*2")
        vf.append("format=yuv420p")
        cmd += ["-vf", ",".join(vf), "-fps_mode", "cfr", "-c:v", "libx264", "-preset", "veryfast", "-crf", "17",
                "-g", "30", "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
                "-c:a", "aac", "-b:a", "192k", "-ar", "48000"]
        why = [k for k in ("hdr", "vfr_suspected") if info.get(k)]
        if info.get("vcodec") != "h264" or info.get("acodec") != "aac":
            why.append(f"codec {info.get('vcodec')}/{info.get('acodec')}")
        if info.get("rotation"):
            why.append(f"rotation {info['rotation']}")
        if args.start:
            why.append("trim start")
        mode = "re-encode (" + ", ".join(why or ["forced/pix_fmt"]) + ")"
    cmd += ["-movflags", "+faststart", str(out)]
    log("conform:", mode)
    run(cmd)
    if downloaded and Path(src).parent == wd:
        os.remove(src)  # source.mp4 is the working copy; keep disk use down
    res = summarize(out)
    res["origin"] = args.input
    res["conform"] = mode
    res["fetch_wall_s"] = round(time.time() - t0, 1)
    save_json(wd / "probe.json", res)
    print(f"source.mp4: {res['duration']:.2f}s {res['width']}x{res['height']} {res['fps']}fps "
          f"{res['vcodec']} / {res.get('acodec')} {res.get('channels')}ch  [{mode}, {res['fetch_wall_s']}s]")
    print(str(out))


if __name__ == "__main__":
    main()
