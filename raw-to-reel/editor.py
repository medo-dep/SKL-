"""Raw-to-Reel editing pipeline (ffmpeg based).

Turns one or more raw talking-head clips into finished reels: cuts silences /
filler words / bad takes, adds zoom cuts, color, studio sound, captions
(styled, word highlight, English line), hook, logo, B-roll (own + Pexels),
whoosh SFX, progress bar, end card, music, speed-up and a thumbnail. Can split
a long video into several reels and exports a DaVinci Resolve cut list.
"""

import json
import os
import re
import shutil
import subprocess
import urllib.parse
import urllib.request
from collections import Counter

FPS = 30
HERE = os.path.dirname(os.path.abspath(__file__))
FONT_FILE = os.path.join(HERE, "fonts", "Qatar2022Arabic-Bold.ttf")
FONT_NAME = "Qatar2022 Arabic"

FILLERS = {
    "um", "umm", "uh", "uhh", "uhm", "er", "erm", "ah", "hmm", "mm",
    "امم", "ام", "اه", "آه", "ااه", "يعني", "إمم", "اممم",
}

ASPECTS = {"9:16": (1080, 1920), "4:5": (1080, 1350), "1:1": (1080, 1080), "16:9": (1920, 1080)}

# name -> (text, box, highlight) as ASS &HAABBGGRR colours, plus the box colour as hex for ffmpeg
PALETTES = {
    "orange": ("&H00FFFFFF", "&H001E50E8", "&H004DE1FF", "0xE8501E"),
    "yellow": ("&H00000000", "&H000AD6FF", "&H003539E5", "0xFFD60A"),
    "red": ("&H00FFFFFF", "&H003539E5", "&H004DE1FF", "0xE53935"),
    "green": ("&H00FFFFFF", "&H005BA522", "&H004DE1FF", "0x22A55B"),
    "blue": ("&H00FFFFFF", "&H00E5881E", "&H004DE1FF", "0x1E88E5"),
    "white": ("&H00000000", "&H00FFFFFF", "&H001E50E8", "0xFFFFFF"),
    "black": ("&H00FFFFFF", "&H00000000", "&H004DE1FF", "0x111111"),
}
CAPTION_SIZES = {"small": 70, "medium": 92, "large": 116}

STOPWORDS = set("""a an the and or but if then so to of in on at by for with from as is are was were be been
being it its this that these those i you he she we they me my your our their them his her what which who
whom how when where why all any some no not only just very can will would should could do does did done
have has had about into over after before again also there here up down out more most other such than
too now one two get got going go really thing things like know think want make people lot way well yes
okay ok today because let us""".split())

DEFAULT_OPTIONS = {
    # cutting
    "remove_bad_takes": True,
    "remove_fillers": True,
    "cut_silences": True,
    "zoom_cuts": True,
    "speed": 1.0,
    "target_length": 0,  # seconds, 0 = auto
    "split_reels": False,
    "reel_length": 60,
    # captions
    "captions": True,
    "caption_color": "orange",
    "caption_size": "medium",
    "caption_position": "lower",  # lower / middle / top
    "highlight_word": True,
    "english_subs": False,
    "text_hook": False,
    "hook_text": "",
    # branding
    "aspect": "9:16",
    "logo": False,
    "logo_position": "top-right",
    "end_card": False,
    "end_card_title": "تابعنا للمزيد",
    "end_card_contact": "",
    "thumbnail": True,
    "thumbnail_text": "",
    "progress_bar": False,
    # sound
    "studio_sound": True,
    "music": False,
    "sfx": False,
    "color": True,
    # b-roll
    "broll": False,
    "pexels": False,
    "pexels_key": "",
    "pexels_keywords": "",
    # transcription
    "whisper_model": "small",
    "language": "",  # "" = auto detect
    "notes": "",
}


def run(cmd, cwd=None):
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed:\n{proc.stderr[-2000:]}")
    return proc


def probe(path):
    out = run([
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", path,
    ]).stdout
    info = json.loads(out)
    video = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    has_audio = any(s["codec_type"] == "audio" for s in info["streams"])
    fps = 30.0
    if video and video.get("avg_frame_rate", "0/0") != "0/0":
        num, den = video["avg_frame_rate"].split("/")
        fps = float(num) / float(den) if float(den) else 30.0
    return {
        "duration": float(info["format"].get("duration", 0) or 0),
        "fps": fps,
        "has_audio": has_audio,
        "width": int(video["width"]) if video else 0,
        "height": int(video["height"]) if video else 0,
    }


def is_image(path):
    return os.path.splitext(path)[1].lower() in (".jpg", ".jpeg", ".png", ".webp", ".bmp")


# ---------------------------------------------------------------- analysis

