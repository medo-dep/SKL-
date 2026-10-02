"""Navy & gold motion-graphics reels ("كحلي وذهبي").

A reel style for Arabic talking-head videos: the speaker sits in a glowing rounded frame on a navy
background, words appear one by one in a navy caption pill (latest word gold, key words on a gold
box), and each idea gets its own animated graphic: paper cards, timelines, month tabs, a clock pie,
bars, stairs, icons...

Workflow (see .claude/skills/navy-gold-reel/SKILL.md):
    python navygold.py analyze VIDEO --out JOB          # transcribe + cut, writes JOB/transcript.txt
    (write JOB/scenes.json: which graphic, when)         # by hand or by Claude Code
    python navygold.py render JOB [--scenes FILE] [--from 0 --to 20]
    python navygold.py auto JOB                          # rough scenes.json from the transcript, no AI
"""

import argparse
import json
import math
import os
import re
import shutil
import subprocess

import designs
import editor

W, H, FPS = 1080, 1920, 30
LIB = os.path.join(editor.LIB_DIR, "navygold")


def ass_color(hex_rgb):
    r, g, b = int(hex_rgb[1:3], 16), int(hex_rgb[3:5], 16), int(hex_rgb[5:7], 16)
    return f"&H00{b:02X}{g:02X}{r:02X}&"


# palette measured from the reference video
HEX = {
    "bg_top": "#1F2C45", "bg_bottom": "#121D33", "pill": "#121F38", "pill_glow": "#4A5670",
    "gold": "#C9A149", "gold_text": "#D5B35C", "gold_dark": "#A9873A", "white": "#FFFFFF",
    "grey": "#798295", "line": "#576375", "tab": "#3F4858", "tab_top": "#303C49", "track": "#2B3650",
    "cream": "#FAF3E6", "ink": "#2A2118", "red": "#C0392B", "navy_text": "#1C2B48",
}
C = {k: ass_color(v) for k, v in HEX.items()}

# layout (1080x1920 canvas)
FRAMES = {
    "framed": (70, 540, 940, 700),   # x, y, w, h of the rounded video frame
    "small": (690, 250, 330, 586),   # portrait card on the right, graphic on the left
    "full": (0, 0, W, H),
}
PILL_Y = 1392
AREAS = {  # where a scene's graphic is drawn: centre x, centre y, width, height
    "framed": (540, 300, 940, 380),
    "full": (540, 300, 940, 380),
    "small": (330, 540, 540, 620),
    "hidden": (540, 760, 960, 1100),
}
PAPER_TYPES = {"checklist", "quote", "marker", "table"}
CENTER_TYPES = PAPER_TYPES | {"pie_clock", "pitch", "steps", "bar_chart"}  # want the frame hidden


# ---------------------------------------------------------------- ASS helpers

def t_ass(t):
    return editor.ass_time(t)


def ev(start, end, text, layer=0, style="D"):
    return f"Dialogue: {layer},{t_ass(start)},{t_ass(max(end, start + 0.05))},{style},,0,0,0,,{text}\n"


def shape(points):
    """Absolute-coordinate polygon (use with \\an7\\pos(0,0))."""
    (x0, y0), rest = points[0], points[1:]
    return f"m {x0:.0f} {y0:.0f} l " + " ".join(f"{x:.0f} {y:.0f}" for x, y in rest)


def rect(x, y, w, h):
    return shape([(x, y), (x + w, y), (x + w, y + h), (x, y + h)])


def rrect(x, y, w, h, r):
    r = min(r, w / 2, h / 2)
    k = r * 0.45
    return (f"m {x + r:.0f} {y:.0f} l {x + w - r:.0f} {y:.0f} b {x + w - k:.0f} {y:.0f} {x + w:.0f} {y + k:.0f} {x + w:.0f} {y + r:.0f} "
            f"l {x + w:.0f} {y + h - r:.0f} b {x + w:.0f} {y + h - k:.0f} {x + w - k:.0f} {y + h:.0f} {x + w - r:.0f} {y + h:.0f} "
            f"l {x + r:.0f} {y + h:.0f} b {x + k:.0f} {y + h:.0f} {x:.0f} {y + h - k:.0f} {x:.0f} {y + h - r:.0f} "
            f"l {x:.0f} {y + r:.0f} b {x:.0f} {y + k:.0f} {x + k:.0f} {y:.0f} {x + r:.0f} {y:.0f}")


def circle(cx, cy, r):
    k = r * 0.5523
    return (f"m {cx:.0f} {cy - r:.0f} b {cx + k:.0f} {cy - r:.0f} {cx + r:.0f} {cy - k:.0f} {cx + r:.0f} {cy:.0f} "
            f"b {cx + r:.0f} {cy + k:.0f} {cx + k:.0f} {cy + r:.0f} {cx:.0f} {cy + r:.0f} "
            f"b {cx - k:.0f} {cy + r:.0f} {cx - r:.0f} {cy + k:.0f} {cx - r:.0f} {cy:.0f} "
            f"b {cx - r:.0f} {cy - k:.0f} {cx - k:.0f} {cy - r:.0f} {cx:.0f} {cy - r:.0f}")


def line(x1, y1, x2, y2, th):
    a = math.atan2(y2 - y1, x2 - x1)
    dx, dy = -math.sin(a) * th / 2, math.cos(a) * th / 2
    return shape([(x1 + dx, y1 + dy), (x2 + dx, y2 + dy), (x2 - dx, y2 - dy), (x1 - dx, y1 - dy)])


def draw(start, end, path, color, layer=10, extra="", fade=(200, 200)):
    return ev(start, end, f"{{\\an7\\pos(0,0)\\p1\\bord0\\shad0\\1c{color}\\fad({fade[0]},{fade[1]}){extra}}}{path}", layer)


def text(start, end, x, y, s, size, color, layer=12, an=5, style="T", extra="", fade=(200, 200)):
    return ev(start, end, f"{{\\an{an}\\pos({x:.0f},{y:.0f})\\fs{size:.0f}\\1c{color}\\fad({fade[0]},{fade[1]}){extra}}}"
              + editor.ass_escape(str(s)), layer, style)


def header():
    styles = [
        f"T,Tajawal,46,{C['white']},{C['white']},&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,0,0,5,0,0,0,-1",
        f"Cap,Tajawal,56,{C['white']},{C['white']},{C['pill']},{C['pill']},0,0,0,0,100,100,0,0,3,9,0,6,0,0,0,-1",
        f"Ruq,Aref Ruqaa,56,{C['ink']},{C['ink']},&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,0,0,5,0,0,0,-1",
        f"Quran,Amiri,54,{C['ink']},{C['ink']},&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,5,0,0,0,-1",
        f"D,Tajawal,40,{C['gold']},{C['gold']},&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,-1",
    ]
    return ("[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\nWrapStyle: 2\n\n[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
            "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
            "MarginR, MarginV, Encoding\n" + "".join(f"Style: {s}\n" for s in styles)
            + "\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")


def text_w(s, size):
    return len(str(s)) * size * 0.5


