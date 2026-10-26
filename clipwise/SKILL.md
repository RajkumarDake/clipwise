---
name: clipwise
description: "Use when the user gives a video file or link and wants Shorts/Reels/TikToks cut from it. Fetches and transcribes locally, you pick the best 10-50 s moments with a rubric, the user approves, then it renders 9:16 clips with face-tracked crop, word-by-word captions, SFX, optional ducked music and -14 LUFS audio, plus titles and hashtags."
license: MIT
---

# clipwise: long video -> vertical Shorts

Everything runs locally (CPU is fine; CUDA is used for transcription when present).
`SKILL_DIR` below means the folder this file is in. All commands are
`python3 SKILL_DIR/scripts/run.py ...`; the scripts switch into the clipwise venv themselves.
Jobs are written to `~/clipwise-work/<job>/` (`$CLIPWISE_WORK`), or to `--workdir DIR`.

## 0. Setup check (every session, once)

```
python3 SKILL_DIR/scripts/run.py check
```
It must end with `ready`. If not, tell the user that clipwise needs a one-time setup (about
2 GB: python packages plus the whisper model), ask them to read `SKILL_DIR/setup.sh`, and
run `bash SKILL_DIR/setup.sh` once they agree. Missing ffmpeg or Python 3.10+: setup.sh prints
the install command for their OS; do not install system packages without asking.

## Workflow

1. **Prep** (fetch + transcribe, one command):
   ```
   python3 SKILL_DIR/scripts/run.py prep "<path or link>" --job <name> [--start S --max-seconds N] [--cookies cookies.txt] [--model large-v3-turbo] [--language xx]
   ```
   Output in the job folder: `source.mp4` (first video + audio stream, SDR, CFR 30 when
   re-encoded), `probe.json`, `transcript.txt` (one `[start-end] (wN) text` line per
   segment, SOURCE seconds), `transcript.json`, `words.tsv`. For long videos use
   `--start/--max-seconds` on the part worth clipping; transcription is about 0.6-1x realtime on CPU.
2. **Pick moments yourself.** Read `transcript.txt` (and any `# WARNING` lines at the top)
   with `SKILL_DIR/references/viral-rubric.md`. Write `<job>/picks.json`: each pick 10-50 s,
   hook first, clean payoff ending. How many: see the rule below.
3. **Get approval before rendering.** Show one line per pick: timestamps, hook, title,
   one-line why. Rendering costs about 1-2 minutes per clip on a small CPU. If you wrote
   caption text by hand (non-English or code-mixed speech), show those lines too.
4. **Render**:
   ```
   python3 SKILL_DIR/scripts/run.py render --job <name> [--fast] [--only 2] [--music bed.mp3]
   ```
   Per pick in `<job>/out/`: `clip_<id>.mp4`, `clip_<id>.txt` (title, description, pinned
   comment, hashtags, why), `clip_<id>.json` (probe numbers, layout, face coverage, SFX,
   timings), `clip_<id>_cutmap.json`, `clip_<id>_sheet.png`. `--fast` = preview quality.
5. **Look at every `clip_<id>_sheet.png`** with your image/vision tool (10 frames; red line
   = y 1620, platform UI below it). Check: face in frame, captions readable, captions not on
   the face, nothing important under the red line, colour not washed out. Fix (picks, caption
   overrides, `--layout`, `--font-size`) and re-render with `--only <id>`.
6. **Deliver** the MP4 paths and the `.txt` contents. Offer `run.py clean --job <name>`
   (deletes `source.mp4` and tmp dirs, keeps `out/`).

## How many Shorts per video

- **The user named a number** ("Make 5 Shorts from <link>"): make exactly that many. If the
  video does not have that many strong moments, say so before rendering and propose the
  honest number ("I found 3 moments that pass the checklist; the other 2 would be weak").
- **No number given:** scale with the length of the source (or of the window transcribed):

  | source length | Shorts |
  | --- | --- |
  | under 5 min | 1-2 |
  | 5-15 min | 3-5 |
  | 15-45 min | 5-8 |
  | 45 min+ (podcast, talk) | 8-15 |

- Only moments that pass the rubric checklist count. Never pad with weak clips to hit a number.

## picks.json (minimal)

```json
[{"id": "1", "start": 65.5, "end": 110.3, "title": "Is every flood caused by climate change?",
  "hook_start": 74.85, "hook_end": 80.1, "payoff_start": 102.0,
  "why": "Unexpected claim first, resolves it at the end.", "comment_bait": "Did you know this?",
  "description": "...", "hashtags": ["#shorts", "#climate"],
  "caption_overrides": [{"w": 219, "text": ""}, {"start": 90.1, "end": 92.4, "text": "exact words here"}]}]
```
Required: `id`, `start`, `end` (source seconds). `hook_start/hook_end` moves a 2-3 s line to
the front (`"hook_mode": "repeat"` keeps it in the body too). `payoff_start` places the low
thud. `caption_overrides`: `{"w": N, "text": ""}` edits/deletes word N (`(wN)` in
transcript.txt, `i` in words.tsv); `{"start", "end", "text"}` replaces all words in that span.
Full schema and scoring: `references/viral-rubric.md`.