def detect_silences(path, duration, noise_db=-32, min_silence=0.45):
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", path, "-af",
         f"silencedetect=noise={noise_db}dB:d={min_silence}", "-f", "null", "-"],
        capture_output=True, encoding="utf-8", errors="replace",
    )
    silences, start = [], None
    for line in proc.stderr.splitlines():
        m = re.search(r"silence_start: (-?[\d.]+)", line)
        if m:
            start = max(0.0, float(m.group(1)))
        m = re.search(r"silence_end: ([\d.]+)", line)
        if m and start is not None:
            silences.append((start, float(m.group(1))))
            start = None
    if start is not None:
        silences.append((start, duration))
    return silences


def transcribe(path, model_name, language, log, translate=False):
    """(words, english_segments); either can be None if no Whisper backend is usable."""
    try:
        return _transcribe(path, model_name, language or None, log, translate)
    except Exception as exc:  # model download / decode failures shouldn't kill the edit
        log(f"⚠️ تعذّر تفريغ الصوت ({type(exc).__name__}): {str(exc)[:160]}")
        return None, None


def load_audio(path, sr=16000):
    """Decode to 16 kHz mono float32 with ffmpeg (bypasses PyAV, whose API changed under faster-whisper)."""
    import numpy as np

    out = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-i", path, "-f", "s16le", "-ac", "1", "-ar", str(sr), "-"],
        capture_output=True, check=True,
    ).stdout
    return np.frombuffer(out, np.int16).astype(np.float32) / 32768.0


def _transcribe(path, model_name, lang, log, translate):
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        WhisperModel = None
    if WhisperModel:
        def run_on(device):
            model = WhisperModel(model_name, device=device, compute_type="int8")
            segments, _ = model.transcribe(audio, language=lang, word_timestamps=True)
            words = [{"start": w.start, "end": w.end, "text": w.word.strip()}
                     for seg in segments for w in (seg.words or [])]
            english = None
            if translate:
                log("ترجمة الكلام للإنجليزي...")
                segs, _ = model.transcribe(audio, language=lang, task="translate")
                english = [{"start": s.start, "end": s.end, "text": s.text.strip()} for s in segs]
            return words, english

        log(f"تفريغ الصوت بـ faster-whisper ({model_name})... أول مرة ينزّل النموذج وياخذ وقت")
        audio = load_audio(path)
        try:
            return run_on("auto")
        except Exception as exc:  # usually missing CUDA DLLs (cublas/cudnn) on NVIDIA laptops
            log(f"⚠️ تعذّر التشغيل على كرت الشاشة ({str(exc)[:120]})، أعيد المحاولة على المعالج...")
            return run_on("cpu")
    try:
        import whisper
    except ImportError:
        return None, None
    log(f"تفريغ الصوت بـ whisper ({model_name})...")
    model = whisper.load_model(model_name)
    audio = load_audio(path)
    result = model.transcribe(audio, language=lang, word_timestamps=True)
    words = [{"start": w["start"], "end": w["end"], "text": w["word"].strip()}
             for seg in result["segments"] for w in seg.get("words", [])]
    english = None
    if translate:
        log("ترجمة الكلام للإنجليزي...")
        res = model.transcribe(audio, language=lang, task="translate")
        english = [{"start": s["start"], "end": s["end"], "text": s["text"].strip()} for s in res["segments"]]
    return words, english


def normalize_word(text):
    return re.sub(r"[^\w]", "", text.lower())


def find_bad_takes(words, gap=0.7, prefix=3):
    """A sentence that restarts with the same opening words as the next one is a bad take."""
    sentences, current = [], []
    for w in words:
        if current and (w["start"] - current[-1]["end"] > gap
                        or re.search(r"[.!?؟]$", current[-1]["text"])):
            sentences.append(current)
            current = []
        current.append(w)
    if current:
        sentences.append(current)

    def opening(sentence):
        return [normalize_word(w["text"]) for w in sentence[:prefix]]

    cuts = []
    for a, b in zip(sentences, sentences[1:]):
        if len(a) >= 2 and opening(a)[:2] == opening(b)[:2]:
            cuts.append((a[0]["start"], b[0]["start"]))
    return cuts


def subtract(span, cuts):
    """[span] minus the union of [cuts] -> list of kept intervals."""
    keep = [span]
    for cs, ce in sorted(cuts):
        nxt = []
        for ks, ke in keep:
            if ce <= ks or cs >= ke:
                nxt.append((ks, ke))
                continue
            if cs > ks:
                nxt.append((ks, cs))
            if ce < ke:
                nxt.append((ce, ke))
        keep = nxt
    return [(s, e) for s, e in keep if e - s >= 0.25]