def steps_of(params, key="steps"):
    return sorted(params.get(key) or [], key=lambda s: s["t"])


def intervals(items, t0, t1):
    """[(start, end, item, previous_item)] for items sorted by time, inside [t0, t1]."""
    out, prev = [], None
    for i, it in enumerate(items):
        a = max(t0, it["t"])
        b = min(t1, items[i + 1]["t"]) if i + 1 < len(items) else t1
        if b > a:
            out.append((a, b, it, prev))
        prev = it
    return out


# ---------------------------------------------------------------- components (ASS)

def c_timeline(p, t0, t1, area):
    """Number line with a gold knob sliding to each value as it is said (e.g. ages, years)."""
    cx, cy, aw, _ = area
    ticks = sorted(p.get("ticks") or [0, 10])
    lo, hi = ticks[0], ticks[-1] or 1
    xl, xr, y = cx - aw / 2 + 50, cx + aw / 2 - 50, cy - 10
    xv = lambda v: xl + (xr - xl) * (v - lo) / ((hi - lo) or 1)
    out = [draw(t0, t1, rect(xl, y - 2, xr - xl, 4), C["line"])]
    for v in ticks:
        out.append(draw(t0, t1, rect(xv(v) - 2, y - 14, 4, 28), C["line"]))
    steps = steps_of(p) or [{"t": t0, "value": lo}]
    pts = [{"t": t0, "value": lo}] + steps
    for a, b, it, prev in intervals(pts, t0, t1):
        v0 = (prev or it)["value"]
        x0, x1 = xv(v0), xv(it["value"])
        f0, f1 = (x0 - xl) / (xr - xl) * 100, (x1 - xl) / (xr - xl) * 100
        fade = (200 if a == t0 else 0, 200 if b == t1 else 0)
        out.append(ev(a, b, f"{{\\an4\\pos({xl:.0f},{y:.0f})\\p1\\bord0\\shad0\\1c{C['gold']}\\fscx{f0:.1f}"
                      f"\\t(0,600,\\fscx{f1:.1f})\\fad{fade}}}m 0 -4 l {xr - xl:.0f} -4 {xr - xl:.0f} 4 0 4", 11))
        out.append(ev(a, b, f"{{\\an5\\move({x0:.0f},{y:.0f},{x1:.0f},{y:.0f},0,600)\\p1\\bord0\\shad0\\1c{C['gold']}"
                      f"\\fad{fade}}}" + circle(0, 0, 20).replace("m 0 -20", "m 0 -20"), 12))
        for v in ticks:
            col = C["white"] if v == it["value"] else C["grey"]
            out.append(text(a, b, xv(v), y + 58, v, 40, col, 12, fade=fade))
    if p.get("unit"):
        out.append(text(t0, t1, xr, y + 118, p["unit"], 36, C["grey"], 12, an=6))
    return out


def c_month_tabs(p, t0, t1, area):
    """Row of tabs (e.g. رجب / شعبان / رمضان); the active one turns gold, with small labels above/below."""
    cx, cy, aw, _ = area
    labels = p.get("labels") or []
    n = max(1, len(labels))
    tw, th, gap = min(230, (aw - 40) / n - 24), 120, 24
    total = n * tw + (n - 1) * gap
    xs = [cx + total / 2 - tw / 2 - i * (tw + gap) for i in range(n)]  # first label on the right (RTL)
    steps = steps_of(p) or [{"t": t0, "active": 0}]
    out = []
    for a, b, it, prev in intervals(steps, t0, t1):
        fade = (200 if a == t0 else 0, 200 if b == t1 else 0)
        for i, lab in enumerate(labels):
            x = xs[i]
            active = i == it.get("active")
            pop = "\\fscx92\\fscy92\\t(0,180,\\fscx100\\fscy100)" if active and prev and prev.get("active") != i else ""
            fill = C["gold"] if active else C["tab"]
            out.append(ev(a, b, f"{{\\an5\\pos({x:.0f},{cy:.0f})\\p1\\bord3\\3c{C['tab_top'] if not active else C['gold_dark']}"
                          f"\\shad0\\1c{fill}\\fad{fade}{pop}}}" + rrect(0, 0, tw, th, 16), 10))
            out.append(text(a, b, x, cy, lab, 52, C["navy_text"] if active else C["white"], 12, fade=fade, extra=pop))
            if active and it.get("above"):
                out.append(text(a, b, x, cy - th / 2 - 26, it["above"], 30, C["gold_text"], 12, fade=fade))
            if active and it.get("below"):
                out.append(text(a, b, x, cy + th / 2 + 26, it["below"], 30, C["gold_text"], 12, fade=fade))
    return out


def c_number_circles(p, t0, t1, area):
    """Row of numbered circles (e.g. ١٣ ١٤ ١٥ — the white days); they fill cream when said."""
    cx, cy, aw, _ = area
    vals = p.get("values") or []
    n, r = max(1, len(vals)), 64
    gap = min(40, (aw - n * 2 * r) / max(1, n - 1))
    total = n * 2 * r + (n - 1) * gap
    out = []
    fill_t = p.get("fill_at", t0)
    for i, v in enumerate(vals):
        x = cx + total / 2 - r - i * (2 * r + gap)
        ti = (p.get("fill_times") or [fill_t] * n)[i]
        out.append(ev(t0, ti, f"{{\\an7\\pos(0,0)\\p1\\bord3\\3c{C['line']}\\1a&HFF&\\shad0\\fad(200,0)}}" + circle(x, cy, r), 10))
        out.append(text(t0, ti, x, cy, v, 44, C["grey"], 12, fade=(200, 0)))
        if ti < t1:
            out.append(ev(ti, t1, f"{{\\an7\\pos(0,0)\\p1\\bord5\\3c{C['gold']}\\shad0\\1c{C['cream']}\\fad(0,200)}}"
                          + circle(x, cy, r), 10))
            out.append(text(ti, t1, x, cy, v, 48, C["navy_text"], 12, fade=(0, 200),
                            extra="\\fscx70\\fscy70\\t(0,200,\\fscx100\\fscy100)"))
    return out


def paper_rows(p, area):
    cx, cy, aw, ah = area
    cw, ch = min(860, aw), min(900, ah)
    return cx - cw / 2, cy - ch / 2, cw, ch


