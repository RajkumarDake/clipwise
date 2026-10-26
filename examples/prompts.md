# Example prompts

Type these to your coding agent once clipwise is installed. The agent loads `SKILL.md`,
runs the scripts, and stops once to show you the picks before it renders.

```text
Make 3 Shorts from https://www.youtube.com/watch?v=XXXXXXXXXXX
```

```text
Cut Reels from ~/Videos/podcast_ep12.mp4. Only use minutes 10-25. Keep captions in Spanish.
```

```text
Turn this talk into TikToks: https://example.org/files/keynote.mp4
Max 30 seconds each, green highlight, no music.
```

```text
Make 2 Shorts from ~/Downloads/interview.mov and add ~/Music/my_bed.mp3 as a quiet music bed.
```

```text
The speaker mixes Hindi and English. Make Shorts from talk.mp4 and write the captions in
Hinglish (Latin script) the way she speaks, not translated English.
```

## What the agent shows you before rendering

```text
Transcript ready (3:00, English). Proposed Shorts:
1. 1:05-1:50 (hook 1:15) "Is every flood caused by climate change?" - opens on a claim
   you don't expect from a climate scientist, ends on "more likely, not always".
2. 2:07-2:35 "How far ahead can we predict a flood?" - the question everyone has,
   ends on a concrete number (two or three weeks).
Render these? (about 1.5 min per clip on a 2-core CPU)
```

`picks.json` in this folder is the file the agent wrote for that list (source: the CC BY
Royal Society interview credited in the README).
