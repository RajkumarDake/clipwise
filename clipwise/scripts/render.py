#!/usr/bin/env python3
"""render.py WORKDIR PICKS.json [--only ID] [--fast] [--music bed.mp3] -> WORKDIR/out/clip_<id>.mp4
(+ clip_<id>.txt, clip_<id>.json, clip_<id>_cutmap.json, clip_<id>_sheet.png)

Per pick (see references/viral-rubric.md for the picks.json format):
  1. spans: optional hook (hook_start..hook_end) moved to the front, then the body
     (start..end, with the hook range removed when it lies inside it).
  2. silence removal inside each span: silencedetect threshold derived from THIS clip's
     own level (p95 speech RMS / noise floor), gaps > 0.35 s cut leaving ~0.12 s breath.
     Kept intervals are snapped to the 30 fps grid so audio length == frames/30 exactly.
     -> cutmap (src_start/src_end/out_start/out_end) + re-timed word timestamps.
  3. audio: atrim+concat per kept interval (asserted vs. frame count), pre-gain to -20 LUFS
     from the clip's measured loudness, highpass + afftdn + compressor, optional music bed
     (--music, user-supplied) ducked with sidechaincompress, synthesised SFX (sfx.py, or your
     own files via --sfx-dir): pop on a moved hook, whoosh on hook->body and a few jump cuts,
     one low thud on the payoff line, then two-pass loudnorm I=-14 TP=-1.5.
  4. face tracking: YuNet on downscaled frames at ~5 fps, dominant face with sticky
     switching, interpolation between samples, hysteresis dead-zone + EMA camera,
     snaps allowed only on cuts. 16:9 -> 9:16 crop window; vertical sources are just scaled.
     Low face coverage on a wide source -> blurred-background pad layout.
  5. punch-in zoom alternating 1.0 / 1.12 every 2-4 s, preferring cut points then
     sentence ends; zoom anchors on the face so it stays put on screen.
  6. word-by-word ASS captions (Lato Black, active word yellow, dark outline + shadow,
     <=4 words / <=1.9 s groups held until the next group, no blank gaps), placed below the
     tracked chin and above y=1620 (Shorts UI).
  7. frames piped decoder -> numpy/cv2 warpAffine -> x264 (ass burned in) + mixed audio.
     H.264 yuv420p CRF 20, AAC 48 kHz stereo, A/V durations asserted.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import ensure_venv  # noqa: E402

ensure_venv(("numpy", "cv2"))

import argparse  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import math  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")
import numpy as np  # noqa: E402

import sfx as sfxsynth  # noqa: E402
from _common import (FFMPEG, FONTS_DIR, OUT_FPS, OUT_H, OUT_W, UI_SAFE_Y, YUNET, assert_av, fmt_ts,  # noqa: E402
                     load_json, log, loudness, need_ffmpeg, need_yunet, run, save_json, stream_duration,
                     summarize)

FPS = OUT_FPS
ZOOM_IN = 1.12
VOICE_REF_LUFS = -20.0


# ----------------------------------------------------------------------------- audio helpers
def decode_audio(src, a, d, sr=16000):
    p = subprocess.run([FFMPEG, "-v", "error", "-ss", f"{a:.3f}", "-t", f"{d:.3f}", "-i", str(src), "-map", "0:a:0",
                        "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"], capture_output=True)
    return np.frombuffer(p.stdout, dtype=np.float32)


def tuned_threshold(src, a, b):
    x = decode_audio(src, a, b - a)
    hop = 320  # 20 ms @16k
    n = len(x) // hop
    if n < 10:
        return -35.0, None, None
    rms = np.sqrt(np.mean(x[: n * hop].reshape(n, hop) ** 2, axis=1) + 1e-12)
    db = 20 * np.log10(rms + 1e-9)
    speech, floor = float(np.percentile(db, 95)), float(np.percentile(db, 10))
    # midpoint between room floor and loud speech, kept 8-18 dB under speech. On a quiet phone take
    # (p95 -22, floor -45) this gives ~-34 dB; a fixed -40 dB finds nothing on such footage.
    thr = min(speech - 8.0, max(speech - 18.0, (floor + speech) / 2))
    return max(thr, -65.0), speech, floor


def silences(src, a, b, thr, min_d=0.35):
    p = run([FFMPEG, "-ss", f"{a:.3f}", "-t", f"{b - a:.3f}", "-i", src, "-map", "0:a:0",
             "-af", f"silencedetect=noise={thr:.1f}dB:d={min_d}", "-f", "null", "-"], capture=True)
    out, cur = [], None
    for line in p.stderr.splitlines():
        m = re.search(r"silence_start: (-?[\d.]+)", line)
        if m:
            cur = max(0.0, float(m.group(1)))
        m = re.search(r"silence_end: (-?[\d.]+)", line)
        if m and cur is not None:
            out.append((cur, float(m.group(1))))
            cur = None
    if cur is not None:
        out.append((cur, b - a))
    return out


def keep_intervals(src, a, b, cut_silence=True):
    """Kept speech intervals (absolute src seconds) of span [a,b] + diagnostics."""
    if not cut_silence:
        return [(a, b)], {"threshold_db": None, "silences": 0}
    thr, speech, floor = tuned_threshold(src, a, b)
    sil = silences(src, a, b, thr)
    d = b - a
    removed = []
    for s, e in sil:
        if e - s <= 0.35:
            continue
        if s <= 0.02:
            removed.append((0.0, max(0.0, e - 0.08)))
        elif e >= d - 0.02:
            removed.append((min(d, s + 0.10), d))
        else:
            removed.append((s + 0.07, e - 0.05))
    kept, t = [], 0.0
    for s, e in removed:
        if s - t > 0.12:
            kept.append((a + t, a + s))
        t = max(t, e)
    if d - t > 0.12:
        kept.append((a + t, a + d))
    if not kept:
        kept = [(a, b)]
    return kept, {"threshold_db": round(thr, 1), "speech_p95_db": speech and round(speech, 1),
                  "floor_p10_db": floor and round(floor, 1), "silences": len(removed),
                  "removed_s": round(sum(e - s for s, e in removed), 2)}


SFX_EXT = (".wav", ".mp3", ".flac", ".ogg", ".m4a")


def sfx_info(name, cache, sfx_dir, tmp):
    """Cue file + measured peak. A user file <sfx_dir>/<name>.<ext> wins over the synthesised cue."""
    if name in cache:
        return cache[name]
    path = None
    if sfx_dir:
        path = next((Path(sfx_dir) / f"{name}{e}" for e in SFX_EXT if (Path(sfx_dir) / f"{name}{e}").exists()), None)
    if path is None:
        cache[name] = sfxsynth.synth_info(name, str(tmp))
        return cache[name]
    p = subprocess.run([FFMPEG, "-v", "error", "-i", str(path), "-ac", "1", "-ar", "48000", "-f", "f32le", "-"],
                       capture_output=True)
    x = np.abs(np.frombuffer(p.stdout, dtype=np.float32))
    if not len(x):
        cache[name] = None
        return None
    pk = float(x.max())
    win = 480
    n = len(x) // win
    env = x[: n * win].reshape(n, win).max(axis=1) if n else x
    cache[name] = {"path": str(path), "peak_db": 20 * math.log10(pk + 1e-9),
                   "peak_t": float(np.argmax(env) * win / 48000), "dur": len(x) / 48000, "source": "file"}
    return cache[name]


# ----------------------------------------------------------------------------- face tracking
def detect_span(src, a, b, W, H, det_fps):
    import cv2
    dw = 640 if W >= H else 360
    dh = int(round(H * dw / W / 2) * 2)
    sc = W / dw
    det = cv2.FaceDetectorYN.create(YUNET, "", (dw, dh), 0.6, 0.3, 50)
    p = subprocess.Popen([FFMPEG, "-v", "error", "-ss", f"{a:.3f}", "-t", f"{b - a:.3f}", "-i", str(src),
                          "-map", "0:v:0", "-vf", f"fps={det_fps},scale={dw}:{dh}", "-pix_fmt", "bgr24",
                          "-f", "rawvideo", "-"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    fsz = dw * dh * 3
    out, k = [], 0
    while True:
        buf = p.stdout.read(fsz)
        if len(buf) < fsz:
            break
        img = np.frombuffer(buf, np.uint8).reshape(dh, dw, 3)
        _, faces = det.detect(img)
        boxes = []
        if faces is not None:
            for f in faces:
                x, y, w, h = (float(v) * sc for v in f[:4])
                boxes.append((x, y, w, h, float(f[-1])))
        out.append((a + k / det_fps, boxes))
        k += 1
    p.wait()
    return out


def _iou(p, q):
    ax, ay, aw, ah = p[:4]
    bx, by, bw, bh = q[:4]
    ix = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    return inter / (aw * ah + bw * bh - inter + 1e-9)


def select_track(samples, det_fps):
    """Dominant face with sticky switching. Returns [(t, box|None)]."""
    switch_n = max(3, int(round(1.2 * det_fps)))
    cur, cand, cand_n, lost = None, None, 0, 0
    res = []
    for t, boxes in samples:
        if not boxes:
            res.append((t, None))
            lost += 1
            continue
        largest = max(boxes, key=lambda b: b[2] * b[3])
        if cur is None:
            cur = largest
        else:
            match = max(boxes, key=lambda b: _iou(b, cur))
            if _iou(match, cur) < 0.15:
                cdist = math.hypot(match[0] + match[2] / 2 - cur[0] - cur[2] / 2, match[1] + match[3] / 2 - cur[1] - cur[3] / 2)
                if cdist > 0.8 * cur[2]:
                    match = None
            if match is None:
                lost += 1
                if lost >= 2:
                    cur, cand, cand_n = largest, None, 0
                else:
                    res.append((t, None))
                    continue
            else:
                lost = 0
                if largest is not match and largest[2] * largest[3] > 1.6 * match[2] * match[3]:
                    if cand is not None and _iou(cand, largest) > 0.2:
                        cand_n += 1
                    else:
                        cand_n = 1
                    cand = largest
                    if cand_n >= switch_n:
                        cur, cand, cand_n = largest, None, 0
                    else:
                        cur = match
                else:
                    cur, cand, cand_n = match, None, 0
        res.append((t, cur))
    return res


class FaceInterp:
    def __init__(self, track):
        self.pts = [(t, b) for t, b in track if b is not None]
        self.ts = np.array([t for t, _ in self.pts]) if self.pts else np.array([])

    def at(self, t):
        """(cx, cy, top, bottom, w) or None."""
        if not self.pts:
            return None
        i = int(np.searchsorted(self.ts, t))
        prev = self.pts[i - 1] if i > 0 else None
        nxt = self.pts[i] if i < len(self.pts) else None

        def feat(b):
            x, y, w, h = b[:4]
            return np.array([x + w / 2, y + h / 2, y, y + h, w])
        if prev and nxt:
            gap = nxt[0] - prev[0]
            if gap <= 3.0:
                u = (t - prev[0]) / gap if gap > 0 else 0
                return tuple(feat(prev[1]) * (1 - u) + feat(nxt[1]) * u)
            near = prev if t - prev[0] < nxt[0] - t else nxt
            return tuple(feat(near[1])) if abs(near[0] - t) <= 1.5 else None
        near = prev or nxt
        return tuple(feat(near[1])) if abs(near[0] - t) <= 1.5 else None


# ----------------------------------------------------------------------------- captions
def esc_ass(s):
    return s.replace("\\", "/").replace("{", "(").replace("}", ")").replace("\n", " ")


def ass_time(t):
    t = max(0.0, t)
    h = int(t // 3600)
    m = int(t % 3600 // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def font_ok(name):
    if FONTS_DIR.is_dir() and any(name.lower().replace(" ", "") in f.stem.lower().replace("-", "")
                                  for f in FONTS_DIR.glob("*.[ot]tf")):
        return True
    if not shutil.which("fc-list"):
        return False
    p = subprocess.run(["fc-list", ":", "family"], capture_output=True, text=True)
    fams = set()
    for line in p.stdout.splitlines():
        for f in line.split(","):
            fams.add(f.strip().lower())
    return name.lower() in fams


def build_groups(words):
    groups, cur = [], []
    for w in words:
        if cur:
            prev = cur[-1]
            chars = sum(len(x["w"]) + 1 for x in cur) + len(w["w"])
            brk = (len(cur) >= 4 or w["oe"] - cur[0]["os"] > 1.9 or chars > 22 or w["span"] != prev["span"]
                   or w["os"] - prev["oe"] > 0.6 or re.search(r"[.?!]$", prev["w"])
                   or (re.search(r"[,;:]$", prev["w"]) and len(cur) >= 2))
            if brk:
                groups.append(cur)
                cur = []
        cur.append(w)
    if cur:
        groups.append(cur)
    return groups


def write_ass(path, groups, ypos, total, font, size, hl, upper):
    hdr = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {OUT_W}
PlayResY: {OUT_H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{font},{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H90000000,0,0,0,0,100,100,1,0,1,7,4,8,70,70,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    for gi, g in enumerate(groups):
        g_start = 0.0 if gi == 0 and g[0]["os"] < 0.6 else g[0]["os"]
        if gi + 1 < len(groups):
            nxt = groups[gi + 1][0]["os"]
            g_end = nxt if nxt - g[-1]["oe"] < 1.2 else g[-1]["oe"] + 0.5
        else:
            g_end = min(total, g[-1]["oe"] + 0.6)
        y = ypos[gi]
        for j, w in enumerate(g):
            es = g_start if j == 0 else w["os"]
            ee = g[j + 1]["os"] if j + 1 < len(g) else g_end
            if ee - es < 0.02:
                continue
            parts = []
            for k, x in enumerate(g):
                t = esc_ass(x["w"].upper() if upper else x["w"])
                if k == j:
                    parts.append(f"{{\\c{hl}\\fscx108\\fscy108}}{t}{{\\c&H00FFFFFF&\\fscx100\\fscy100}}")
                else:
                    parts.append(t)
            pop = "\\fscx86\\fscy86\\t(0,90,\\fscx100\\fscy100)" if j == 0 else ""
            lines.append(f"Dialogue: 0,{ass_time(es)},{ass_time(ee)},Cap,,0,0,0,,{{\\an8\\pos(540,{y}){pop}}}"
                         + " ".join(parts))
    with open(path, "w") as f:
        f.write(hdr + "\n".join(lines) + "\n")
    return len(lines)


# ----------------------------------------------------------------------------- planning
def apply_overrides(words, overrides):
    words = [dict(w) for w in words]
    for ov in overrides or []:
        if "w" in ov:
            for w in words:
                if w["i"] == ov["w"]:
                    w["w"] = ov.get("text", "")
        elif "start" in ov and "end" in ov:
            s, e = float(ov["start"]), float(ov["end"])
            hit = [w for w in words if s <= (w["s"] + w["e"]) / 2 < e]
            a = min([w["s"] for w in hit], default=s)
            b = max([w["e"] for w in hit], default=e)
            toks = ov.get("text", "").split()
            words = [w for w in words if w not in hit]
            tot = sum(len(t) + 1 for t in toks) or 1
            t, new = a, []
            for tk in toks:
                d = (b - a) * (len(tk) + 1) / tot
                new.append({"i": -1, "w": tk, "s": t, "e": t + d * 0.92, "p": 1.0})
                t += d
            words = sorted(words + new, key=lambda w: w["s"])
    return [w for w in words if w["w"].strip()]


def merge_hyphen_tokens(words):
    """Whisper splits 'top-of-the-line' into 'top', '-of', '-the', '-line': glue them back."""
    out = []
    for w in words:
        if out and len(w["w"]) > 1 and w["w"].startswith("-") and w["s"] - out[-1]["e"] < 0.15:
            out[-1] = dict(out[-1], w=out[-1]["w"] + w["w"], e=w["e"])
        else:
            out.append(w)
    return out


def build_spans(pick, src_dur):
    s, e = float(pick["start"]), min(float(pick["end"]), src_dur)
    hs, he = pick.get("hook_start"), pick.get("hook_end")
    spans = []
    if hs is not None and he is not None and he - hs >= 0.8:
        hs, he = float(hs), float(he)
        spans.append(("hook", hs, he))
        if pick.get("hook_mode", "move") == "move" and s - 0.05 <= hs and he <= e + 0.05:
            if hs - s > 0.4:
                spans.append(("body", s, hs))
            if e - he > 0.4:
                spans.append(("body", he, e))
        else:
            spans.append(("body", s, e))
    else:
        spans.append(("body", s, e))
    return spans


def zoom_schedule(total, segs, words, start_in):
    cuts = sorted(sg["out_start"] for sg in segs if sg["cut"] in ("jump", "span"))
    hook_cut = [sg["out_start"] for sg in segs if sg["cut"] == "hook_to_body"]
    sent, wb = [], []
    for a, b in zip(words, words[1:]):
        mid = (a["oe"] + b["os"]) / 2
        if re.search(r"[.?!]$", a["w"]) or b["os"] - a["oe"] > 0.25:
            sent.append(mid)
        wb.append(mid)
    bounds, last = [], 0.0
    forced = list(hook_cut)
    while True:
        lo, hi = last + 2.0, last + 4.0
        if lo > total - 1.0:
            break
        f = [x for x in forced if last + 0.8 < x <= hi]
        if f:
            nb = f[0]
        else:
            pick = None
            for pool in (cuts, sent, wb):
                c = [x for x in pool if lo <= x <= hi and x < total - 1.0]
                if c:
                    pick = min(c, key=lambda x: abs(x - (last + 3.0)))
                    break
            nb = pick if pick is not None else last + 3.0
        if nb > total - 1.0:
            break
        bounds.append(nb)
        last = nb
    levels, z = [], (ZOOM_IN if start_in else 1.0)
    edges = [0.0] + bounds + [total + 1]
    for i in range(len(edges) - 1):
        levels.append((edges[i], edges[i + 1], z))
        z = 1.0 if z != 1.0 else ZOOM_IN
    return levels


# ----------------------------------------------------------------------------- main per pick
def render_pick(pick, wd, src, meta, transcript, args):
    t_start = time.time()
    pid = str(pick["id"])
    out_dir = wd / "out"
    tmp = wd / f"tmp_{pid}"
    out_dir.mkdir(exist_ok=True)
    tmp.mkdir(exist_ok=True)
    W, H, src_dur = meta["width"], meta["height"], meta["duration"]
    log(f"--- clip {pid}: {pick.get('title', '')}")

    # 1-2. spans + silence removal + frame-grid segments
    segs, diag = [], []
    for si, (kind, a, b) in enumerate(build_spans(pick, src_dur)):
        kept, dg = keep_intervals(src, a, b, not args.no_silence_cut)
        diag.append({"span": si, "kind": kind, "src": [round(a, 2), round(b, 2)], **dg})
        for ki, (ka, kb) in enumerate(kept):
            fa, fb = int(round(ka * FPS)), int(round(kb * FPS))
            if fb - fa < 3:
                continue
            if not segs:
                cut = "start"
            elif segs[-1]["span"] != si:
                cut = "hook_to_body" if segs[-1]["kind"] == "hook" else "span"
            else:
                cut = "jump"
            segs.append({"span": si, "kind": kind, "fa": fa, "fb": fb, "cut": cut,
                         "gap_removed": round(fa / FPS - segs[-1]["src_end"], 3) if cut == "jump" else None,
                         "src_start": fa / FPS, "src_end": fb / FPS})
    o = 0
    for sg in segs:
        sg["n"] = sg["fb"] - sg["fa"]
        sg["out_start"] = o / FPS
        o += sg["n"]
        sg["out_end"] = o / FPS
    nframes = o
    total = nframes / FPS
    if not 5 <= total <= 70:
        log(f"WARNING: clip {pid} is {total:.1f}s after cuts (target 10-50s)")
    save_json(out_dir / f"clip_{pid}_cutmap.json", {"segments": [
        {k: (round(v, 3) if isinstance(v, float) else v) for k, v in sg.items() if k not in ("fa", "fb")}
        for sg in segs], "out_duration": round(total, 3), "silence": diag})

    def src2out(t):
        for sg in segs:
            if sg["src_start"] <= t < sg["src_end"]:
                return sg["out_start"] + (t - sg["src_start"]), sg
        return None, None

    # words -> cut timeline
    allw = [w for s in transcript["segments"] for w in s["words"]]
    allw = merge_hyphen_tokens(apply_overrides(allw, pick.get("caption_overrides")))
    words = []
    for w in allw:
        mid = (w["s"] + w["e"]) / 2
        om, sg = src2out(mid)
        if om is None:
            continue
        os_ = sg["out_start"] + max(0.0, w["s"] - sg["src_start"])
        oe = sg["out_start"] + min(sg["src_end"], w["e"]) - sg["src_start"]
        words.append({"w": w["w"], "os": os_, "oe": max(oe, os_ + 0.05), "span": sg["span"], "seg": segs.index(sg)})
    words.sort(key=lambda w: w["os"])
    for a, b in zip(words, words[1:]):  # no overlaps
        if a["oe"] > b["os"]:
            a["oe"] = b["os"]

    # 3. audio ----------------------------------------------------------------
    t_a = time.time()
    fc, labels = [], []
    for i, sg in enumerate(segs):
        d = sg["n"] / FPS
        fc.append(f"[0:a:0]atrim=start={sg['src_start']:.4f}:end={sg['src_end']:.4f},asetpts=PTS-STARTPTS,"
                  f"afade=t=in:d=0.008,afade=t=out:st={max(0, d - 0.008):.4f}:d=0.008[a{i}]")
        labels.append(f"[a{i}]")
    fc.append("".join(labels) + f"concat=n={len(segs)}:v=0:a=1,aresample=48000[cat]")
    voice_raw = tmp / "voice_raw.wav"
    run([FFMPEG, "-y", "-i", src, "-filter_complex", ";".join(fc), "-map", "[cat]", "-c:a", "pcm_s16le", voice_raw])
    vr = stream_duration(voice_raw, "a:0")
    if abs(vr - total) > 0.05:
        raise SystemExit(f"A/V DESYNC after audio concat: audio {vr:.3f}s vs {nframes} frames = {total:.3f}s")
    lr = loudness(voice_raw)
    pregain = max(-10.0, min(24.0, VOICE_REF_LUFS - float(lr["input_i"])))

    inputs = ["-i", str(voice_raw)]
    chain = [f"[0:a]volume={pregain:.2f}dB,highpass=f=85,afftdn=nf=-25,"
             "acompressor=threshold=-20dB:ratio=3:attack=12:release=220:makeup=1.5,"
             "aformat=channel_layouts=stereo,asplit=2[v][vsc]"]
    mixins = ["[v]"]
    cues = []
    ok_music = bool(args.music) and not args.no_music
    if ok_music:
        mpath = Path(os.path.expanduser(args.music))
        mcache = wd / "music_lufs.json"
        key = f"{mpath.resolve()}:{mpath.stat().st_mtime_ns}"
        cached = load_json(mcache) if mcache.exists() else {}
        if cached.get("key") == key:
            ml, mdur = cached["I"], cached["dur"]
        else:
            mdur = summarize(mpath)["duration"] or 0.0
            ml = float(loudness(mpath, min(20.0, mdur / 4), min(60.0, mdur))["input_i"])
            save_json(mcache, {"key": key, "I": ml, "dur": mdur})
        mgain = (VOICE_REF_LUFS - args.music_db) - ml
        span = max(0.0, mdur - total - 2.0)
        off = (int(hashlib.md5(pid.encode()).hexdigest(), 16) % int(span)) if span >= 1 else 0
        inputs += ["-stream_loop", "-1", "-ss", str(off), "-i", str(mpath)]
        idx = inputs.count("-i") - 1
        chain.append(f"[{idx}:a]atrim=duration={total + 0.5:.3f},volume={mgain:.2f}dB,afade=t=in:d=0.8,"
                     f"afade=t=out:st={max(0, total - 1.6):.3f}:d=1.5,aformat=channel_layouts=stereo,aresample=48000[m]")
        chain.append("[m][vsc]sidechaincompress=threshold=0.04:ratio=8:attack=10:release=350[md]")
        mixins.append("[md]")
    else:
        chain.append("[vsc]anullsink")

    if not args.no_sfx:
        # whoosh on hook->body; a few jump cuts (gap >= 0.6s, >= 5s apart, max 3); one thud on payoff
        hb = [sg["out_start"] for sg in segs if sg["cut"] == "hook_to_body"]
        if hb:  # the clip opens on a moved hook: a small pop with the first caption
            cues.append(("pop", 0.06, -24.0))
        for t in hb:
            cues.append(("whoosh_fast", t, -20.0))
        jumps = [sg for sg in segs if sg["cut"] in ("jump", "span") and (sg["gap_removed"] or 0.6) >= 0.6]
        last = -99.0
        for sg in jumps:
            t = sg["out_start"]
            if t - last >= 5.0 and all(abs(t - h) > 1.5 for h in hb) and t < total - 2.0 and \
                    sum(1 for c in cues if c[0] == "whoosh_soft") < 3:
                cues.append(("whoosh_soft", t, -22.0))
                last = t
        pay = pick.get("payoff_start")
        if pay is None:
            body_end = float(pick["end"])
            cands = [s["start"] for s in transcript["segments"]
                     if float(pick["start"]) + 3 <= s["start"] < body_end - 1.0]
            pay = cands[-1] if cands else None
        if pay is not None:
            ot, _ = src2out(float(pay) + 0.02)
            if ot is not None and ot > 2.0:
                cues = [c for c in cues if abs(c[1] - ot) > 1.0]
                cues.append(("thud_low", ot, -15.0))
        sfx_cache = {}
        for name, t, target_peak in cues:
            inf = sfx_info(name, sfx_cache, args.sfx_dir, tmp)
            if not inf:
                continue
            start_at = t - inf["peak_t"]
            trim0 = max(0.0, -start_at)
            delay = max(0.0, start_at)
            dur = min(inf["dur"] - trim0, 1.4)
            if dur <= 0.02:
                continue
            gain = target_peak - inf["peak_db"]
            inputs += ["-i", inf["path"]]
            idx = inputs.count("-i") - 1
            lab = f"s{idx}"
            chain.append(f"[{idx}:a]atrim=start={trim0:.3f}:duration={dur:.3f},asetpts=PTS-STARTPTS,"
                         f"afade=t=out:st={max(0, dur - 0.3):.3f}:d=0.3,volume={gain:.2f}dB,aresample=48000,"
                         f"aformat=channel_layouts=stereo,adelay={int(delay * 1000)}:all=1[{lab}]")
            mixins.append(f"[{lab}]")
    chain.append("".join(mixins) + f"amix=inputs={len(mixins)}:normalize=0:duration=first:dropout_transition=0,"
                 f"apad=whole_dur={total:.4f},atrim=end={total:.4f}[mix]")
    mix_pre = tmp / "mix_pre.wav"
    run([FFMPEG, "-y", *inputs, "-filter_complex", ";".join(chain), "-map", "[mix]", "-c:a", "pcm_s16le",
         "-ar", "48000", mix_pre])
    m1 = loudness(mix_pre)
    ln = (f"loudnorm=I=-14:TP=-1.5:LRA=11:measured_I={m1['input_i']}:measured_TP={m1['input_tp']}:"
          f"measured_LRA={m1['input_lra']}:measured_thresh={m1['input_thresh']}:offset={m1['target_offset']}:"
          f"linear=true,aresample=48000,apad=whole_dur={total:.4f},atrim=end={total:.4f}")
    audio_final = tmp / "audio_final.wav"
    run([FFMPEG, "-y", "-i", mix_pre, "-af", ln, "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2", audio_final])
    t_audio = time.time() - t_a

    # 4. face tracking --------------------------------------------------------
    t_f = time.time()
    det_fps = 4 if args.fast else 5
    interps, cover = {}, []
    for si in sorted(set(sg["span"] for sg in segs)):
        ss = [sg for sg in segs if sg["span"] == si]
        a, b = ss[0]["src_start"], ss[-1]["src_end"]
        samples = detect_span(src, a, b, W, H, det_fps)
        kept_samples = [(t, bx) for t, bx in samples if any(sg["src_start"] <= t < sg["src_end"] for sg in ss)]
        tr = select_track(kept_samples, det_fps)
        cover += [bx is not None for _, bx in tr]
        interps[si] = FaceInterp(tr)
    coverage = sum(cover) / len(cover) if cover else 0.0
    t_face = time.time() - t_f

    landscape = W / H > 9 / 16 + 0.02
    layout = args.layout
    if layout == "auto":
        layout = "pad" if (landscape and coverage < 0.4) else "crop"
    if landscape:
        cw, ch = H * 9 / 16, float(H)
        bx0, by0 = 0.0, 0.0
    else:
        if W / H > 9 / 16:
            cw, ch = H * 9 / 16, float(H)
        else:
            cw, ch = float(W), W * 16 / 9
        bx0, by0 = (W - cw) / 2, (H - ch) / 2

    # per-frame target + camera with hysteresis dead-zone and EMA, snaps only on cuts
    zlv = zoom_schedule(total, segs, words, start_in=segs[0]["kind"] == "hook")
    plan = []  # (zx0, zy0, zw, zh, s, chin_out, face_top_out)
    cam_x = cam_y = None
    moving_x = moving_y = False
    dz_start, dz_stop, alpha = 0.07 * cw, 0.015 * cw, 0.10
    zi = 0
    for sg in segs:
        fi = sg["fa"]
        for k in range(sg["n"]):
            t_out = (len(plan)) / FPS
            ts = (fi + k) / FPS
            f = interps[sg["span"]].at(ts)
            if f is not None:
                tx, ty = f[0], f[1]
            else:
                tx, ty = W / 2, H * 0.42
            if cam_x is None or (k == 0 and sg["cut"] != "start" and abs(tx - cam_x) > dz_start):
                cam_x, cam_y = tx, ty
            else:
                ex, ey = tx - cam_x, ty - cam_y
                if moving_x or abs(ex) > dz_start:
                    moving_x = abs(ex) > dz_stop
                    cam_x += alpha * ex
                if moving_y or abs(ey) > 0.06 * ch:
                    moving_y = abs(ey) > 0.015 * ch
                    cam_y += 0.08 * ey
            while zi < len(zlv) - 1 and t_out >= zlv[zi][1]:
                zi += 1
            z = zlv[zi][2]
            if layout == "crop":
                x0 = bx0 if not landscape else min(max(cam_x - cw / 2, 0.0), W - cw)
                y0 = by0
                fx = min(max(cam_x, x0), x0 + cw)
                fy = min(max(cam_y, y0), y0 + ch)
                zw, zh = cw / z, ch / z
                zx0 = min(max(fx - (fx - x0) / z, x0), x0 + cw - zw)
                zy0 = min(max(fy - (fy - y0) / z, y0), y0 + ch - zh)
                s = OUT_W / zw
                chin = (f[3] - zy0) * s if f is not None else None
                top = (f[2] - zy0) * s if f is not None else None
            else:  # pad: full-width foreground band over blurred background
                s = OUT_W * z / W
                zx0 = (W - OUT_W / s) / 2
                fg_h = H * s
                fg_y = 880 - fg_h / 2
                zy0 = -fg_y / s
                zw, zh = OUT_W / s, OUT_H / s
                chin = fg_y + fg_h if f is None else fg_y + f[3] * s
                top = fg_y if f is None else fg_y + f[2] * s
                chin = max(chin, fg_y + fg_h) if f is None else chin
            plan.append((zx0, zy0, zw, zh, s, chin, top))

    # 6. captions -------------------------------------------------------------
    font = args.font if font_ok(args.font) else "DejaVu Sans"
    if font != args.font:
        log(f"font {args.font!r} not found, using {font}")
    groups = build_groups(words)
    size = args.font_size
    ypos, warn_face = [], 0
    default_top = 1290
    for g in groups:
        chars = sum(len(w["w"]) + 1 for w in g)
        nlines = 1 if chars * 0.62 * size <= 940 else 2
        block = nlines * size * 1.15 + 20
        max_top = int(UI_SAFE_Y - 15 - block)
        f0, f1 = int(g[0]["os"] * FPS), min(nframes - 1, int(g[-1]["oe"] * FPS) + 1)
        chins = [plan[i][5] for i in range(f0, f1 + 1) if plan[i][5] is not None]
        tops = [plan[i][6] for i in range(f0, f1 + 1) if plan[i][6] is not None]
        chin = max(chins) if chins else None
        y = default_top
        if chin is not None and chin + 60 > y:
            y = int(chin + 60)
        if y > max_top:
            if chin is not None and chin + 15 > max_top and tops and min(tops) - 40 - block > 140:
                y = int(min(tops) - 40 - block)  # face too low: go above the head
            else:
                y = max_top
            if chin is not None and y < chin + 15 and y > min(tops or [0]) - block:
                warn_face += 1
        ypos.append(y)
    ass = tmp / "captions.ass"
    hl = "&H0066FF00&" if args.highlight == "green" else "&H0000FFFF&"
    n_ev = write_ass(ass, groups, ypos, total, font, size, hl, not args.no_upper)

    # 7. video pass -----------------------------------------------------------
    import cv2
    t_v = time.time()
    out = out_dir / f"clip_{pid}.mp4"
    preset = args.preset or ("ultrafast" if args.fast else "faster")
    crf = str(args.crf if args.crf is not None else (24 if args.fast else 20))
    def fesc(x):
        return str(x).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
    ass_arg = f"'{fesc(ass)}'" + (f":fontsdir='{fesc(FONTS_DIR)}'" if FONTS_DIR.is_dir() else "")
    enc = subprocess.Popen([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24",
                            "-s", f"{OUT_W}x{OUT_H}", "-r", str(FPS), "-i", "-", "-i", str(audio_final),
                            "-filter_complex", f"[0:v]ass={ass_arg},format=yuv420p[v]", "-map", "[v]", "-map", "1:a",
                            "-c:v", "libx264", "-preset", preset, "-crf", crf, "-pix_fmt", "yuv420p",
                            "-profile:v", "high", "-colorspace", "bt709", "-color_primaries", "bt709",
                            "-color_trc", "bt709", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
                            "-movflags", "+faststart", str(out)],
                           stdin=subprocess.PIPE, stderr=open(tmp / "enc.log", "w"))
    fsz = W * H * 3
    pi = 0
    interp = cv2.INTER_LINEAR
    for si in sorted(set(sg["span"] for sg in segs)):
        ss = [sg for sg in segs if sg["span"] == si]
        f0, f_end = ss[0]["fa"], ss[-1]["fb"]
        dec = subprocess.Popen([FFMPEG, "-v", "error", "-ss", f"{f0 / FPS:.4f}", "-i", str(src),
                                "-t", f"{(f_end - f0) / FPS + 0.5:.4f}", "-map", "0:v:0", "-vf", f"fps={FPS}",
                                "-pix_fmt", "bgr24", "-f", "rawvideo", "-"],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        idx, last = f0, None
        for sg in ss:
            while idx < sg["fa"]:
                b = dec.stdout.read(fsz)
                if len(b) == fsz:
                    last = b
                idx += 1
            for _ in range(sg["n"]):
                b = dec.stdout.read(fsz)
                if len(b) == fsz:
                    last = b
                elif last is None:
                    raise SystemExit(f"decoder returned no frames at src {idx / FPS:.2f}s")
                idx += 1
                frame = np.frombuffer(last, np.uint8).reshape(H, W, 3)
                zx0, zy0, zw, zh, s, _, _ = plan[pi]
                M = np.float32([[s, 0, -zx0 * s], [0, s, -zy0 * s]])
                if layout == "pad":
                    small = cv2.resize(frame, (W // 8, H // 8), interpolation=cv2.INTER_AREA)
                    sh, sw = small.shape[:2]
                    bw = min(sw, int(sh * 9 / 16))
                    bg = small[:, (sw - bw) // 2:(sw - bw) // 2 + bw]
                    bg = cv2.GaussianBlur(bg, (0, 0), 4)
                    bg = cv2.resize(bg, (OUT_W, OUT_H), interpolation=cv2.INTER_LINEAR)
                    bg = (bg * 0.55).astype(np.uint8)
                    img = cv2.warpAffine(frame, M, (OUT_W, OUT_H), dst=bg, flags=interp,
                                         borderMode=cv2.BORDER_TRANSPARENT)
                else:
                    img = cv2.warpAffine(frame, M, (OUT_W, OUT_H), flags=interp, borderMode=cv2.BORDER_REPLICATE)
                enc.stdin.write(img.tobytes())
                pi += 1
        dec.stdout.close()
        dec.kill()
        dec.wait()
    enc.stdin.close()
    rc = enc.wait()
    if rc != 0:
        raise SystemExit(f"encoder failed: {(tmp / 'enc.log').read_text()[-1500:]}")
    t_video = time.time() - t_v
    vd, ad = assert_av(out, 0.1, f"clip {pid}")
    fin = summarize(out)
    lf = loudness(out)

    # 8. sidecar text + metadata ---------------------------------------------
    tags = pick.get("hashtags") or ["#shorts"]
    with open(out_dir / f"clip_{pid}.txt", "w") as f:
        f.write(f"TITLE: {pick.get('title', '')}\n\n")
        f.write(f"DESCRIPTION: {pick.get('description') or pick.get('hook_line', '')}\n\n")
        if pick.get("comment_bait"):
            f.write(f"PINNED COMMENT: {pick['comment_bait']}\n\n")
        f.write("HASHTAGS: " + " ".join(tags[:5]) + "\n\n")
        f.write(f"WHY: {pick.get('why', '')}\n")
        f.write(f"SOURCE: {fmt_ts(float(pick['start']))}-{fmt_ts(float(pick['end']))}"
                + (f", hook {fmt_ts(float(pick['hook_start']))}-{fmt_ts(float(pick['hook_end']))}"
                   if pick.get("hook_start") is not None else "") + "\n")
    rep = {"id": pid, "out": str(out), "duration_video": vd, "duration_audio": ad, "width": fin["width"],
           "height": fin["height"], "fps": fin["fps"], "lufs": float(lf["input_i"]), "true_peak": float(lf["input_tp"]),
           "layout": layout, "face_coverage": round(coverage, 2), "segments": len(segs),
           "caption_groups": len(groups), "caption_events": n_ev, "caption_face_conflicts": warn_face,
           "zoom_changes": len(zlv) - 1, "sfx": [(c[0], round(c[1], 2)) for c in cues], "music": ok_music,
           "voice_pregain_db": round(pregain, 1), "font": font, "preset": preset, "crf": crf,
           "timing_s": {"audio": round(t_audio, 1), "face": round(t_face, 1), "video": round(t_video, 1),
                        "total": round(time.time() - t_start, 1)}}
    save_json(out_dir / f"clip_{pid}.json", rep)
    if not args.keep_tmp:
        for p in tmp.iterdir():
            p.unlink()
        tmp.rmdir()
    # contact sheet
    subprocess.run([sys.executable, str(Path(__file__).parent / "look.py"), str(out)], check=False)
    print(f"clip_{pid}.mp4: v {vd:.2f}s / a {ad:.2f}s, {fin['width']}x{fin['height']} {fin['fps']}fps, "
          f"{rep['lufs']:.1f} LUFS TP {rep['true_peak']:.1f}, layout={layout} face={coverage:.0%}, "
          f"{len(segs)} segs, {len(zlv) - 1} zooms, sfx={len(cues)}, render {rep['timing_s']['total']}s")
    return rep


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("workdir")
    ap.add_argument("picks", help="picks.json (list of picks or {\"picks\": [...]})")
    ap.add_argument("--only", action="append", help="render only these pick ids")
    ap.add_argument("--fast", action="store_true", help="preview: x264 ultrafast CRF 24, 4 fps face sampling")
    ap.add_argument("--preset", help="x264 preset (default faster; ultrafast with --fast)")
    ap.add_argument("--crf", type=int)
    ap.add_argument("--layout", choices=["auto", "crop", "pad"], default="auto")
    ap.add_argument("--font", default="Lato Black")
    ap.add_argument("--font-size", type=int, default=84)
    ap.add_argument("--highlight", choices=["yellow", "green"], default="yellow")
    ap.add_argument("--no-upper", action="store_true", help="keep caption case (default UPPERCASE)")
    ap.add_argument("--music", help="optional music bed (your own licensed file); ducked under the voice")
    ap.add_argument("--music-db", type=float, default=17.0, help="music level below the voice in LU (default 17)")
    ap.add_argument("--no-music", action="store_true", help="ignore --music")
    ap.add_argument("--sfx-dir", help="folder with your own <cue>.wav/.mp3 (whoosh_fast, whoosh_soft, thud_low, "
                                      "pop, tick) to use instead of the synthesised cues")
    ap.add_argument("--no-sfx", action="store_true")
    ap.add_argument("--no-silence-cut", action="store_true")
    ap.add_argument("--keep-tmp", action="store_true")
    args = ap.parse_args()
    need_ffmpeg()
    need_yunet()
    if args.music and not os.path.exists(os.path.expanduser(args.music)):
        raise SystemExit(f"--music file not found: {args.music}")
    if args.sfx_dir and not os.path.isdir(os.path.expanduser(args.sfx_dir)):
        raise SystemExit(f"--sfx-dir is not a folder: {args.sfx_dir}")

    wd = Path(os.path.expanduser(args.workdir))
    src = wd / "source.mp4"
    meta = summarize(src)
    transcript = load_json(wd / "transcript.json")
    picks = load_json(args.picks)
    if isinstance(picks, dict):
        picks = picks.get("picks", [])
    if args.only:
        picks = [p for p in picks if str(p["id"]) in set(args.only)]
    if not picks:
        raise SystemExit("no picks to render")
    reps = []
    for p in picks:
        reps.append(render_pick(p, wd, src, meta, transcript, args))
    save_json(wd / "out" / "render_report.json", reps)
    print(json.dumps([{k: r[k] for k in ("id", "out", "duration_video", "duration_audio", "lufs", "layout",
                                          "face_coverage", "timing_s")} for r in reps], indent=1))


if __name__ == "__main__":
    main()