def c_checklist(p, t0, t1, area):
    """Paper note: gold handwritten title, items appear with a checkbox that gets a gold tick + underline."""
    x0, y0, cw, ch = paper_rows(p, area)
    right, left = x0 + cw - 120, x0 + 110
    out = [text(t0, t1, right, y0 + 130, p.get("title", ""), 86, C["gold_dark"], 13, an=6, style="Ruq")]
    for i, it in enumerate(p.get("items") or []):
        y = y0 + 280 + i * 140
        ti = max(t0, it.get("t", t0))
        out.append(text(ti, t1, right, y, it["text"], 66, C["ink"], 13, an=6, style="Ruq", fade=(250, 200)))
        out.append(ev(ti, t1, f"{{\\an7\\pos(0,0)\\p1\\bord4\\3c{C['ink']}\\1a&HFF&\\shad0\\fad(200,200)}}"
                      + rrect(left - 34, y - 34, 68, 68, 8), 13))
        tick = it.get("check", True)
        if tick:
            tc = ti + float(it.get("check_delay", 0.35))
            out.append(ev(tc, t1, f"{{\\an7\\pos(0,0)\\p1\\bord0\\shad0\\1c{C['gold']}\\fad(120,200)}}"
                          + shape([(left - 26, y - 2), (left - 14, y - 12), (left - 2, y + 4), (left + 30, y - 34),
                                   (left + 40, y - 22), (left - 2, y + 24)]), 14))
            out.append(ev(tc, t1, f"{{\\an6\\pos({right:.0f},{y + 50:.0f})\\p1\\bord0\\shad0\\1c{C['gold']}\\fscx0"
                          f"\\t(0,450,\\fscx100)\\fad(0,200)}}" + rect(0, 0, cw - 280, 6), 13))
    return out


def c_quote(p, t0, t1, area):
    """Paper card with a quote / saying; each line can be ink or gold and appear at its own time."""
    x0, y0, cw, ch = paper_rows(p, area)
    out = []
    lines = p.get("lines") or []
    top = y0 + ch / 2 - (len(lines) - 1) * 60
    for i, ln in enumerate(lines):
        ti = max(t0, ln.get("t", t0))
        col = C["gold_dark"] if ln.get("color") == "gold" else C["ink"]
        out.append(text(ti, t1, x0 + cw / 2, top + i * 120, ln["text"], 86, col, 13, style="Ruq", fade=(300, 200),
                        extra="\\fscx90\\fscy90\\t(0,250,\\fscx100\\fscy100)"))
    if p.get("arrow"):
        ax, ay = x0 + cw * 0.62, y0 + ch - 140
        out.append(draw(t0 + 0.4, t1, shape([(ax - 18, ay + 80), (ax - 18, ay), (ax - 44, ay), (ax, ay - 50),
                                              (ax + 44, ay), (ax + 18, ay), (ax + 18, ay + 80)]), C["gold_dark"], 13))
    return out


def c_marker(p, t0, t1, area):
    """Paper card where phrases get swiped with a gold highlighter as they are said."""
    x0, y0, cw, ch = paper_rows(p, area)
    out = [draw(t0, t1, rect(x0 + 120, y0 + 110, cw - 240, 3), C["gold_dark"], 13)]
    for i, ph in enumerate(p.get("phrases") or []):
        ti = max(t0, ph.get("t", t0))
        y = y0 + 260 + i * 150
        wdt = min(cw - 200, text_w(ph["text"], 76) + 80)
        out.append(ev(ti, t1, f"{{\\an6\\pos({x0 + cw / 2 + wdt / 2:.0f},{y:.0f})\\p1\\bord0\\shad0\\1c{C['gold']}\\1a&H40&"
                      f"\\fscx0\\t(0,350,\\fscx100)\\fad(0,200)}}" + rect(0, -40, wdt, 80), 13))
        out.append(text(ti + 0.15, t1, x0 + cw / 2, y, ph["text"], 76, C["ink"], 14, style="Ruq", fade=(250, 200)))
    return out


def c_table(p, t0, t1, area):
    """Paper weekly programme: column headers, row labels, gold ticks; optional red circles on headers."""
    x0, y0, cw, ch = paper_rows(p, area)
    cols, rows = p.get("columns") or [], p.get("rows") or []
    out = [text(t0, t1, x0 + cw / 2, y0 + 110, p.get("title", ""), 66, C["gold_dark"], 13, style="Ruq")]
    label_w = 150
    cell = (cw - 160 - label_w) / max(1, len(cols))
    col_x = lambda j: x0 + cw - 80 - label_w - cell * (j + 0.5)
    for j, c in enumerate(cols):
        out.append(text(t0, t1, col_x(j), y0 + 220, c, 32, C["ink"], 13, style="Ruq"))
    for j in p.get("circled") or []:
        tj = j.get("t", t0) if isinstance(j, dict) else t0
        jj = j["col"] if isinstance(j, dict) else j
        out.append(ev(tj, t1, f"{{\\an5\\pos({col_x(jj):.0f},{y0 + 222:.0f})\\p1\\bord3\\3c{C['red']}\\1a&HFF&\\shad0"
                      f"\\fscx0\\fscy0\\t(0,250,\\fscx100\\fscy100)\\fad(0,200)}}" + rrect(0, 0, cell * 0.95, 52, 26), 14))
    for i, row in enumerate(rows):
        y = y0 + 330 + i * 120
        out.append(text(t0, t1, x0 + cw - 90, y, row["label"], 40, C["ink"], 13, an=6, style="Ruq"))
        for j in row.get("checks") or []:
            tj = row.get("t", t0) + 0.08 * j
            cx_ = col_x(j)
            out.append(ev(tj, t1, f"{{\\an7\\pos(0,0)\\p1\\bord0\\shad0\\1c{C['gold_dark']}\\fad(150,200)}}"
                          + shape([(cx_ - 20, y), (cx_ - 10, y - 8), (cx_, y + 4), (cx_ + 24, y - 24),
                                   (cx_ + 32, y - 14), (cx_, y + 20)]), 14))
    return out


def c_title_strip(p, t0, t1, area):
    """A torn paper strip with a big gold handwritten word (e.g. البركة)."""
    cx, cy, _, _ = area
    return [text(t0, t1, cx, cy, p.get("text", ""), 130, C["gold_dark"], 13, style="Ruq",
                 extra="\\fscx85\\fscy85\\t(0,300,\\fscx100\\fscy100)")]


def c_verse(p, t0, t1, area):
    """Quran verse on a paper strip with a thin double border."""
    cx, cy, aw, _ = area
    bw, bh = 900, 170
    out = [ev(t0, t1, f"{{\\an7\\pos(0,0)\\p1\\bord2\\3c{C['gold_dark']}\\1a&HFF&\\shad0\\fad(300,200)}}"
              + rrect(cx - bw / 2 + 26, cy - bh / 2 + 22, bw - 52, bh - 44, 6), 13)]
    out.append(text(t0, t1, cx, cy, f"﴿{p.get('text', '').strip('﴿﴾ ')}﴾", 60, C["ink"], 14, style="Quran", fade=(400, 200)))
    return out