## Render flags

| flag | effect |
| --- | --- |
| `--fast` | x264 ultrafast CRF 24, 4 fps face sampling (preview). Default: preset faster, CRF 20 |
| `--only ID` | render one pick (repeatable) |
| `--layout auto\|crop\|pad` | `pad` = full 16:9 frame on a blurred background (use for wide multi-person shots). auto picks pad when a wide source has < 40 % face coverage |
| `--music FILE` | user-supplied music bed, about 17 LU under the voice, ducked by sidechain compression. `--music-db 20` = quieter |
| `--sfx-dir DIR` | use your own `whoosh_fast`, `whoosh_soft`, `thud_low`, `pop`, `tick` (.wav/.mp3/.flac/.ogg) instead of the synthesised ones |
| `--no-sfx`, `--no-silence-cut` | turn those off |
| `--font "Lato Black"`, `--font-size 84`, `--highlight yellow\|green`, `--no-upper` | caption style |
| `--preset`, `--crf`, `--keep-tmp` | encoder overrides; keep voice/mix wavs and `captions.ass` for debugging |

Environment: `CLIPWISE_HOME` (default `~/.clipwise`), `CLIPWISE_VENV`, `CLIPWISE_WORK`,
`CLIPWISE_MODEL` (default `large-v3-turbo`), `CLIPWISE_DEVICE` (`auto|cpu|cuda`).

## What render does per pick

Hook moved to the front, silences > 0.35 s cut (threshold from the clip's own speech/noise
levels), kept pieces snapped to the 30 fps grid so audio samples equal frames. Face tracking
with YuNet on the frames that survive the cut: dominant face, sticky (a new face must be 1.6x
larger for 1.2 s to take over), smooth camera with a dead-zone, snaps only on cuts. Punch-in
zoom 1.0/1.12 every 2-4 s. Word-by-word ASS captions (4 words max, active word highlighted)
placed below the chin and above y 1620. Voice cleanup, synthesised SFX, optional ducked
music, two-pass loudnorm to -14 LUFS / -1.5 dBTP. H.264 + AAC 48 kHz, 1080x1920, 30 fps.
Final A/V stream durations are asserted.

## Measured timings (2-core CPU, no GPU)

| step | measured |
| --- | --- |
| fetch: 85 MB 1080p VP9 web download + 3 min window re-encode to H.264 | 7.7 min total (download dominated, about 5 min) |
| fetch: 20 s 10-bit HDR iPhone clip (tone-map + CFR) | 45 s (HDR costs about 2.3x the duration) |
| transcribe large-v3-turbo, 3 min English | 110 s ASR (0.61x realtime) + 19 s model load |
| render default, 40.6 s clip | 85 s |
| render `--fast`, 26.4 s clip / 17.1 s pad clip | 32 s / 30 s |

## Pitfalls

- **YouTube bot wall** ("Sign in to confirm you're not a bot"): intermittent, IP-dependent.
  fetch exits 3. Ask the user for `--cookies cookies.txt` (exported from their browser) or the
  file itself / a Drive or Dropbox link. Do not loop over yt-dlp player clients or proxies.
  Private Drive links exit 2: ask for "anyone with the link" sharing or a local file.
- **A/V sync is a hard rule.** Every output's audio and video stream durations must match
  within 0.1 s (render asserts it). Never trim with stream copy at a non-zero start, never
  cut audio with `aselect`; use the scripts.
- **ASR recipe is fixed:** normalise the audio first, auto-detect the language, no VAD, no
  `initial_prompt` (it leaks into the transcript). Keep large-v3-turbo; `small` loops and
  hallucinates on accented or code-mixed speech (transcribe warns about loops).
- **Non-English speech may come back translated.** For many languages written in a
  non-Latin script (and for code-mixed speech) whisper returns English. The transcript then
  is a timing and meaning guide only: write the caption lines in the speaker's own language
  and script with `caption_overrides` `{"start", "end", "text"}` spans, and show them to the
  user before rendering. Titles and hashtags also follow the speaker's language.
- **Captions:** never over the face, and above the bottom ~300 px (platform UI). If the
  sheet shows a conflict, shorten the pick, use `--layout pad`, or lower `--font-size`.
- **Tracker follows the biggest face, not the speaker.** For two people talking in one
  wide shot use `--layout pad`, or pick a window where one person speaks.
- **Upscaling:** a 1080p 16:9 source is cropped to 608x1080 and scaled 1.8x; 720p sources
  are 2.7x and look soft. Prefer 1080p+ downloads.
- **Small CPUs:** never run two encodes, or transcription and an encode, at the same time;
  render picks one after another (run.py already does).
