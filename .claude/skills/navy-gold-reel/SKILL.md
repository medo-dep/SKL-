---
name: navy-gold-reel
description: Turn an Arabic talking-head video into a "navy & gold" motion-graphics reel — speaker in a glowing rounded frame on navy, word-by-word caption pill (newest word gold, key words on a gold box), and an animated graphic for each idea (timeline, month tabs, paper checklist, quote card, clock pie, bars, stairs, icons, football pitch...). Use when the user says "كحلي وذهبي", "navy gold", "موشن جرافيك مثل الفيديو", "سوّ نفس التصميم", or wants motion graphics planned from what the speaker says.
---

# Navy & Gold reel

Engine: `raw-to-reel/navygold.py` (ffmpeg + libass, no internet, no paid tools). Style values: `STYLE.md` next to this file.
A full example of the plan file: `scenes.example.json` next to this file.

## 1. Analyse (transcribe + cut)

```
python3 raw-to-reel/navygold.py analyze <video> --out raw-to-reel/workspace/jobs/<name>
```
Needs `ffmpeg`, `faster-whisper`, `opencv-python-headless` (same as Raw to Reel). It removes silences, fillers and
bad takes, and writes into the job folder:
- `clean.mp4`: the cut 9:16 video with no text on it
- `transcript.txt`: one line per sentence, `[start → end] text`, **times on the output timeline**
- `navygold.json`: every word with its start/end time (use it for exact `t` values) plus face positions

Extra options go in `--options '{"remove_fillers": false, "whisper_model": "medium"}'` (names: `DEFAULT_OPTIONS` in `editor.py`).

## 2. Plan the graphics → `<job>/scenes.json`

Read `transcript.txt` and the word times in `navygold.json`, then write `scenes.json`. A quick start without AI is
`python3 raw-to-reel/navygold.py auto <job>` (it handles durations/ages → timeline, month names → tabs, "أولاً/الخطوة الثانية" → points).
Planning it yourself is much better. Follow these rules:

- **One idea = one scene.** A scene usually lasts 2–8 s and starts **0.2–0.4 s before** the trigger word. Leave 30–50% of the video
  with no graphic (`framed` with nothing on top). These gaps are filled automatically.
- Each animated beat (a tab turning gold, a tick, a slice, the knob moving) gets its own `t` equal to the **start time of the word** it illustrates.
- Use the speaker's own words, shortened to 1–4 words. Use Arabic-Indic digits (١٣ ١٤ ١٥) when the speaker says numbers in Arabic.
- Don't repeat the same component twice in a row. Vary between `framed` (graphic above the frame), `small` (graphic on the left)
  and `hidden` (full screen on crumpled paper). `hidden` is for lists, quotes, the clock and the pitch, at most about 25% of the video.
- `keywords`: 1 key word for every 4–6 s (nouns or numbers the idea depends on). They get the gold box in the caption.
  Leave the list empty to let the engine pick them (`"auto_keywords": true`).

### Which component for what is said

| The speaker… | type | parameters |
|---|---|---|
| gives ages, years, durations, a change over time ("من ٢ إلى ٧ سنوات") | `timeline` | `unit`, `ticks` [numbers], `steps` [{t, value}] |
| names months, days or phases in order (رجب، شعبان، رمضان) | `month_tabs` | `labels`, `steps` [{t, active (index), above?, below?}] |
| names 2–5 specific numbers/dates (الأيام البيض ١٣ ١٤ ١٥) | `number_circles` | `values`, `fill_times` [t per value] |
| lists steps, a routine, advice (برنامجي اليومي) | `checklist` | `title`, `items` [{text, t, check?:true}] |
| quotes a saying, proverb or maxim | `quote` | `lines` [{text, t, color?:"gold"}], `arrow?` |
| stresses 2–4 phrases ("المهم… والأهم…") | `marker` | `phrases` [{text, t}] |
| describes a weekly schedule or plan | `table` | `title`, `columns`, `rows` [{label, checks [col idx], t}], `circled` [{col, t}] |
| says one concept word to remember (البركة) | `title_strip` | `text` |
| recites a Quran verse or hadith | `verse` | `text` (no brackets, they are added) |
| explains where time goes, or proportions (الصحبة، التلفون، الأكل…) | `pie_clock` | `slices` [{label, t, share 0–1}] |
| compares two quantities (الأكل vs النشاط), too much or too little | `compare_bars` | `bars` [{label, color?, steps [{t, value 0–1, color?:"red"}]}] |
| describes gradual progress or levels ("خطوة خطوة") | `steps` | `count`, `steps` [{t, step (0-based)}] |
| gives several numbers to compare, or growth | `bar_chart` | `columns` [{label, value (blocks), t}] |
| mentions time running out, a phone, being locked/addicted, day and night, food | `icon` | `name`: hourglass, phone (+`play`), lock, orbit, plate; `label?` |
| uses a sports or match analogy | `pitch` | `moves` [{t, x 0–1, y 0–1}], `timer?` {t, text:"90:00"} |
| says anything else that deserves a headline | `text` | `text`, `size?`, `color?` gold/white |

Scene fields: `start`, `end`, `graphic`, and optionally `frame` (`framed` | `small` | `full` | `hidden`) and `bg` (`plain` | `paper`).
Good defaults are chosen when you leave them out: paper types and the clock/pitch/steps/bars get `hidden`, everything else gets `framed`.

Shortcut without Claude: the "🎨 تصميم كحلي وذهبي" button on the Raw to Reel page runs analyze + auto + render, and shows `scenes.json` in an editable box with a "🔁 ارسم من جديد" button.

## 3. Render and check

```
python3 raw-to-reel/navygold.py render <job> --from 20 --to 35    # quick preview of one part → navygold_preview.mp4
python3 raw-to-reel/navygold.py render <job>                      # full video → <job>/navygold.mp4
```
After each render, grab frames in the middle of each scene
(`ffmpeg -ss T -i <job>/navygold.mp4 -frames:v 1 f.png`) and look at them. Check that text doesn't overflow the paper
or the screen, the graphic doesn't cover the face, and the beat times match the words. Fix `scenes.json` and render again.
Tell the user where the file is and list the scenes you used (time → graphic → why).