def c_pie_clock(p, t0, t1, area):
    """Cream clock face; gold wedges get added as reasons are named and the hand sweeps to each one."""
    cx, cy = area[0], area[1] - 60
    r = 300
    out = [ev(t0, t1, f"{{\\an7\\pos(0,0)\\p1\\bord0\\shad6\\4c&H60000000&\\1c{C['cream']}\\fad(250,250)}}"
              + circle(cx, cy, r), 10)]
    for i in range(60):
        th = math.radians(i * 6)
        r1 = r - (34 if i % 5 == 0 else 18)
        out.append(draw(t0, t1, line(cx + r1 * math.sin(th), cy - r1 * math.cos(th), cx + (r - 8) * math.sin(th),
                                     cy - (r - 8) * math.cos(th), 5 if i % 5 == 0 else 2), C["ink"], 11))
    angle, prev_end = 0.0, 0.0
    slices = sorted(p.get("slices") or [], key=lambda s: s["t"])
    shares = [float(s.get("share", 1 / max(1, len(slices)))) for s in slices]
    for i, sl in enumerate(slices):
        a0, a1 = angle, angle + 360 * shares[i]
        angle = a1
        ti = max(t0, sl["t"])
        pts = [(cx, cy)] + [(cx + (r - 4) * math.sin(math.radians(a)), cy - (r - 4) * math.cos(math.radians(a)))
                            for a in [a0 + (a1 - a0) * k / 24 for k in range(25)]]
        col = C["gold"] if i % 2 == 0 else C["gold_dark"]
        out.append(ev(ti, t1, f"{{\\an7\\pos(0,0)\\p1\\bord0\\shad0\\1c{col}\\fad(250,250)}}" + shape(pts), 11))
        mid = math.radians((a0 + a1) / 2)
        out.append(text(ti + 0.1, t1, cx + r * 0.6 * math.sin(mid), cy - r * 0.6 * math.cos(mid), sl["label"], 42,
                        C["navy_text"], 13))
        nxt = slices[i + 1]["t"] if i + 1 < len(slices) else t1
        out.append(ev(ti, nxt, f"{{\\an4\\pos({cx:.0f},{cy:.0f})\\org({cx:.0f},{cy:.0f})\\p1\\bord0\\shad0\\1c{C['navy_text']}"
                      f"\\frz{90 - prev_end:.1f}\\t(0,700,\\frz{90 - a1:.1f})\\fad({200 if i == 0 else 0},{200 if nxt == t1 else 0})}}"
                      f"m 0 -5 l {r * 0.82:.0f} -3 {r * 0.82:.0f} 3 0 5", 14))
        prev_end = a1
    out.append(draw(t0, t1, circle(cx, cy, 14), C["navy_text"], 15))
    if not slices:
        out.append(ev(t0, t1, f"{{\\an4\\pos({cx:.0f},{cy:.0f})\\org({cx:.0f},{cy:.0f})\\p1\\bord0\\shad0\\1c{C['navy_text']}"
                      f"\\frz60\\t(0,{int((t1 - t0) * 1000)},\\frz-300)\\fad(200,200)}}m 0 -5 l {r * 0.8:.0f} -3 {r * 0.8:.0f} 3 0 5", 14))
    return out


def c_compare_bars(p, t0, t1, area):
    """Labelled progress bars (e.g. الأكل vs النشاط) that grow — a bar can turn red when it overflows."""
    cx, cy, aw, _ = area
    bars = p.get("bars") or []
    xl, xr = cx - aw / 2 + 40, cx + aw / 2 - 200
    out = []
    for i, bar in enumerate(bars):
        y = cy - (len(bars) - 1) * 50 + i * 100
        out.append(text(t0, t1, cx + aw / 2 - 30, y, bar["label"], 46, C["white"], 12, an=6))
        out.append(draw(t0, t1, rrect(xl, y - 14, xr - xl, 28, 14), C["track"], 10))
        steps = steps_of(bar) or [{"t": t0, "value": bar.get("value", 0.5)}]
        for a, b, it, prev in intervals([{"t": t0, "value": 0}] + steps, t0, t1):
            v0, v1 = (prev or it)["value"] * 100, it["value"] * 100
            col = C.get(it.get("color") or bar.get("color", "gold"), C["gold"])
            fade = (200 if a == t0 else 0, 200 if b == t1 else 0)
            out.append(ev(a, b, f"{{\\an4\\pos({xl:.0f},{y:.0f})\\p1\\bord0\\shad0\\1c{col}\\fscx{v0:.1f}"
                          f"\\t(0,700,\\fscx{v1:.1f})\\fad{fade}}}" + rrect(0, -14, xr - xl, 28, 14), 11))
    return out


def c_steps(p, t0, t1, area):
    """Stairs going up; a gold dot climbs to each step as it is reached."""
    cx, cy, aw, ah = area
    n = int(p.get("count", 8))
    sw, sh = min(110, (aw - 100) / n), min(70, (ah - 160) / n)
    x0, base = cx - n * sw / 2, cy + n * sh / 2
    pos = lambda i: (x0 + i * sw + sw / 2, base - (i + 1) * sh)
    climbs = steps_of(p) or [{"t": t0, "step": n - 1}]
    out = []
    for i in range(n):
        x, y = pos(i)
        out.append(draw(t0, t1, rect(x - sw / 2 + 4, y, 2, base - y), C["line"], 10))
        reached = next((c["t"] for c in climbs if c["step"] >= i), None)
        out.append(draw(t0, reached or t1, rect(x - sw / 2 + 6, y - 3, sw - 12, 8), C["line"], 11, fade=(200, 0)))
        if reached is not None and reached < t1:
            out.append(draw(reached, t1, rect(x - sw / 2 + 6, y - 4, sw - 12, 10), C["gold"], 11, fade=(150, 200)))
    for a, b, it, prev in intervals([{"t": t0, "step": 0}] + climbs, t0, t1):
        (xa, ya), (xb, yb) = pos((prev or it)["step"]), pos(it["step"])
        fade = (200 if a == t0 else 0, 200 if b == t1 else 0)
        out.append(ev(a, b, f"{{\\an5\\move({xa:.0f},{ya - 26:.0f},{xb:.0f},{yb - 26:.0f},0,700)\\p1\\bord0\\shad0"
                      f"\\1c{C['gold']}\\fad{fade}}}" + circle(0, 0, 20), 12))
    return out


def c_bar_chart(p, t0, t1, area):
    """Columns 1..n made of gold blocks that stack up as values are given."""
    cx, cy, aw, ah = area
    vals = p.get("columns") or []
    n = max(1, len(vals))
    colw = min(90, (aw - 60) / n)
    x0, base, bh = cx - n * colw / 2, cy + ah / 2 - 140, 22
    out = [draw(t0, t1, rect(x0, base + 6, n * colw, 3), C["line"], 10)]
    for j, col in enumerate(vals):
        x = x0 + j * colw
        out.append(text(t0, t1, x + colw / 2, base + 40, col.get("label", j + 1), 30, C["grey"], 12))
        tj = max(t0, col.get("t", t0))
        out.append(draw(t0, tj if col.get("value") else t1, rect(x + 10, base - 6, colw - 20, 6), C["line"], 10))
        for k in range(int(col.get("value", 0))):
            out.append(ev(tj + 0.06 * k, t1, f"{{\\an7\\pos(0,0)\\p1\\bord0\\shad0\\1c{C['gold']}\\fad(120,200)}}"
                          + rect(x + 8, base - (k + 1) * (bh + 6), colw - 16, bh), 11))
    return out


