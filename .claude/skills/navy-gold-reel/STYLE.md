# Navy & gold — style reference

Measured from the reference reel (720×1280) and scaled to the 1080×1920 canvas used by `navygold.py`.

## Colours

| Role | Hex |
|---|---|
| Background gradient top → bottom | `#1F2C45` → `#121D33` |
| Crumpled paper background (behind paper cards) | `#18263A` with lit/shaded facets and a vignette |
| Caption pill fill / glow | `#121F38` / `#4A5670` (soft, 70% transparent) |
| Gold (boxes, fills, ticks) | `#C9A149` (darker edge `#A9873A`) |
| Gold text (newest word, small labels) | `#D5B35C` |
| Inactive tab / its border | `#3F4858` / `#303C49` |
| Lines, ticks, track | `#576375`, `#2B3650` |
| Muted labels | `#798295` |
| Paper / ink / red marker | beige `#E9D9BA` / `#2A2118` / `#C0392B` |
| Cream (clock face, filled circles) | `#FAF3E6` |

## Layout (1080×1920)

- Video frame (`framed`): x 70, y 540, 940×700, corner radius 30, soft white glow (about 46 px). The face is kept in the upper-middle part of the crop.
- `small`: portrait frame 330×586 at (690, 250), with the graphic area centred at (330, 540).
- Graphic area above the frame: centre (540, 300), 940×380. Full-screen (`hidden`) area: centre (540, 760), 960×1100.
- Caption pill: centred at y 1392, height 100, radius 26, width fitted to the phrase (320–940). Text is right-aligned 44 px inside the pill.
- Paper cards: 860×900 torn ruled paper with a red margin on the right, tape at the top, and a drop shadow. They slide up 50 px and fade in over 0.3 s.

## Type

- UI and captions: **Tajawal** (Medium 56 for captions, ExtraBold for numbers and labels).
- Handwriting on paper: **Aref Ruqaa** (titles 86–130, lines 66–86).
- Quran: **Amiri** (60) inside ﴿ ﴾ on a paper strip with a thin gold inner border.

## Motion

- Captions: phrases of up to 6 words or 30 characters, broken at pauses over 0.7 s and at punctuation. Words appear one at a time; the newest is gold, earlier ones are white, and key words sit on a gold box.
- Every element fades in about 200 ms and out about 200 ms. Pops scale from 85–92% to 100% in 180–300 ms.
- Movement (knob, dot, clock hand, bar fill) eases over 600–800 ms, starting at the spoken word.
- Ticks follow their item by 0.35 s, and the underline draws in over 450 ms.
