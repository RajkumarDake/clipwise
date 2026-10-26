#!/usr/bin/env python3
"""transcribe.py WORKDIR [--model large-v3-turbo] [--language xx] [--device auto|cpu|cuda]

WORKDIR/source.mp4 -> WORKDIR/transcript.json (segments + word timings, global word
index `i`), WORKDIR/transcript.txt (one timestamped line per segment, for the agent to
read and pick moments from) and WORKDIR/words.tsv (i, start, end, prob, word).

Recipe (measured on quiet, code-mixed phone takes; do not "improve" it):
  normalise a 16 kHz mono ASR copy first (highpass, afftdn, loudnorm, dynaudnorm),
  language=None (auto-detect), vad_filter=False, condition_on_previous_text=False,
  temperature fallback, NO initial_prompt (it leaks into the transcript).

Model: --model or $CLIPWISE_MODEL, default large-v3-turbo. Smaller models (small, base)
are faster but loop/hallucinate on accented or code-mixed speech.
Device: CUDA (float16) when a GPU is visible to CTranslate2, else CPU int8.
Do not run this at the same time as an ffmpeg encode on a small CPU.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import ensure_venv  # noqa: E402

ensure_venv(("numpy", "faster_whisper"))

import argparse  # noqa: E402
import time  # noqa: E402
import wave  # noqa: E402
import zlib  # noqa: E402
from pathlib import Path  # noqa: E402

import numpy as np  # noqa: E402

from _common import FFMPEG, fmt_ts, log, need_ffmpeg, run, save_json  # noqa: E402

MODELS = {
    "large-v3-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
    "turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
}
# languages written in a non-Latin script: if whisper returns mostly ASCII for these, it translated
NON_LATIN = {"te", "hi", "ta", "kn", "ml", "mr", "bn", "gu", "pa", "ur", "ne", "si", "or", "as", "ar", "fa", "he",
             "yi", "ru", "uk", "bg", "sr", "mk", "be", "kk", "el", "hy", "ka", "am", "th", "lo", "km", "my", "zh",
             "ja", "ko", "bo", "mn", "ps", "sd", "yue"}


def pick_device(want):
    if want != "auto":
        return want
    try:
        import ctranslate2
        return "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
    except Exception:  # noqa: BLE001
        return "cpu"


def load_model(name, device, compute_type, threads):
    from faster_whisper import WhisperModel
    kw = dict(device=device, compute_type=compute_type, cpu_threads=threads)
    try:  # use the cached copy without touching the network when it is there
        return WhisperModel(name, local_files_only=True, **kw)
    except Exception:  # noqa: BLE001
        log(f"model {name} not cached yet: downloading it once (large-v3-turbo is ~1.6 GB)")
        return WhisperModel(name, **kw)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("workdir")
    ap.add_argument("--model", default=os.environ.get("CLIPWISE_MODEL", "large-v3-turbo"),
                    help="large-v3-turbo (default) | large-v3 | medium | small | any faster-whisper model id/path")
    ap.add_argument("--language", default=None, help="force a language code; default auto-detect (recommended)")
    ap.add_argument("--device", default=os.environ.get("CLIPWISE_DEVICE", "auto"), choices=["auto", "cpu", "cuda"])
    ap.add_argument("--compute-type", default=None, help="default int8 on CPU, float16 on CUDA")
    ap.add_argument("--beam", type=int, default=5)
    ap.add_argument("--threads", type=int, default=os.cpu_count() or 2)
    args = ap.parse_args()
    need_ffmpeg()

    wd = Path(os.path.expanduser(args.workdir))
    src = wd / "source.mp4"
    if not src.exists():
        raise SystemExit(f"{src} missing; run fetch.py first")
    t0 = time.time()
    wav = wd / "asr16k.wav"
    run([FFMPEG, "-y", "-i", src, "-map", "0:a:0", "-vn", "-af",
         "highpass=f=80,afftdn=nf=-25,loudnorm=I=-16:TP=-1.5:LRA=11,dynaudnorm=f=200:g=5",
         "-ac", "1", "-ar", "16000", wav])
    t_norm = time.time() - t0

    device = pick_device(args.device)
    ctype = args.compute_type or ("float16" if device == "cuda" else "int8")
    name = MODELS.get(args.model, args.model)
    t1 = time.time()
    model = load_model(name, device, ctype, args.threads)
    t_load = time.time() - t1
    t2 = time.time()
    # decode the wav ourselves: faster-whisper's PyAV decoder breaks with some PyAV versions
    with wave.open(str(wav), "rb") as wf:
        pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
    segs, info = model.transcribe(
        pcm, language=args.language, task="transcribe", beam_size=args.beam,
        vad_filter=False, condition_on_previous_text=False, temperature=[0.0, 0.2, 0.4, 0.6],
        word_timestamps=True)
    out_segs, gi = [], 0
    for s in segs:
        words = []
        for w in (s.words or []):
            txt = w.word.strip()
            if not txt:
                continue
            words.append({"i": gi, "w": txt, "s": round(w.start, 3), "e": round(w.end, 3),
                          "p": round(w.probability, 3)})
            gi += 1
        out_segs.append({"id": len(out_segs), "start": round(s.start, 3), "end": round(s.end, 3),
                         "text": s.text.strip(), "words": words})
        log(f"{fmt_ts(s.start)} {s.text.strip()[:90]}")
    t_asr = time.time() - t2

    warnings = []
    dur = float(info.duration)
    if info.language in NON_LATIN:
        letters = [c for s in out_segs for c in s["text"] if c.isalpha()]
        ascii_share = sum(c.isascii() for c in letters) / max(1, len(letters))
        if ascii_share > 0.6:
            warnings.append(f"detected '{info.language}' but {ascii_share:.0%} of the text is Latin script: whisper "
                            "most likely TRANSLATED the speech into English. Use the text as a timing skeleton and "
                            "meaning guide only; write captions in the speaker's own words/script via "
                            "caption_overrides in picks.json.")
    for s in out_segs:  # degenerate-loop detector (token repeats, or character loops)
        toks = [w["w"].lower().strip(".,!?") for w in s["words"]]
        b = s["text"].encode("utf-8")
        ratio = len(b) / max(1, len(zlib.compress(b))) if len(b) > 40 else 0
        if (len(toks) >= 8 and max(toks.count(t) for t in set(toks)) > 0.5 * len(toks)) or ratio > 2.4:
            warnings.append(f"segment {s['id']} at {fmt_ts(s['start'])} looks like a repetition loop "
                            "(degenerate decode) - rerun with --model large-v3-turbo")
    covered = sum(s["end"] - s["start"] for s in out_segs)
    if dur > 10 and covered < 0.4 * dur:
        warnings.append(f"segments cover only {covered:.0f}s of {dur:.0f}s - transcript is incomplete "
                        "(music-only parts are fine; otherwise try a larger model)")

    res = {"model": name, "device": device, "compute_type": ctype, "language": info.language,
           "language_probability": round(info.language_probability, 3), "duration": round(dur, 3),
           "n_words": gi, "segments": out_segs, "warnings": warnings,
           "timing": {"normalise_s": round(t_norm, 1), "model_load_s": round(t_load, 1), "asr_s": round(t_asr, 1),
                      "realtime_factor": round(t_asr / dur, 2) if dur else None}}
    save_json(wd / "transcript.json", res)
    with open(wd / "transcript.txt", "w", encoding="utf-8") as f:
        f.write(f"# model={args.model} language={info.language} ({info.language_probability:.2f}) "
                f"duration={dur:.1f}s  times are SOURCE seconds; [start-end] (first word index)\n")
        for w in warnings:
            f.write(f"# WARNING: {w}\n")
        for s in out_segs:
            wi = s["words"][0]["i"] if s["words"] else "-"
            f.write(f"[{s['start']:.2f}-{s['end']:.2f}] (w{wi}) {s['text']}\n")
    with open(wd / "words.tsv", "w", encoding="utf-8") as f:
        f.write("i\tstart\tend\tprob\tword\n")
        for s in out_segs:
            for w in s["words"]:
                f.write(f"{w['i']}\t{w['s']:.2f}\t{w['e']:.2f}\t{w['p']:.2f}\t{w['w']}\n")
    os.remove(wav)
    print(f"transcribed {dur:.1f}s with {args.model} on {device}/{ctype}: lang={info.language} "
          f"({info.language_probability:.2f}), {len(out_segs)} segments, {gi} words; ASR {t_asr:.1f}s "
          f"(x{res['timing']['realtime_factor']} realtime), model load {t_load:.1f}s, normalise {t_norm:.1f}s")
    for w in warnings:
        print("WARNING:", w)
    print(str(wd / "transcript.txt"))


if __name__ == "__main__":
    main()