def c_icon(p, t0, t1, area):
    """Simple gold line icons: hourglass, phone, lock, orbit (sun/moon around), plate."""
    cx, cy = area[0], area[1]
    name, g = p.get("name", "hourglass"), C["gold"]
    out = []
    if name == "hourglass":
        out.append(draw(t0, t1, rect(cx - 70, cy - 120, 140, 12), g, 11))
        out.append(draw(t0, t1, rect(cx - 70, cy + 108, 140, 12), g, 11))
        out.append(ev(t0, t1, f"{{\\an7\\pos(0,0)\\p1\\bord5\\3c{g}\\1a&HFF&\\shad0\\fad(200,200)}}"
                      + shape([(cx - 55, cy - 108), (cx + 55, cy - 108), (cx + 6, cy), (cx + 55, cy + 108),
                               (cx - 55, cy + 108), (cx - 6, cy)]), 11))
        out.append(ev(t0, t1, f"{{\\an7\\pos(0,0)\\p1\\bord0\\shad0\\1c{g}\\fad(200,200)\\clip(0,{cy + 20:.0f},{W},{cy + 108:.0f})"
                      f"\\t(0,{int((t1 - t0) * 1000)},\\clip(0,{cy + 60:.0f},{W},{cy + 108:.0f}))}}"
                      + shape([(cx - 4, cy + 30), (cx + 4, cy + 30), (cx + 44, cy + 104), (cx - 44, cy + 104)]), 12))
    elif name == "phone":
        out.append(ev(t0, t1, f"{{\\an7\\pos(0,0)\\p1\\bord5\\3c{C['white']}\\shad0\\1c{C['pill']}\\fad(200,200)}}"
                      + rrect(cx - 80, cy - 150, 160, 300, 26), 11))
        if p.get("play"):
            out.append(draw(t0, t1, shape([(cx - 18, cy - 26), (cx + 26, cy), (cx - 18, cy + 26)]), g, 12))
    elif name == "lock":
        out.append(ev(t0, t1, f"{{\\an7\\pos(0,0)\\p1\\bord14\\3c{g}\\1a&HFF&\\shad0\\fad(200,200)}}"
                      + rrect(cx - 50, cy - 140, 100, 150, 50), 11))
        out.append(draw(t0, t1, rrect(cx - 85, cy - 40, 170, 140, 18), g, 12))
        out.append(draw(t0, t1, circle(cx, cy + 20, 16), C["pill"], 13))
    elif name == "orbit":
        r = 120
        for i in range(36):
            th = math.radians(i * 10)
            out.append(draw(t0, t1, circle(cx + r * math.cos(th), cy + r * math.sin(th), 2.5), C["grey"], 10))
        dur = int((t1 - t0) * 1000)
        out.append(ev(t0, t1, f"{{\\an5\\pos({cx:.0f},{cy:.0f})\\org({cx:.0f},{cy:.0f})\\p1\\bord0\\shad0\\1c{g}"
                      f"\\fad(200,200)\\t(0,{dur},\\frz-180)}}" + circle(r, 0, 18).replace("m", "m", 1), 12))
        out.append(ev(t0, t1, f"{{\\an5\\pos({cx:.0f},{cy:.0f})\\org({cx:.0f},{cy:.0f})\\p1\\bord0\\shad0\\1c{C['white']}"
                      f"\\fad(200,200)\\frz180\\t(0,{dur},\\frz0)}}" + circle(r, 0, 14), 12))
    elif name == "plate":
        out.append(ev(t0, t1, f"{{\\an7\\pos(0,0)\\p1\\bord5\\3c{g}\\shad0\\1c{C['cream']}\\fad(200,200)}}"
                      + circle(cx, cy, 90).replace("m", "m"), 11))
        for dx, dy in ((-24, -10), (10, -18), (0, 14), (26, 8)):
            out.append(draw(t0, t1, circle(cx + dx, cy + dy, 16), g, 12))
    if p.get("label"):
        out.append(text(t0, t1, cx, cy + 200, p["label"], 40, C["white"], 12))
    return out


def c_pitch(p, t0, t1, area):
    """Football pitch (an analogy): line drawing, a dot that moves to positions, optional timer pill."""
    cx, cy, aw, ah = area
    pw, ph = min(760, aw - 80), min(1000, ah - 60)
    x0, y0 = cx - pw / 2, cy - ph / 2
    lw = C["white"]
    out = [ev(t0, t1, f"{{\\an7\\pos(0,0)\\p1\\bord3\\3c{lw}\\1a&HFF&\\shad0\\fad(250,200)}}" + rect(x0, y0, pw, ph), 10)]
    out.append(draw(t0, t1, rect(x0, cy - 1.5, pw, 3), lw, 10))
    out.append(ev(t0, t1, f"{{\\an7\\pos(0,0)\\p1\\bord3\\3c{lw}\\1a&HFF&\\shad0\\fad(250,200)}}" + circle(cx, cy, 100), 10))
    for yb in (y0, y0 + ph - 150):
        out.append(ev(t0, t1, f"{{\\an7\\pos(0,0)\\p1\\bord3\\3c{lw}\\1a&HFF&\\shad0\\fad(250,200)}}"
                      + rect(cx - 160, yb, 320, 150), 10))
    moves = [{"t": t0, "x": 0.5, "y": 0.5}] + steps_of(p, "moves")
    for a, b, it, prev in intervals(moves, t0, t1):
        pa, pb = prev or it, it
        fade = (200 if a == t0 else 0, 200 if b == t1 else 0)
        out.append(ev(a, b, f"{{\\an5\\move({x0 + pa['x'] * pw:.0f},{y0 + pa['y'] * ph:.0f},{x0 + pb['x'] * pw:.0f},"
                      f"{y0 + pb['y'] * ph:.0f},0,800)\\p1\\bord0\\shad0\\1c{C['gold'] if it.get('gold', True) else lw}"
                      f"\\fad{fade}}}" + circle(0, 0, 22), 12))
    if p.get("timer"):
        tt = p["timer"]
        ty = y0 - 70
        out.append(draw(tt.get("t", t0), t1, rrect(cx - 100, ty - 38, 200, 76, 16), C["gold"], 12))
        out.append(text(tt.get("t", t0), t1, cx, ty, tt.get("text", "90:00"), 48, C["navy_text"], 13))
    return out


def c_text(p, t0, t1, area):
    """Plain big text in the graphic area (gold or white)."""
    return [text(t0, t1, area[0], area[1], p.get("text", ""), p.get("size", 80),
                 C["gold_text"] if p.get("color", "gold") == "gold" else C["white"], 12,
                 extra="\\fscx85\\fscy85\\t(0,250,\\fscx100\\fscy100)")]


COMPONENTS = {
    "timeline": c_timeline, "month_tabs": c_month_tabs, "number_circles": c_number_circles,
    "checklist": c_checklist, "quote": c_quote, "marker": c_marker, "table": c_table,
    "title_strip": c_title_strip, "verse": c_verse, "pie_clock": c_pie_clock, "compare_bars": c_compare_bars,
    "steps": c_steps, "bar_chart": c_bar_chart, "icon": c_icon, "pitch": c_pitch, "text": c_text,
}