def plan_cuts(inputs, opts, log):
    """Kept segments across all inputs, plus English translation segments per input index."""
    segments, english = [], {}
    for idx, path in enumerate(inputs):
        info = probe(path)
        name = os.path.basename(path)
        log(f"تحليل {name} ({info['duration']:.1f} ث)")

        words = None
        wants_english = opts["english_subs"] or (opts["pexels"] and not opts["pexels_keywords"].strip())
        needs_words = (opts["captions"] or opts["remove_fillers"] or opts["remove_bad_takes"]
                       or opts["text_hook"] or wants_english)
        if needs_words and info["has_audio"]:
            words, english[idx] = transcribe(path, opts["whisper_model"], opts["language"], log, wants_english)
            if words is None:
                log("⚠️ بدون تفريغ صوتي: لن تعمل الترجمة وحذف الكلمات الزائدة والإعادات. "
                    "تأكد من تثبيت Whisper: pip install faster-whisper")

        cuts = []
        if opts["cut_silences"] and info["has_audio"]:
            pad = 0.12
            for s, e in detect_silences(path, info["duration"]):
                if e - s > 2 * pad:
                    cuts.append((s + pad if s > 0 else 0, e - pad if e < info["duration"] else e))
            log(f"  صمت: {len(cuts)} مقطع")
        if words and opts["remove_fillers"]:
            fillers = [(w["start"], w["end"]) for w in words if normalize_word(w["text"]) in FILLERS]
            cuts += fillers
            log(f"  كلمات زائدة: {len(fillers)}")
        if words and opts["remove_bad_takes"]:
            takes = find_bad_takes(words)
            cuts += takes
            log(f"  إعادات/أخطاء: {len(takes)}")

        for s, e in subtract((0.0, info["duration"]), cuts):
            seg_words = [w for w in (words or []) if w["start"] >= s - 0.05 and w["end"] <= e + 0.05]
            segments.append({"file": path, "index": idx, "start": round(s, 3), "end": round(e, 3),
                             "fps": info["fps"], "has_audio": info["has_audio"], "words": seg_words})

    target = float(opts.get("target_length") or 0)
    if target > 0:
        total, trimmed = 0.0, []
        for seg in segments:
            if total >= target:
                break
            room = target - total
            if seg["end"] - seg["start"] > room:
                seg = split_segment(seg, seg["start"] + room)[0]
            trimmed.append(seg)
            total += seg["end"] - seg["start"]
        segments = trimmed
    return segments, english


def seg_len(seg):
    return seg["end"] - seg["start"]


def split_segment(seg, at):
    """Split a segment near time `at`, snapping back to the end of the last word before it."""
    ends = [w["end"] for w in seg["words"] if seg["start"] + 1.0 < w["end"] <= at]
    cut = round(ends[-1] if ends else at, 3)
    first = dict(seg, end=cut, words=[w for w in seg["words"] if w["end"] <= cut])
    second = dict(seg, start=cut, words=[w for w in seg["words"] if w["start"] >= cut])
    return first, second


def split_into_reels(segments, reel_len):
    reels, cur, total = [], [], 0.0
    queue = list(segments)
    while queue:
        seg = queue.pop(0)
        d = seg_len(seg)
        if total + d <= reel_len + 3:
            cur.append(seg)
            total += d
        elif total >= reel_len * 0.6:
            reels.append(cur)
            cur, total = [], 0.0
            queue.insert(0, seg)
        else:
            first, second = split_segment(seg, seg["start"] + (reel_len - total))
            if seg_len(first) >= 0.5:
                cur.append(first)
            reels.append(cur)
            cur, total = [], 0.0
            if seg_len(second) >= 0.5:
                queue.insert(0, second)
    if cur:
        if reels and sum(seg_len(s) for s in cur) < 10:
            reels[-1].extend(cur)
        else:
            reels.append(cur)
    return [r for r in reels if r]


# ---------------------------------------------------------------- subtitles

