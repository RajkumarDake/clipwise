"""Shared helpers for the clipwise scripts (stdlib only; numpy/cv2 are imported lazily).

Locations (all overridable by environment variables):
  CLIPWISE_HOME   runtime home: venv, models, fonts          default ~/.clipwise
  CLIPWISE_VENV   python virtualenv used for ASR/rendering   default $CLIPWISE_HOME/venv
  CLIPWISE_YUNET  YuNet face detector .onnx                  default $CLIPWISE_HOME/models/yunet.onnx
  CLIPWISE_WORK   where jobs are written                     default ~/clipwise-work
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
DEFAULT_HOME = "~/.clipwise"
HOME = Path(os.path.expanduser(os.environ.get("CLIPWISE_HOME") or DEFAULT_HOME))
VENV = Path(os.path.expanduser(os.environ.get("CLIPWISE_VENV") or str(HOME / "venv")))
YUNET = os.path.expanduser(os.environ.get("CLIPWISE_YUNET") or str(HOME / "models" / "yunet.onnx"))
FONTS_DIR = HOME / "fonts"
WORK_ROOT = Path(os.path.expanduser(os.environ.get("CLIPWISE_WORK") or "~/clipwise-work"))
FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"
OUT_W, OUT_H, OUT_FPS = 1080, 1920, 30
UI_SAFE_Y = 1620  # Shorts/Reels/TikTok UI (title, buttons) covers roughly the bottom 300 px

SETUP_HINT = (f"Run the one-time setup first:  bash {SKILL_DIR / 'setup.sh'}\n"
              f"(it creates {VENV} and downloads the face model). "
              "Set CLIPWISE_HOME / CLIPWISE_VENV if you installed elsewhere.")


def log(*a):
    print("[clipwise]", *a, file=sys.stderr, flush=True)


def venv_python():
    for c in (VENV / "bin" / "python", VENV / "Scripts" / "python.exe"):
        if c.exists():
            return str(c)
    return None


def ensure_venv(modules=("numpy",)):
    """Re-exec the calling script under the clipwise venv when we are not already in it.

    Falls back to the current interpreter only if it already has every module in `modules`;
    otherwise exits with a clear 'run setup.sh' message.
    """
    vp = venv_python()
    in_venv = Path(sys.prefix).resolve() == VENV.resolve()
    if vp and not in_venv and not os.environ.get("CLIPWISE_NO_REEXEC"):
        os.environ["CLIPWISE_NO_REEXEC"] = "1"  # never loop
        os.execv(vp, [vp, os.path.abspath(sys.argv[0]), *sys.argv[1:]])
    missing = [m for m in modules if importlib.util.find_spec(m) is None]
    if missing:
        raise SystemExit(f"clipwise: python modules missing ({', '.join(missing)}) and no usable venv at {VENV}.\n"
                         + SETUP_HINT)


def need_ffmpeg():
    if not shutil.which(FFMPEG) or not shutil.which(FFPROBE):
        raise SystemExit("clipwise: ffmpeg/ffprobe not found on PATH. " + SETUP_HINT)


def need_yunet():
    if not os.path.exists(YUNET):
        raise SystemExit(f"clipwise: face model missing at {YUNET}. " + SETUP_HINT)


def run(cmd, capture=False, check=True, quiet=True):
    """Run a command list. Returns CompletedProcess. Raises SystemExit with stderr tail on failure."""
    if quiet and cmd and os.path.basename(str(cmd[0])) in ("ffmpeg", "ffmpeg.exe") and "-hide_banner" not in cmd:
        cmd = [cmd[0], "-hide_banner", "-nostdin"] + list(cmd[1:])
    p = subprocess.run([str(c) for c in cmd], capture_output=True, text=True)
    if check and p.returncode != 0:
        tail = (p.stderr or "")[-2500:]
        raise SystemExit(f"command failed ({p.returncode}): {' '.join(map(str, cmd))[:400]}\n{tail}")
    return p


def _float(s):
    s = (s or "").strip().strip(",").split(",")[0].strip()
    return float(s) if s and s != "N/A" else None


def probe(path):
    p = run([FFPROBE, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)], capture=True)
    return json.loads(p.stdout)


def stream_duration(path, sel):
    """Duration of the first stream of a kind ('v:0'/'a:0'). Falls back to the last packet."""
    p = run([FFPROBE, "-v", "error", "-select_streams", sel, "-show_entries", "stream=duration",
             "-of", "csv=p=0", str(path)], capture=True)
    d = _float(p.stdout)
    if d is None:  # mkv/webm often has no per-stream duration
        p = run([FFPROBE, "-v", "error", "-select_streams", sel, "-show_entries", "packet=pts_time,duration_time",
                 "-of", "csv=p=0", str(path)], capture=True)
        last = 0.0
        for line in p.stdout.splitlines():
            parts = [x for x in line.strip().strip(",").split(",") if x and x != "N/A"]
            if parts:
                try:
                    last = max(last, float(parts[0]) + (float(parts[1]) if len(parts) > 1 else 0))
                except ValueError:
                    pass
        d = last
    return d


def assert_av(path, tol=0.25, label=""):
    vd = stream_duration(path, "v:0")
    ad = stream_duration(path, "a:0")
    if vd is None or ad is None or abs(vd - ad) > tol:
        raise SystemExit(f"A/V DESYNC {label}: video {vd}s vs audio {ad}s in {path}")
    return vd, ad


def summarize(path):
    d = probe(path)
    v = next((s for s in d["streams"] if s["codec_type"] == "video"), None)
    a = next((s for s in d["streams"] if s["codec_type"] == "audio"), None)
    out = {"path": str(path), "duration": _float(d["format"].get("duration"))}
    if v:
        w, h = int(v["width"]), int(v["height"])
        rot = 0
        for sd in v.get("side_data_list", []) or []:
            if "rotation" in sd:
                rot = int(sd["rotation"])
        if abs(rot) in (90, 270):
            w, h = h, w
        num, den = (v.get("avg_frame_rate") or "0/1").split("/")
        rnum, rden = (v.get("r_frame_rate") or "0/1").split("/")
        fps = float(num) / float(den) if float(den) else 0.0
        rfps = float(rnum) / float(rden) if float(rden) else 0.0
        trc = v.get("color_transfer") or ""
        out.update(width=w, height=h, rotation=rot, fps=round(fps, 3), r_fps=round(rfps, 3),
                   vcodec=v.get("codec_name"), pix_fmt=v.get("pix_fmt"),
                   hdr=trc in ("arib-std-b67", "smpte2084") or (v.get("color_primaries") == "bt2020"),
                   color_transfer=trc, color_primaries=v.get("color_primaries"),
                   vfr_suspected=bool(fps and rfps and abs(fps - rfps) > 0.005))
    if a:
        out.update(acodec=a.get("codec_name"), channels=a.get("channels"), sample_rate=a.get("sample_rate"))
    out["n_data_streams"] = sum(1 for s in d["streams"] if s["codec_type"] not in ("video", "audio"))
    return out


def loudness(path, start=None, dur=None):
    """Integrated loudness / true peak via loudnorm print_format=json (first audio stream)."""
    cmd = [FFMPEG]
    if start is not None:
        cmd += ["-ss", f"{start:.3f}"]
    if dur is not None:
        cmd += ["-t", f"{dur:.3f}"]
    cmd += ["-i", str(path), "-map", "0:a:0", "-af", "loudnorm=I=-14:TP=-1.5:LRA=11:print_format=json",
            "-f", "null", "-"]
    p = run(cmd, capture=True)
    txt = p.stderr
    return json.loads(txt[txt.rindex("{"): txt.rindex("}") + 1])


def fmt_ts(t):
    m, s = divmod(max(0.0, t), 60)
    return f"{int(m):d}:{s:05.2f}"


def load_json(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def save_json(p, obj):
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, ensure_ascii=False)