# ---------------------------------------------------------------- captions

def caption_chunks(words, max_chars=30, max_words=6):
    chunks, cur = [], []
    for w in words:
        if editor.normalize_word(w["text"]) in editor.FILLERS:
            continue
        brk = cur and (w["start"] - cur[-1]["end"] > 0.7 or re.search(r"[.!?؟،,]$", cur[-1]["text"])
                       or len(" ".join(x["text"] for x in cur + [w])) > max_chars or len(cur) >= max_words)
        if brk:
            chunks.append(cur)
            cur = []
        cur.append(w)
    if cur:
        chunks.append(cur)
    return chunks


def caption_events(words, keywords):
    """Navy pill per phrase; words appear one at a time, the newest gold, key words on a gold box."""
    keys = {editor.stem(k) for k in keywords if editor.stem(k)}
    out = []
    for chunk in caption_chunks(words):
        s0 = chunk[0]["start"]
        s1 = max(chunk[-1]["end"] + 0.25, s0 + 0.6)
        full = " ".join(w["text"] for w in chunk)
        pw = min(940, max(320, text_w(full, 56) + 110))
        x, y = 540 - pw / 2, PILL_Y - 50
        out.append(ev(s0, s1, f"{{\\an7\\pos(0,0)\\p1\\bord0\\shad0\\1c{C['pill_glow']}\\1a&H70&\\blur22\\fad(120,120)}}"
                      + rrect(x - 14, y - 14, pw + 28, 128, 34), 20))
        out.append(ev(s0, s1, f"{{\\an7\\pos(0,0)\\p1\\bord0\\shad0\\1c{C['pill']}\\fad(120,120)}}" + rrect(x, y, pw, 100, 26), 21))
        for i, w in enumerate(chunk):
            a = s0 if i == 0 else w["start"]
            b = chunk[i + 1]["start"] if i + 1 < len(chunk) else s1
            if b - a < 0.02:
                continue
            parts = []
            for j, x_ in enumerate(chunk[:i + 1]):
                key = editor.stem(x_["text"]) in keys
                box = C["gold"] if key else C["pill"]
                col = C["gold_text"] if (j == i and not key) else C["white"]
                parts.append(f"{{\\1c{col}\\3c{box}\\4c{box}}}{editor.ass_escape(x_['text'])}")
            out.append(ev(a, b, f"{{\\an6\\pos({540 + pw / 2 - 44:.0f},{PILL_Y:.0f})\\fad({100 if i == 0 else 0},"
                          f"{100 if i == len(chunk) - 1 else 0})}}" + " ".join(parts), 22, "Cap"))
    return out


# ---------------------------------------------------------------- scenes

def load_scenes(path, total):
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    scenes = sorted(doc.get("scenes") or [], key=lambda s: s["start"])
    for sc in scenes:
        g = sc.get("graphic") or {}
        sc.setdefault("frame", "hidden" if g.get("type") in CENTER_TYPES else "framed")
        sc.setdefault("bg", "paper" if g.get("type") in PAPER_TYPES | {"pie_clock", "pitch"} else "plain")
        sc["end"] = min(float(sc["end"]), total)
    # fill the gaps with the default framed look
    timeline, t = [], 0.0
    for sc in scenes:
        if sc["start"] > t + 0.05:
            timeline.append({"start": t, "end": sc["start"], "frame": "framed", "bg": "plain"})
        timeline.append(sc)
        t = max(t, sc["end"])
    if t < total - 0.05:
        timeline.append({"start": t, "end": total, "frame": "framed", "bg": "plain"})
    return doc, timeline


def gradient_bg():
    import numpy as np
    top, bot = [int(HEX["bg_top"][i:i + 2], 16) for i in (5, 3, 1)], [int(HEX["bg_bottom"][i:i + 2], 16) for i in (5, 3, 1)]
    k = np.linspace(0, 1, H)[:, None, None]
    img = np.array(top, np.float32) * (1 - k) + np.array(bot, np.float32) * k
    img = np.repeat(img, W, axis=1)
    img += np.random.default_rng(3).normal(0, 1.2, img.shape)  # a little grain against banding
    return np.clip(img, 0, 255).astype(np.uint8)


def corner_patch(bg_path, geom, r):
    """Canvas-sized PNG that repaints the background outside the frame's rounded corners."""
    import cv2
    import numpy as np
    bg = cv2.imread(bg_path)
    x, y, w, h = geom
    out = np.zeros((H, W, 4), np.uint8)
    inside = designs.rounded_mask(w, h, r)
    region = out[y:y + h, x:x + w]
    region[..., :3] = bg[y:y + h, x:x + w]
    region[..., 3] = 255 - inside
    return out


def build_assets():
    paths = {
        "bg_plain": designs.ensure("bg_plain.png", gradient_bg, LIB),
        "bg_paper": designs.ensure("bg_paper.png", lambda: designs.crumpled(W, H, base=(58, 38, 24)), LIB),
        "paper": designs.ensure("paper_card.png", lambda: designs.with_shadow(designs.torn_paper(860, 900, lines=6)), LIB),
        "strip": designs.ensure("paper_strip.png", lambda: designs.with_shadow(designs.torn_paper(900, 190, lines=0, seed=8),
                                                                              spread=20), LIB),
    }
    for mode in ("framed", "small"):
        x, y, w, h = FRAMES[mode]
        paths[f"glow_{mode}"] = designs.ensure(f"glow_{mode}.png", lambda w=w, h=h: designs.glow(w, h, 30, 46, strength=0.42), LIB)
        for bg in ("plain", "paper"):
            paths[f"corners_{mode}_{bg}"] = designs.ensure(
                f"corners_{mode}_{bg}.png", lambda bg=bg, x=x, y=y, w=w, h=h: corner_patch(paths[f"bg_{bg}"], (x, y, w, h), 30), LIB)
    return paths


