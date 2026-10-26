# Contributing

Thanks for helping. Keep changes small and testable.

- **Run it for real.** Any change to `fetch.py`, `transcribe.py` or `render.py` needs one real
  render: `run.py prep` on a short clip, a `picks.json`, `run.py render --fast`, then look at the
  `_sheet.png`. Paste the `clip_N.mp4: v ... / a ...` line and the LUFS figure in the PR.
- **Keep the A/V rule.** Audio and video stream durations of every output must match within
  0.1 s; `render.py` asserts it. Do not switch the cut assembly to `aselect`/`select`.
- **No media in the repo.** No video, audio, fonts or model files. SFX are synthesised in
  `scripts/sfx.py`; models and fonts are downloaded by `setup.sh` with a pinned sha256.
- **Portable paths only.** Scripts find themselves relative to their own file and use
  `CLIPWISE_HOME` / `CLIPWISE_VENV` / `CLIPWISE_WORK`. No absolute user paths.
- **Agent-agnostic docs.** `clipwise/SKILL.md` must work for any coding agent. Keep it tight:
  commands, schema, pitfalls.
- Python 3.10+, standard library plus the packages in `requirements.txt`.

Bug reports: include `python3 clipwise/scripts/run.py check` output, your OS, the command,
and the last 30 lines of the error.
