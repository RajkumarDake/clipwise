# Picking Shorts moments: rubric and picks.json

The agent picks moments by READING `transcript.txt` (not with a script), scores them
against this rubric, writes `picks.json`, and shows the user the list before rendering.

## Hard rules

- **Length 10-50 s** after silence removal (render.py cuts roughly 5-15 % dead air, so a
  45 s source window lands near 40 s).
- **No intro, no slow setup.** The clip starts on value or a claim. "Hi everyone, in
  this video..." is never inside a pick.
- **The first 2-3 s must stop the scroll.** If the strongest line comes later, move it to
  the front: set `hook_start`/`hook_end` to that 2-3 s line (render.py plays it first,
  then the body, and removes it from the body so it is not heard twice).
- **Self-contained**: makes sense with zero context from the rest of the video.
- **Clean ending on a payoff**, never mid-sentence. End on the verdict / twist / number.
  Set `payoff_start` to the payoff line's start (the low thud lands there).
- **Stay in the speaker's language and voice.** Titles, captions and descriptions use the
  language (and script, and code-mixing) the speaker uses. Do not rewrite a Spanish,
  Hindi or Hinglish video into polished English. When the user names an audience or
  language for titles, follow that.

## Score each candidate (0-3 each, write the numbers in `scores`)

| key | what earns a 3 |
| --- | --- |
| hook | pattern interrupt / unexpected claim in the first 2 s ("X is useless", a number, a confession) |
| emotion | real surprise, anger, humour, inspiration, fear or excitement is audible |
| curiosity | opens a question early and pays it off at the end (curiosity gap) |
| story | mini arc: hook -> problem -> struggle -> twist -> solution -> ending |
| value | teaches something new in under 30 s; someone would save it |
| relatable | a "this is literally me" moment for the people who watch this channel |

Also boost: **conflict** (X vs Y, myth vs truth, beginner vs expert) and a concrete
detail (a number, a name, a price). Take the top picks by total (how many: the user's
number, else the length rule in SKILL.md); drop anything with hook < 2 or that needs
context, even if that leaves fewer clips than asked. Say so instead of padding.

## Cut the swipe-away points

Read the pick window line by line and predict where a viewer leaves: dead air, a
rambling setup, the same point said twice, "uh so basically", a tangent. Move `start`
past it, end before it, or split into two picks. render.py removes pauses > 0.35 s
itself; you remove the *content* that is boring.

Something must change on screen every few seconds. render.py does cuts, punch-in
zooms every 2-4 s, caption pops and sparse SFX. If a pick has a 10 s stretch with no
sentence break, it will feel static; prefer a different window.

In interviews, prefer windows where one person makes the point. The face tracker follows
the largest face, not the active speaker; a rapid back-and-forth crops badly (use
`--layout pad` for those).

## Per pick, write

- `title`: curiosity, true to the clip (no clickbait lie), <= 60 chars.
- `comment_bait`: one line that invites a reply ("Which one do you use?").
- `why`: one line: why this stops the scroll and why people watch to the end.
- `description` and 3-5 `hashtags` (include `#shorts`).

## Final checklist (every pick, before rendering)

1. Stops the scroll in 2 s?
2. A reason to watch to the end (open loop / payoff)?
3. Emotion present?
4. Would someone send it to a friend or comment?
5. No boring parts left?

Any "no" -> fix the window or drop the pick.

## Do NOT produce

Market analysis, demand scores, "10 angles", "15 hooks", thumbnail concepts, series
ideas, or self-rated "9/10 virality" numbers. They are noise; the user wants clips.

## picks.json format

A JSON list (or `{"picks": [...]}`). Times are SOURCE seconds from `transcript.txt`.

```json
[
 {
  "id": "1",
  "start": 132.4,
  "end": 171.0,
  "title": "The one setting nobody changes (and should)",
  "hook_line": "This one setting cost me a whole month",
  "hook_start": 150.2,
  "hook_end": 152.6,
  "payoff_start": 166.8,
  "why": "Opens on a costly mistake, explains it in 3 steps, ends on the fix.",
  "comment_bait": "Did you know about this one?",
  "description": "The default everyone keeps, why it hurts, and the 10-second fix.",
  "hashtags": ["#shorts", "#productivity", "#tips"],
  "scores": {"hook": 3, "emotion": 2, "curiosity": 3, "story": 2, "value": 3, "relatable": 2},
  "caption_overrides": [
   {"start": 150.2, "end": 152.6, "text": "This one setting cost me a whole month"},
   {"w": 412, "text": "Kubernetes"},
   {"w": 415, "text": ""}
  ]
 }
]
```

Required: `id`, `start`, `end`. Optional: `title`, `hook_line`, `hook_start`/`hook_end`
(2-3 s line played first), `hook_mode` (`"move"` default, or `"repeat"` to also keep it in
the body), `payoff_start` (defaults to the last transcript line starting inside the
pick), `caption_overrides`, `description`, `hashtags`, `comment_bait`, `why`, `scores`.

`caption_overrides` entries, applied on the source timeline before re-timing:
- `{"start": s, "end": e, "text": "..."}`: replace every ASR word whose midpoint is in
  [s, e) with these words, timed by character length across the replaced span. Use it
  for any mangled sentence and for speech whisper translated instead of transcribing
  (write the line in the speaker's own words and script).
- `{"w": index, "text": "..."}`: replace one word by its global index (from `words.tsv`
  or the `(wN)` marker in `transcript.txt`); `"text": ""` deletes it (fillers, "um").

Show the user the list before rendering, one line per pick:
`1. 2:12-2:51 (hook 2:30) "The one setting nobody changes" - costly mistake, ends on the fix`.