def face_rows(faces, a, b, default=0.42):
    ys = [box[1] / H for t, box in faces if a <= t <= b and box]
    return sorted(ys)[len(ys) // 2] if ys else default


def between(spans):
    return "+".join(f"between(t,{a:.2f},{b:.2f})" for a, b in spans) or "0"


def render(job_dir, scenes_path=None, t_from=None, t_to=None, log=print):
    with open(os.path.join(job_dir, "navygold.json"), encoding="utf-8") as f:
        meta = json.load(f)
    clean, words, faces = os.path.join(job_dir, meta["clean"]), meta["words"], meta["faces"]
    total = editor.probe(clean)["duration"]
    doc, timeline = load_scenes(scenes_path or os.path.join(job_dir, "scenes.json"), total)
    assets = build_assets()
    fonts = os.path.join(job_dir, "fonts")
    os.makedirs(fonts, exist_ok=True)
    for f in ("Tajawal-Medium.ttf", "Tajawal-ExtraBold.ttf", "ArefRuqaa-Bold.ttf", "Amiri-Regular.ttf"):
        shutil.copy(os.path.join(editor.HERE, "fonts", f), fonts)

    # ---- ASS: graphics + captions
    keywords = doc.get("keywords") or []
    if doc.get("auto_keywords", not keywords):
        hl = editor.find_highlights(words, [], editor.normalize_options({"highlight_density": "many"}), total, [])
        keywords = keywords + [h["word"] for h in hl]
    events = caption_events(words, keywords)
    for sc in timeline:
        g = sc.get("graphic")
        if not g or g.get("type") not in COMPONENTS:
            continue
        area = AREAS[sc["frame"] if sc["frame"] in AREAS else "framed"]
        if g.get("type") in {"title_strip", "verse"} and sc["frame"] != "hidden":
            area = (540, 300, 940, 190)
        events += COMPONENTS[g["type"]](g, sc["start"], sc["end"], area)
    with open(os.path.join(job_dir, "navygold.ass"), "w", encoding="utf-8") as f:
        f.write(header() + "".join(events))

    # ---- ffmpeg graph: background, frames, paper cards, ASS
    cmd = ["ffmpeg", "-y", "-v", "error"]
    if t_from is not None:
        cmd += ["-ss", str(t_from)]
    if t_to is not None:
        cmd += ["-to", str(t_to)]
    cmd += ["-i", clean]
    n_in = 1
    chain = []

    def still(path):
        nonlocal n_in
        cmd.extend(["-loop", "1", "-framerate", str(FPS), "-t", f"{total:.2f}", "-i", path])
        n_in += 1
        return n_in - 1

    off = t_from or 0.0  # with -ss the clean video's timestamps start at 0; the stills don't
    shift = lambda spans: [(a - off, b - off) for a, b in spans]
    src = still(assets["bg_plain"])
    chain.append(f"[{src}:v]trim=start={off:.2f},setpts=PTS-STARTPTS,format=yuv420p[base]")
    cur = "base"
    paper_bg = [(s["start"], s["end"]) for s in timeline if s.get("bg") == "paper"]
    if paper_bg:
        src = still(assets["bg_paper"])
        chain.append(f"[{src}:v]trim=start={off:.2f},setpts=PTS-STARTPTS[pbg]")
        chain.append(f"[{cur}][pbg]overlay=enable='{between(shift(paper_bg))}'[b1]")
        cur = "b1"
    for mode in ("framed", "small"):
        spans = [(s["start"], s["end"]) for s in timeline if s["frame"] == mode]
        if spans:
            x, y, w, h = FRAMES[mode]
            src = still(assets[f"glow_{mode}"])
            chain.append(f"[{src}:v]trim=start={off:.2f},setpts=PTS-STARTPTS[g{mode}]")
            chain.append(f"[{cur}][g{mode}]overlay=x={x - 46}:y={y - 46}:enable='{between(shift(spans))}'[c{mode}]")
            cur = f"c{mode}"
    vis = [s for s in timeline if s["frame"] in FRAMES]
    if vis:
        chain.append(f"[0:v]split={len(vis)}" + "".join(f"[v{i}]" for i in range(len(vis))))
    for i, sc in enumerate(vis):
        a, b = sc["start"] - off, sc["end"] - off
        if b <= 0:
            chain.append(f"[v{i}]nullsink")
            continue
        x, y, w, h = FRAMES[sc["frame"]]
        if sc["frame"] == "full":
            crop = "null"
        else:
            ch_ = int(W * h / w) // 2 * 2 if w > h * 0.9 else H
            cw_ = W if w > h * 0.9 else int(H * w / h) // 2 * 2
            fy = face_rows(faces, sc["start"], sc["end"])
            cy_ = int(min(max(fy * H - ch_ * 0.42, 0), H - ch_))
            crop = f"crop={cw_}:{ch_}:(iw-{cw_})/2:{cy_},scale={w}:{h}"
        chain.append(f"[v{i}]trim=start={max(0, a):.3f}:end={b:.3f},setpts=PTS-STARTPTS+{max(0, a):.3f}/TB,{crop}[f{i}]")
        chain.append(f"[{cur}][f{i}]overlay=x={x}:y={y}:eof_action=pass:enable='between(t,{max(0, a):.3f},{b:.3f})'[o{i}]")
        cur = f"o{i}"
    for mode in ("framed", "small"):
        for bg in ("plain", "paper"):
            spans = [(s["start"], s["end"]) for s in timeline if s["frame"] == mode and s.get("bg", "plain") == bg]
            if spans:
                src = still(assets[f"corners_{mode}_{bg}"])
                chain.append(f"[{src}:v]trim=start={off:.2f},setpts=PTS-STARTPTS[k{mode}{bg}]")
                chain.append(f"[{cur}][k{mode}{bg}]overlay=enable='{between(shift(spans))}'[q{mode}{bg}]")
                cur = f"q{mode}{bg}"
    for i, sc in enumerate(timeline):
        g = sc.get("graphic") or {}
        kind = "paper" if g.get("type") in PAPER_TYPES else "strip" if g.get("type") in {"title_strip", "verse"} else None
        if not kind:
            continue
        a, b = sc["start"] - off, sc["end"] - off
        if b <= 0:
            continue
        area = AREAS[sc["frame"] if sc["frame"] in AREAS else "framed"]
        if kind == "strip" and sc["frame"] != "hidden":
            area = (540, 300, 940, 190)
        if kind == "paper":
            x0, y0, cw, ch = paper_rows(g, area)
            px, py, pad = x0, y0, 28
        else:
            px, py, pad = area[0] - 450, area[1] - 95, 20
        src = still(assets[kind])
        chain.append(f"[{src}:v]trim=start={off:.2f},setpts=PTS-STARTPTS,format=rgba,"
                     f"fade=in:st={max(0, a):.2f}:d=0.3:alpha=1,fade=out:st={max(0, b - 0.25):.2f}:d=0.25:alpha=1[p{i}]")
        chain.append(f"[{cur}][p{i}]overlay=x={px - pad:.0f}:y='{py - pad:.0f}+50*max(0,1-(t-{max(0, a):.2f})/0.35)'"
                     f":enable='between(t,{max(0, a):.2f},{b:.2f})'[r{i}]")
        cur = f"r{i}"
    ass = "navygold.ass"
    if off:
        shifted = os.path.join(job_dir, "navygold_shift.ass")
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-itsoffset", str(-off), "-i", os.path.join(job_dir, ass), shifted],
                       check=True)
        ass = "navygold_shift.ass"
    chain.append(f"[{cur}]ass={ass}:fontsdir=fonts,format=yuv420p[vo]")
    out = os.path.join(job_dir, "navygold.mp4" if t_from is None and t_to is None else "navygold_preview.mp4")
    cmd += ["-filter_complex", ";".join(chain), "-map", "[vo]", "-map", "0:a?", "-c:v", "libx264", "-preset", "medium",
            "-crf", "19", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
            "-shortest", out]
    log("رسم التصميم الكحلي والذهبي...")
    editor.run(cmd, cwd=job_dir)
    log(f"✅ {out}")
    return out