def ass_time(t):
    cs = int(round(max(0.0, t) * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def srt_time(t):
    ms = int(round(max(0.0, t) * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def timeline_words(segments):
    """Map each kept word onto the reel's output timeline."""
    out, offset = [], 0.0
    for seg in segments:
        for w in seg["words"]:
            out.append({"text": w["text"],
                        "start": offset + max(0.0, w["start"] - seg["start"]),
                        "end": offset + min(seg["end"], w["end"]) - seg["start"]})
        offset += seg_len(seg)
    return out


def timeline_english(segments, english):
    """Map source-time English segments onto the reel's output timeline."""
    out = []
    for idx, segs in english.items():
        for e in segs or []:
            pieces, offset = [], 0.0
            for seg in segments:
                if seg["index"] == idx:
                    s, t = max(e["start"], seg["start"]), min(e["end"], seg["end"])
                    if t - s > 0.05:
                        pieces.append((offset + s - seg["start"], offset + t - seg["start"]))
                offset += seg_len(seg)
            if pieces and e["text"]:
                out.append({"start": pieces[0][0], "end": pieces[-1][1], "text": e["text"]})
    return sorted(out, key=lambda x: x["start"])


def caption_chunks(words, size=3):
    chunks = []
    for i in range(0, len(words), size):
        group = [w for w in words[i:i + size] if normalize_word(w["text"]) not in FILLERS]
        if group:
            chunks.append({"text": " ".join(w["text"] for w in group), "words": group,
                           "start": group[0]["start"], "end": group[-1]["end"]})
    for a, b in zip(chunks, chunks[1:]):
        a["end"] = min(max(a["end"], a["start"] + 0.3), b["start"])
    return chunks


def ass_escape(text):
    return text.replace("\\", "\\\\").replace("{", "(").replace("}", ")").replace("\n", " ")


def ass_header(w, h, opts):
    text, box, _, _ = PALETTES.get(opts["caption_color"], PALETTES["orange"])
    k = min(w, h) / 1080
    size = int(CAPTION_SIZES.get(opts["caption_size"], 92) * k)
    pos = opts["caption_position"]
    align, margin = {"top": (8, int(0.17 * h)), "middle": (5, 0)}.get(pos, (2, int(0.32 * h)))
    # top hook sits below the logo area (logo: 5% from top, ~17% of the short side tall)
    hook_align, hook_margin = (5, 0) if pos == "top" else (8, int(0.05 * h + 0.2 * min(w, h)))
    en_margin = int(0.12 * h) if pos != "lower" else int(0.32 * h) - int(size * 1.9)
    end_text = "&H00FFFFFF" if opts["caption_color"] == "white" else text  # white palette gets a dark end card
    styles = [
        f"Caption,{FONT_NAME},{size},{text},{text},{box},{box},1,0,0,0,100,100,0,0,3,{int(14 * k)},0,{align},80,80,{margin},-1",
        f"Hook,{FONT_NAME},{int(84 * k)},{box},{box},{text},{text},1,0,0,0,100,100,0,0,3,{int(22 * k)},0,{hook_align},80,80,{hook_margin},-1",
        f"English,{FONT_NAME},{int(size * 0.58)},&H00FFFFFF,&H00FFFFFF,&H70000000,&H70000000,1,0,0,0,100,100,0,0,3,{int(10 * k)},0,2,90,90,{en_margin},-1",
        f"Thumb,{FONT_NAME},{int(128 * k)},{text},{text},{box},{box},1,0,0,0,100,100,0,0,3,{int(26 * k)},0,2,70,70,{int(0.2 * h)},-1",
        f"EndTitle,{FONT_NAME},{int(110 * k)},{end_text},{end_text},&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,0,0,5,60,60,0,-1",
        f"EndContact,{FONT_NAME},{int(64 * k)},{end_text},{end_text},&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,0,0,2,60,60,{int(0.3 * h)},-1",
    ]
    # Encoding -1 = full Unicode bidi; otherwise libass mimics VSFilter and lays Arabic words out left-to-right
    return (f"[Script Info]\nScriptType: v4.00+\nPlayResX: {w}\nPlayResY: {h}\nWrapStyle: 0\n\n"
            "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
            "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, "
            "Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
            + "".join(f"Style: {s}\n" for s in styles)
            + "\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")


def dialogue(start, end, style, text, layer=0):
    return f"Dialogue: {layer},{ass_time(start)},{ass_time(end)},{style},,0,0,0,,{text}\n"


def caption_events(chunks, opts):
    text_c, _, hl_c, _ = PALETTES.get(opts["caption_color"], PALETTES["orange"])
    pop = "{\\fscx85\\fscy85\\t(0,90,\\fscx100\\fscy100)}"
    lines = []
    for c in chunks:
        if not opts["highlight_word"] or len(c["words"]) < 2:
            lines.append(dialogue(c["start"], c["end"], "Caption", pop + ass_escape(c["text"])))
            continue
        for i, w in enumerate(c["words"]):
            start = c["start"] if i == 0 else w["start"]
            end = c["words"][i + 1]["start"] if i + 1 < len(c["words"]) else c["end"]
            if end - start < 0.02:
                continue
            # every word gets its own colour tag: libass lays out multi-word Arabic runs left-to-right
            parts = [f"{{\\c{hl_c if j == i else text_c}}}{ass_escape(x['text'])}" for j, x in enumerate(c["words"])]
            lines.append(dialogue(start, end, "Caption", (pop if i == 0 else "") + " ".join(parts)))
    return lines


def write_srt(path, items):
    with open(path, "w", encoding="utf-8") as f:
        for i, c in enumerate(items, 1):
            f.write(f"{i}\n{srt_time(c['start'])} --> {srt_time(c['end'])}\n{c['text']}\n\n")


# ---------------------------------------------------------------- pexels b-roll

def pexels_keywords(opts, english_items, count):
    manual = [k.strip() for k in re.split(r"[,،\n]", opts["pexels_keywords"]) if k.strip()]
    if manual:
        return manual[:count]
    words = Counter(
        w for item in english_items for w in re.findall(r"[a-z]{4,}", item["text"].lower()) if w not in STOPWORDS
    )
    return [w for w, _ in words.most_common(count)]


def fetch_pexels(keywords, key, aspect, out_dir, log):
    orientation = {"16:9": "landscape", "1:1": "square"}.get(aspect, "portrait")
    os.makedirs(out_dir, exist_ok=True)
    paths = []
    for kw in keywords:
        url = "https://api.pexels.com/videos/search?" + urllib.parse.urlencode(
            {"query": kw, "orientation": orientation, "per_page": 5, "size": "medium"})
        try:
            req = urllib.request.Request(url, headers={"Authorization": key, "User-Agent": "raw-to-reel"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                videos = json.load(resp).get("videos", [])
            files = [f for v in videos[:1] for f in v.get("video_files", [])
                     if f.get("file_type") == "video/mp4" and f.get("width")]
            if not files:
                log(f"  Pexels: ما لقيت لقطة لـ «{kw}»")
                continue
            best = min(files, key=lambda f: abs(max(f["width"], f["height"]) - 1920))
            path = os.path.join(out_dir, f"pexels-{re.sub(r'[^a-z0-9]+', '-', kw.lower())}.mp4")
            req = urllib.request.Request(best["link"], headers={"User-Agent": "raw-to-reel"})
            with urllib.request.urlopen(req, timeout=120) as resp, open(path, "wb") as f:
                shutil.copyfileobj(resp, f)
            paths.append(path)
            log(f"  Pexels: لقطة «{kw}» ✓")
        except Exception as exc:
            log(f"⚠️ Pexels «{kw}»: {str(exc)[:120]}")
    return paths


# ---------------------------------------------------------------- rendering

def video_filter(opts, w, h, zoom):
    vf = [f"scale={w}:{h}:force_original_aspect_ratio=increase", f"crop={w}:{h}"]
    if zoom:
        vf += [f"scale={int(w * 1.15) // 2 * 2}:{int(h * 1.15) // 2 * 2}", f"crop={w}:{h}:(iw-{w})/2:(ih-{h})/3"]
    if opts["color"]:
        vf.append("eq=contrast=1.06:saturation=1.18:brightness=0.015:gamma=0.98")
    vf += [f"fps={FPS}", "setsar=1", "format=yuv420p"]
    return ",".join(vf)


ENCODE = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-pix_fmt", "yuv420p",
          "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]


def render_end_card(job_dir, name, opts, w, h, logo, duration=3.0):
    _, _, _, box_hex = PALETTES.get(opts["caption_color"], PALETTES["orange"])
    if box_hex == "0xFFFFFF":
        box_hex = "0x111111"
    with open(os.path.join(job_dir, f"{name}.ass"), "w", encoding="utf-8") as f:
        f.write(ass_header(w, h, opts))
        f.write(dialogue(0, duration, "EndTitle", "{\\fad(250,0)}" + ass_escape(opts["end_card_title"] or "تابعنا")))
        if opts["end_card_contact"].strip():
            f.write(dialogue(0, duration, "EndContact", "{\\fad(400,0)}" + ass_escape(opts["end_card_contact"])))
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"color=c={box_hex}:s={w}x{h}:r={FPS}:d={duration}",
           "-f", "lavfi", "-t", str(duration), "-i", "anullsrc=r=48000:cl=stereo"]
    graph = f"[0:v]ass={name}.ass:fontsdir=fonts,fade=in:d=0.3[v]"
    if logo:
        cmd += ["-i", logo]
        graph = (f"[0:v]ass={name}.ass:fontsdir=fonts[bg];[2:v]scale={int(min(w, h) * 0.3)}:-1,format=rgba[lg];"
                 f"[bg][lg]overlay=(W-w)/2:H*0.18,fade=in:d=0.3[v]")
    cmd += ["-filter_complex", graph, "-map", "[v]", "-map", "1:a", "-t", str(duration), *ENCODE, f"{name}.mp4"]
    run(cmd, cwd=job_dir)
    return f"{name}.mp4"


def make_whoosh(job_dir):
    path = os.path.join(job_dir, "whoosh.wav")
    if not os.path.exists(path):
        run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "anoisesrc=d=0.5:c=pink:r=48000:a=0.7",
             "-af", "highpass=f=350,lowpass=f=5000,afade=t=in:d=0.22:curve=qsin,afade=t=out:st=0.22:d=0.28,"
                    "aformat=channel_layouts=stereo", path])
    return path


def plan_broll(brolls, duration, reel_no):
    """Evenly spaced cutaways, at least 8s apart, avoiding the first and last 3 seconds."""
    if not brolls or duration < 12:
        return []
    slots = min(len(brolls), int((duration - 6) // 8))
    placed = []
    for i in range(slots):
        path = brolls[(reel_no * slots + i) % len(brolls)]
        d = 2.5 if is_image(path) else min(3.5, max(1.0, probe(path)["duration"]))
        t = 3 + (i + 0.5) * (duration - 6) / slots - d / 2
        placed.append({"path": path, "start": round(t, 2), "dur": d})
    return placed


def render_reel(job_dir, prefix, reel_no, segments, english, opts, assets, log, progress):
    w, h = ASPECTS.get(opts["aspect"], ASPECTS["9:16"])
    _, _, _, box_hex = PALETTES.get(opts["caption_color"], PALETTES["orange"])
    parts_dir = os.path.join(job_dir, f"{prefix}_parts")
    os.makedirs(parts_dir, exist_ok=True)

    # 1. cut parts
    listing, boundaries, offset = [], [], 0.0
    for n, seg in enumerate(segments):
        zoom = opts["zoom_cuts"] and n % 2 == 1
        if zoom and n:
            boundaries.append(offset)
        cmd = ["ffmpeg", "-y", "-v", "error", "-ss", str(seg["start"]), "-t", str(seg_len(seg)), "-i", seg["file"]]
        if not seg["has_audio"]:
            cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-shortest"]
        cmd += ["-vf", video_filter(opts, w, h, zoom), "-af", "aresample=48000,aformat=channel_layouts=stereo",
                *ENCODE, os.path.join(parts_dir, f"{n:04d}.mp4")]
        run(cmd)
        listing.append(f"file '{prefix}_parts/{n:04d}.mp4'")  # relative: avoids Windows path issues
        offset += seg_len(seg)
        progress(f"قص المقطع {n + 1}/{len(segments)}", (n + 1) / len(segments) * 0.6)
    content = offset

    end_card = 0.0
    if opts["end_card"]:
        listing.append(f"file '{render_end_card(job_dir, prefix + '_end', opts, w, h, assets.get('logo'))}'")
        end_card = 3.0
    total = content + end_card

    with open(os.path.join(job_dir, f"{prefix}_parts.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(listing))
    joined = f"{prefix}_joined.mp4"
    run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", f"{prefix}_parts.txt",
         "-c", "copy", joined], cwd=job_dir)

    # 2. subtitles
    words = timeline_words(segments)
    chunks = caption_chunks(words) if opts["captions"] else []
    en_items = timeline_english(segments, english) if opts["english_subs"] else []
    hook = ""
    if opts["text_hook"]:
        hook = opts["hook_text"].strip() or " ".join(w["text"] for w in words[:6])
    has_ass = bool(chunks or en_items or hook)
    if has_ass:
        with open(os.path.join(job_dir, f"{prefix}.ass"), "w", encoding="utf-8") as f:
            f.write(ass_header(w, h, opts))
            f.writelines(caption_events(chunks, opts))
            f.writelines(dialogue(e["start"], e["end"], "English", ass_escape(e["text"]), 1) for e in en_items)
            if hook:
                f.write(dialogue(0, min(3.0, content), "Hook", "{\\fad(0,250)}" + ass_escape(hook), 2))
    if chunks:
        write_srt(os.path.join(job_dir, f"{prefix}.srt"), chunks)
    if en_items:
        write_srt(os.path.join(job_dir, f"{prefix}_en.srt"), en_items)

    # 3. thumbnail (from the clean cut, before captions are burned in)
    thumb = None
    if opts["thumbnail"]:
        title = opts["thumbnail_text"].strip() or hook or " ".join(w["text"] for w in words[:5])
        vf = "thumbnail=120"
        if title:
            with open(os.path.join(job_dir, f"{prefix}_thumb.ass"), "w", encoding="utf-8") as f:
                f.write(ass_header(w, h, opts))
                f.write(dialogue(0, 36000, "Thumb", ass_escape(title)))
            vf += f",ass={prefix}_thumb.ass:fontsdir=fonts"
        thumb = f"{prefix}_thumb.jpg"
        run(["ffmpeg", "-y", "-v", "error", "-ss", "0.5", "-t", str(max(1.0, min(12.0, content - 0.5))),
             "-i", joined, "-vf", vf, "-frames:v", "1", "-q:v", "2", thumb], cwd=job_dir)

    # 4. final pass: b-roll, captions, logo, progress bar, sfx, music, speed
    progress("المعالجة النهائية (ترجمة، صوت، موسيقى، B-roll)...", 0.7)
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", joined]
    vchain, achain, n_in = [], [], 1

    def add_input(*args):
        nonlocal n_in
        cmd.extend(args)
        n_in += 1
        return n_in - 1

    v = "0:v"
    brolls = plan_broll(assets.get("broll", []), content, reel_no)
    for j, b in enumerate(brolls):
        src = add_input("-loop", "1", "-t", str(b["dur"]), "-i", b["path"]) if is_image(b["path"]) \
            else add_input("-t", str(b["dur"]), "-i", b["path"])
        fade_out = max(0.0, b["dur"] - 0.2)
        vchain.append(f"[{src}:v]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1,"
                      f"fps={FPS},format=yuva420p,fade=in:st=0:d=0.2:alpha=1,fade=out:st={fade_out}:d=0.2:alpha=1,"
                      f"setpts=PTS-STARTPTS+{b['start']}/TB[b{j}]")
        vchain.append(f"[{v}][b{j}]overlay=eof_action=pass:enable='between(t,{b['start']},{b['start'] + b['dur']})'[vb{j}]")
        v = f"vb{j}"
    if has_ass:
        vchain.append(f"[{v}]ass={prefix}.ass:fontsdir=fonts[vs]")
        v = "vs"
    if opts["logo"] and assets.get("logo"):
        src = add_input("-i", assets["logo"])
        m = int(min(w, h) * 0.045)
        x, y = {"top-left": (f"{m}", f"{int(h * 0.05)}"), "bottom-left": (f"{m}", f"H-h-{int(h * 0.1)}"),
                "bottom-right": (f"W-w-{m}", f"H-h-{int(h * 0.1)}")}.get(opts["logo_position"], (f"W-w-{m}", f"{int(h * 0.05)}"))
        vchain.append(f"[{src}:v]scale={int(min(w, h) * 0.17)}:-1,format=rgba,colorchannelmixer=aa=0.9[lg]")
        vchain.append(f"[{v}][lg]overlay={x}:{y}:enable='lt(t,{content})'[vl]")
        v = "vl"
    if opts["progress_bar"]:
        bar_h = max(8, int(h * 0.007))
        vchain.append(f"color=c={box_hex}:s={w}x{bar_h}:r={FPS}[pb]")
        vchain.append(f"[{v}][pb]overlay=x='-w+w*t/{total:.3f}':y=0:shortest=1[vp]")
        v = "vp"
    speed = float(opts.get("speed") or 1.0)
    vchain.append(f"[{v}]setpts=PTS/{speed}[vo]" if speed != 1.0 else f"[{v}]null[vo]")

    voice = "anull"
    if opts["studio_sound"]:
        voice = "highpass=f=80,lowpass=f=14000,afftdn=nf=-25,acompressor=threshold=-20dB:ratio=3:attack=5:release=80"
    achain.append(f"[0:a]{voice}[a0]")
    a = "a0"
    if opts["sfx"]:
        hits = sorted(set(round(t, 2) for t in boundaries + [b["start"] for b in brolls]
                          + ([content] if end_card else [])))
        hits = [t for i, t in enumerate(hits) if t > 0.3 and (i == 0 or t - hits[i - 1] > 1.0)]
        if hits:
            src = add_input("-i", make_whoosh(job_dir))
            achain.append(f"[{src}:a]asplit={len(hits)}" + "".join(f"[s{i}]" for i in range(len(hits))))
            for i, t in enumerate(hits):
                ms = int(max(0.0, t - 0.2) * 1000)
                achain.append(f"[s{i}]adelay={ms}|{ms},volume=0.35[d{i}]")
            achain.append(f"[{a}]" + "".join(f"[d{i}]" for i in range(len(hits)))
                          + f"amix=inputs={len(hits) + 1}:duration=first:normalize=0[asfx]")
            a = "asfx"
    if opts["music"] and assets.get("music"):
        src = add_input("-stream_loop", "-1", "-i", assets["music"])
        achain.append(f"[{src}:a]volume=0.12,afade=t=in:d=1,afade=t=out:st={max(0.0, total - 1.5):.2f}:d=1.5[m]")
        achain.append(f"[{a}][m]amix=inputs=2:duration=first:normalize=0[amus]")
        a = "amus"
    tail = [f"atempo={speed}"] if speed != 1.0 else []
    if opts["studio_sound"]:
        # loudnorm buffers ~3s; it must come after amix (duration=first) or that tail gets dropped
        tail.append("loudnorm=I=-14:TP=-1.5:LRA=9")
    achain.append(f"[{a}]{','.join(tail) or 'anull'}[ao]")

    final = f"{prefix}.mp4"
    cmd += ["-filter_complex", ";".join(vchain + achain), "-map", "[vo]", "-map", "[ao]",
            "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", final]
    run(cmd, cwd=job_dir)

    shutil.rmtree(parts_dir, ignore_errors=True)
    for leftover in (joined, f"{prefix}_parts.txt", f"{prefix}_end.mp4"):
        if os.path.exists(os.path.join(job_dir, leftover)):
            os.remove(os.path.join(job_dir, leftover))
    return {
        "final": final,
        "srt": f"{prefix}.srt" if chunks else None,
        "srt_en": f"{prefix}_en.srt" if en_items else None,
        "thumbnail": thumb,
        "seconds": round(total / speed, 2),
        "broll": len(brolls),
    }


# ---------------------------------------------------------------- resolve export

def tc(seconds, fps):
    frames = int(round(seconds * fps))
    f = frames % fps
    s = frames // fps
    return f"{s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}:{f:02d}"


def write_resolve_exports(job_dir, segments):
    fps = int(round(segments[0]["fps"])) if segments else FPS
    rec = 3600.0  # Resolve timelines start at 01:00:00:00
    lines = ["TITLE: Raw to Reel", "FCM: NON-DROP FRAME", ""]
    for n, seg in enumerate(segments, 1):
        dur = seg_len(seg)
        lines.append(f"{n:03d}  AX       AA/V  C        {tc(seg['start'], fps)} {tc(seg['end'], fps)} "
                     f"{tc(rec, fps)} {tc(rec + dur, fps)}")
        lines.append(f"* FROM CLIP NAME: {os.path.basename(seg['file'])}")
        lines.append("")
        rec += dur
    with open(os.path.join(job_dir, "timeline.edl"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    cuts = [{"file": os.path.abspath(s["file"]), "start": s["start"], "end": s["end"], "fps": s["fps"]}
            for s in segments]
    with open(os.path.join(job_dir, "cuts.json"), "w", encoding="utf-8") as f:
        json.dump(cuts, f, ensure_ascii=False, indent=2)
    shutil.copy(os.path.join(HERE, "resolve_import.py"), os.path.join(job_dir, "resolve_import.py"))


# ---------------------------------------------------------------- entry point

def process(job_dir, inputs, options, assets=None, log=print):
    opts = {**DEFAULT_OPTIONS, **(options or {})}
    for key in ("speed", "target_length", "reel_length"):
        opts[key] = float(opts[key] or 0)
    opts["speed"] = opts["speed"] or 1.0
    opts["reel_length"] = opts["reel_length"] or 60
    assets = dict(assets or {})
    assets["broll"] = list(assets.get("broll") or []) if opts["broll"] else []
    os.makedirs(os.path.join(job_dir, "fonts"), exist_ok=True)
    if os.path.exists(FONT_FILE):
        shutil.copy(FONT_FILE, os.path.join(job_dir, "fonts"))

    def emit(msg, progress=None):
        log(msg, progress) if log is not print else print(msg)

    for opt, asset, label in (("music", "music", "موسيقى"), ("logo", "logo", "شعار")):
        if opts[opt] and not assets.get(asset):
            emit(f"⚠️ مفعّل «{label}» بس ما رفعت ملف، بيتم التخطي")
    if opts["broll"] and not assets["broll"]:
        emit("⚠️ مفعّل «B-roll خاص» بس ما رفعت صور أو مقاطع")

    emit("بدء التحليل...", 0.05)
    segments, english = plan_cuts(inputs, opts, emit)
    if not segments:
        raise RuntimeError("لم يتبقَّ أي جزء من الفيديو بعد القص. جرّب إيقاف حذف الصمت.")
    total_in = sum(probe(p)["duration"] for p in inputs)
    total_out = sum(seg_len(s) for s in segments)
    write_resolve_exports(job_dir, segments)

    if opts["pexels"]:
        if not opts["pexels_key"].strip():
            emit("⚠️ B-roll من Pexels يحتاج مفتاح API، بيتم التخطي")
        else:
            en_all = timeline_english(segments, english)
            keywords = pexels_keywords(opts, en_all, max(1, min(6, int(total_out // 15))))
            if keywords:
                emit(f"تنزيل لقطات من Pexels: {', '.join(keywords)}")
                assets["broll"] += fetch_pexels(keywords, opts["pexels_key"].strip(), opts["aspect"],
                                                os.path.join(job_dir, "pexels"), emit)
            else:
                emit("⚠️ ما قدرت أطلع كلمات بحث لـ Pexels، اكتبها بنفسك بالإنجليزي")

    reels = split_into_reels(segments, opts["reel_length"]) if opts["split_reels"] else [segments]
    emit(f"الخطة: {len(segments)} لقطة، {total_in:.1f}ث ← {total_out:.1f}ث"
         + (f"، {len(reels)} ريلز" if len(reels) > 1 else ""), 0.3)

    results = []
    for k, reel in enumerate(reels):
        prefix = "final" if len(reels) == 1 else f"reel_{k + 1}"
        base = 0.3 + 0.68 * k / len(reels)

        def progress(msg, frac, base=base, k=k):
            label = f"[ريل {k + 1}/{len(reels)}] " if len(reels) > 1 else ""
            emit(label + msg, base + 0.68 * frac / len(reels))

        results.append(render_reel(job_dir, prefix, k, reel, english, opts, assets, emit, progress))

    safe_opts = {k: v for k, v in opts.items() if k != "pexels_key"}
    result = {
        "reels": results,
        "final": results[0]["final"],
        "edl": "timeline.edl",
        "resolve_script": "resolve_import.py",
        "captions_missing": bool(opts["captions"] and not any(r["srt"] for r in results)),
        "segments": len(segments),
        "input_seconds": round(total_in, 2),
        "output_seconds": round(sum(r["seconds"] for r in results), 2),
        "options": safe_opts,
    }
    with open(os.path.join(job_dir, "result.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    emit("✅ تم! الفيديو جاهز" if len(results) == 1 else f"✅ تم! {len(results)} ريلز جاهزة", 1.0)
    return result


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Raw-to-Reel CLI")
    ap.add_argument("inputs", nargs="+")
    ap.add_argument("--out", default="workspace/jobs/cli")
    ap.add_argument("--music")
    ap.add_argument("--logo")
    ap.add_argument("--broll", nargs="*", default=[])
    ap.add_argument("--options", default="{}", help="JSON options override")
    a = ap.parse_args()
    absp = os.path.abspath
    print(json.dumps(process(absp(a.out), [absp(p) for p in a.inputs], json.loads(a.options),
                             {"music": a.music and absp(a.music), "logo": a.logo and absp(a.logo),
                              "broll": [absp(p) for p in a.broll]}),
                     ensure_ascii=False, indent=2))