# ---------------------------------------------------------------- analyse / auto plan

CLEAN_OVERRIDES = dict(aspect="9:16", captions=False, english_subs=False, thumbnail=False, end_card=False,
                       motion_highlights=False, stickers=False, section_titles=False, text_hook=False,
                       progress_bar=False, logo=False, zoom_cuts=False, transition="none", split_reels=False,
                       post_text=False, translate_to="", also_4x5=False, also_1x1=False, also_16x9=False,
                       speed=1.0, broll=False, pexels=False, caption_position="lower")


def analyze(video, job_dir, options=None, assets=None, edits=None, log=print):
    opts = dict(options or {})
    opts.setdefault("language", "ar")
    plan = editor.analyze(job_dir, [os.path.abspath(video)], opts, log)
    return prepare(job_dir, plan, opts, assets, edits, log)


def prepare(job_dir, plan, opts, assets=None, edits=None, log=print):
    """Cut a clean 9:16 video (no text) and save the output-timeline words and face positions."""
    res = editor.render_plan(job_dir, plan, {**opts, **CLEAN_OVERRIDES}, assets, edits, log=log)
    os.replace(os.path.join(job_dir, res["final"]), os.path.join(job_dir, "clean.mp4"))
    segments = editor.apply_edits(plan["segments"], edits or {})
    words = editor.timeline_words(segments)
    faces, offset = [], 0.0
    for seg in segments:
        _, sf = editor.reframe(plan["tracks"].get(seg["file"]), seg, W, H, False, True)
        faces += [(offset + t, b) for t, b in sf]
        offset += editor.seg_len(seg)
    meta = {"clean": "clean.mp4", "words": words, "faces": faces, "duration": offset}
    with open(os.path.join(job_dir, "navygold.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)
    with open(os.path.join(job_dir, "transcript.txt"), "w", encoding="utf-8") as f:
        for s in sentences(words):
            f.write(f"[{s['start']:7.2f} → {s['end']:7.2f}] {s['text']}\n")
    log(f"✅ التفريغ في {os.path.join(job_dir, 'transcript.txt')}")
    return meta


def sentences(words, gap=0.6):
    out, cur = [], []
    for w in words:
        if cur and (w["start"] - cur[-1]["end"] > gap or re.search(r"[.!?؟]$", cur[-1]["text"])):
            out.append(cur)
            cur = []
        cur.append(w)
    if cur:
        out.append(cur)
    return [{"start": round(c[0]["start"], 2), "end": round(c[-1]["end"], 2), "text": " ".join(w["text"] for w in c)}
            for c in out]


MONTHS = ["محرم", "صفر", "ربيع", "جمادى", "رجب", "شعبان", "رمضان", "شوال", "ذو القعدة", "ذو الحجة"]
TIME_UNITS = {"سنه": "سنوات", "سنوات": "سنوات", "سنين": "سنوات", "عام": "سنوات", "اعوام": "سنوات",
              "شهر": "أشهر", "اشهر": "أشهر", "شهور": "أشهر", "يوم": "أيام", "ايام": "أيام",
              "ساعه": "ساعات", "ساعات": "ساعات", "دقيقه": "دقائق", "دقايق": "دقائق", "دقائق": "دقائق"}


def auto_scenes(job_dir):
    """A first scenes.json without AI: timelines for ages/durations, month tabs, checklists for steps."""
    with open(os.path.join(job_dir, "navygold.json"), encoding="utf-8") as f:
        meta = json.load(f)
    words, scenes = meta["words"], []
    stems = [editor.stem(w["text"]) for w in words]
    # numbers followed by a time unit -> one timeline over the whole run of mentions
    hits = []
    for i, w in enumerate(words):
        num = editor.parse_number(w["text"]) or editor.COUNTS.get(stems[i])
        unit = TIME_UNITS.get(stems[i + 1]) if i + 1 < len(words) else None
        if num is not None and unit:
            hits.append({"t": w["start"], "value": num, "unit": unit})
    if len(hits) >= 2:
        ticks = sorted({0} | {h["value"] for h in hits})
        scenes.append({"start": max(0, hits[0]["t"] - 0.5), "end": hits[-1]["t"] + 3,
                       "graphic": {"type": "timeline", "unit": hits[0]["unit"], "ticks": ticks,
                                   "steps": [{"t": h["t"], "value": h["value"]} for h in hits]}})
    # months
    month_hits = [(w["start"], m) for w, s in zip(words, stems) for m in MONTHS if editor.stem(m) == s]
    if month_hits:
        labels = list(dict.fromkeys(m for _, m in month_hits))
        order = sorted(labels, key=MONTHS.index)
        scenes.append({"start": max(0, month_hits[0][0] - 0.3), "end": month_hits[-1][0] + 3,
                       "graphic": {"type": "month_tabs", "labels": order,
                                   "steps": [{"t": t, "active": order.index(m)} for t, m in month_hits]}})
    # "أولاً / الخطوة الثانية..." -> checklist
    secs = editor.find_sections(words)
    if len(secs) >= 2 and secs[-1]["start"] - secs[0]["start"] > 12:  # far apart: a short title per point
        for s in secs:
            scenes.append({"start": s["start"], "end": s["start"] + 2.6,
                           "graphic": {"type": "text", "text": s["label"], "size": 96}})
    elif len(secs) >= 2:
        scenes.append({"start": secs[0]["start"], "end": secs[-1]["start"] + 3.5, "frame": "hidden", "bg": "paper",
                       "graphic": {"type": "checklist", "title": "أهم النقاط",
                                   "items": [{"text": s["sub"] or s["label"], "t": s["start"]} for s in secs]}})
    scenes.sort(key=lambda s: s["start"])
    clean = []
    for sc in scenes:  # drop overlaps, earliest wins
        if not clean or sc["start"] >= clean[-1]["end"]:
            clean.append(sc)
    for sc in clean:
        sc["start"], sc["end"] = round(sc["start"], 2), round(sc["end"], 2)
    doc = {"keywords": [], "auto_keywords": True, "scenes": clean}
    with open(os.path.join(job_dir, "scenes.json"), "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    return doc


def main():
    ap = argparse.ArgumentParser(description="Navy & gold motion-graphics reels")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("analyze")
    a.add_argument("video")
    a.add_argument("--out", required=True)
    a.add_argument("--options", default="{}")
    r = sub.add_parser("render")
    r.add_argument("job")
    r.add_argument("--scenes")
    r.add_argument("--from", dest="t_from", type=float)
    r.add_argument("--to", dest="t_to", type=float)
    au = sub.add_parser("auto")
    au.add_argument("job")
    args = ap.parse_args()
    if args.cmd == "analyze":
        analyze(args.video, os.path.abspath(args.out), json.loads(args.options))
    elif args.cmd == "render":
        render(os.path.abspath(args.job), args.scenes and os.path.abspath(args.scenes), args.t_from, args.t_to)
    else:
        print(json.dumps(auto_scenes(os.path.abspath(args.job)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
