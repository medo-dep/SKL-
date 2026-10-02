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
import sys
import urllib.parse
import urllib.request
from collections import Counter

import library

FPS = 30
HERE = os.path.dirname(os.path.abspath(__file__))
FACE_MODEL = os.path.join(HERE, "models", "face_detection_yunet_2023mar.onnx")
EMOJI_DIR = os.path.join(HERE, "assets", "emoji")  # Twemoji, CC-BY 4.0
LIB_DIR = os.path.join(HERE, "workspace", "library")  # generated music / sfx cache
WHISPER_DIR = os.path.join(HERE, "models", "whisper")  # filled by tools/make_portable.py for offline PCs
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

DICT_FILE = os.path.join(HERE, "workspace", "dictionary.json")
PREVIEW_SECONDS = 15

DEFAULT_OPTIONS = {
    # cutting
    "remove_bad_takes": True,
    "remove_fillers": True,
    "cut_silences": True,
    "zoom_cuts": True,
    "auto_reframe": True,  # follow the speaker's face when cropping
    "speed": 1.0,
    "target_length": 0,  # seconds, 0 = auto
    "split_reels": False,
    "reel_length": 60,
    # captions
    "captions": True,
    "caption_color": "orange",
    "caption_size": "medium",
    "caption_position": "auto",  # auto (avoid face/body) / lower / middle / top
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
    # publishing
    "also_4x5": False,
    "also_1x1": False,
    "also_16x9": False,
    "post_text": True,
    "translate_to": "",  # "" or a language code (fr, tr, ur, es...) -> extra .srt via argostranslate
    "music": False,
    "music_source": "upload",  # upload / calm / upbeat / lofi / inspiring (built-in, generated)
    "music_ducking": True,
    "sfx": False,
    "color": True,
    "color_look": "auto",  # none / auto / warm / cool / cinematic / bw / vivid / vintage
    "transition": "none",  # none / fade / flash / zoom / whip / mix
    "stickers": False,
    "section_titles": False,
    # b-roll
    "broll": False,
    "pexels": False,
    "pexels_key": "",
    "pexels_keywords": "",
    # motion graphics on the key words
    "motion_highlights": False,
    "highlight_density": "normal",  # few / normal / many
    "highlight_words": "",  # words the speaker wants emphasised, comma separated
    "punch_zoom": True,
    "highlight_images": False,  # Pexels photos for highlighted words (needs pexels_key)
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
        local = {"download_root": WHISPER_DIR} if os.path.isdir(WHISPER_DIR) else {}

        def run_on(device):
            model = WhisperModel(model_name, device=device, compute_type="int8", **local)
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
        wants_english = (opts["english_subs"] or opts["translate_to"] or (opts["pexels"] and not opts["pexels_keywords"].strip())
                         or (opts["motion_highlights"] and opts["highlight_images"] and opts["pexels_key"].strip()))
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
        f"HL,{FONT_NAME},{int(size * 1.6)},{text},{text},&H00000000,&H64000000,1,0,0,0,100,100,0,0,1,0,{int(4 * k)},5,40,40,0,-1",
        f"HLSmall,{FONT_NAME},{int(size * 0.62)},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,{int(4 * k)},0,5,60,60,0,-1",
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


def caption_events(chunks, opts, place=None):
    text_c, _, hl_c, _ = PALETTES.get(opts["caption_color"], PALETTES["orange"])
    lines = []
    for n, c in enumerate(chunks):
        pos = place[n] if place else ""
        pop = pos + "{\\fscx85\\fscy85\\t(0,90,\\fscx100\\fscy100)}"
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
            lines.append(dialogue(start, end, "Caption", (pop if i == 0 else pos) + " ".join(parts)))
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


# ---------------------------------------------------------------- motion highlights

AR_STOP = set("""في من على الى عن هذا هذه ذلك تلك اللي التي الذي الذين و او ثم لكن بس يعني كان كانت يكون تكون هو هي
انا احنا نحن انت انتم هم ما لا لم لن قد كل بعض مع عند عشان علشان لما اذا لو كيف ليش وش ايش شو هنا هناك الحين الان
اليوم شي شيء كثير جدا مره طيب اوكي والله السلام عليكم يا اي ايه فيه فيها منه منها عليه عليها له لها لهم بعد قبل حتى
اما انه انها ان كمان برضو زي مثل هيك كذا كذه خلاص يعني تعرف عارف شوف خلي خلينا نحكي نتكلم بنتكلم""".split())
CUES = set("""اهم السر سر نصيحه انتبه لازم ضروري افضل اخطر مشكله الحل خطوه ابدا دايما دائما اول اكبر اصغر اسرع مهم
important secret tip never always best key mistake first biggest""".split())
NUMBER_WORDS = {"مليون", "مليار", "الف", "الاف", "ميه", "مئه", "مايه", "ضعف", "نص", "ربع"}
AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


def norm_ar(text):
    t = re.sub(r"[ً-ْـ]", "", text.lower())  # tashkeel, tatweel
    t = re.sub("[إأآ]", "ا", t).replace("ى", "ي").replace("ة", "ه")
    return re.sub(r"[^\w%٪]", "", t)


def stem(text):
    t = norm_ar(text)
    for p in ("وال", "بال", "فال", "كال", "لل", "ال", "و"):
        if t.startswith(p) and len(t) - len(p) >= 3:
            return t[len(p):]
    return t


def parse_number(text):
    m = re.search(r"\d+(?:[.,]\d+)?", text.translate(AR_DIGITS))
    return float(m.group(0).replace(",", ".")) if m else None


def word_loudness(path, words):
    """Loudness of each word in dB (relative values are what matter)."""
    import numpy as np

    audio = load_audio(path)
    out = []
    for w in words:
        clip = audio[int(w["start"] * 16000):max(int(w["start"] * 16000) + 1, int(w["end"] * 16000))]
        out.append(20 * np.log10(float(np.sqrt(np.mean(clip ** 2))) + 1e-6) if len(clip) else -90.0)
    return out


def find_highlights(words, loud, opts, content, busy):
    """Pick the key moments: numbers, stressed words, words after cue phrases, repeated words, the user's own words."""
    manual = [stem(m) for m in re.split(r"[,،\n]", opts["highlight_words"]) if stem(m)]
    stems = [stem(w["text"]) for w in words]
    freq = Counter(t for t in stems if len(t) >= 3 and t not in AR_STOP and t not in STOPWORDS)
    median = sorted(loud)[len(loud) // 2] if loud else 0
    scored = []
    for i, w in enumerate(words):
        t, num = stems[i], parse_number(w["text"])
        is_num = num is not None or t in NUMBER_WORDS
        mine = any(m and (m in t or t in m) for m in manual) and len(t) >= 2
        if not (is_num or mine) and (len(t) < 3 or t in AR_STOP or t in STOPWORDS or t in FILLERS):
            continue
        score = 10 if mine else 0
        score += 4 if is_num else 0
        score += 2.5 if any(stems[j] in CUES for j in range(max(0, i - 3), i)) else 0
        score += min(2.0, (freq[t] - 1) * 0.7)
        score += max(0.0, min(3.0, (loud[i] - median) / 2)) if loud else 0
        score += min(1.0, (len(t) - 3) * 0.2)
        if score >= 2.5:
            scored.append((score, i, num))
    per = {"few": 20, "many": 7}.get(opts["highlight_density"], 12)
    limit = max(1, int(content // per))
    picked = []
    for score, i, num in sorted(scored, reverse=True):
        start = max(0.0, words[i]["start"] - 0.08)
        end = start + 1.7
        if end > content - 0.2 or any(start < b and end > a for a, b in busy) \
                or any(abs(start - p["start"]) < 6 or stems[p["index"]] == stems[i] for p in picked):
            continue
        picked.append({"start": start, "end": end, "index": i, "word": words[i]["text"], "number": num,
                       "kind": "number" if num is not None else "word"})
        if len(picked) >= limit:
            break
    return sorted(picked, key=lambda p: p["start"])


VAGUE = set("""biggest bigger small smaller good better great many much every each something anything everything
talk talking today tomorrow about take care need needs want wants said says very really make makes""".split())


def english_keyword(en_items, t):
    """Search term for a photo: the sentence's most repeated content word (plus the word after it)."""
    item = next((e for e in en_items if e["start"] - 0.3 <= t <= e["end"] + 0.3), None)
    if not item:
        return None
    every = Counter(x for e in en_items for x in re.findall(r"[a-z]+", e["text"].lower()))
    tokens = re.findall(r"[a-z]+", item["text"].lower())
    ok = [i for i, x in enumerate(tokens) if len(x) >= 4 and x not in STOPWORDS and x not in VAGUE]
    if not ok:
        return None
    best = max(ok, key=lambda i: (every[tokens[i]], len(tokens[i])))
    nxt = best + 1
    if nxt in ok:
        return f"{tokens[best]} {tokens[nxt]}"
    return tokens[best]


def fetch_pexels_photo(query, key, orientation, out_dir, log):
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"photo-{re.sub(r'[^a-z0-9]+', '-', query.lower())}.jpg")
    if os.path.exists(path):
        return path
    try:
        url = "https://api.pexels.com/v1/search?" + urllib.parse.urlencode(
            {"query": query, "per_page": 1, "orientation": orientation})
        req = urllib.request.Request(url, headers={"Authorization": key, "User-Agent": "raw-to-reel"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            photos = json.load(resp).get("photos", [])
        if not photos:
            return None
        req = urllib.request.Request(photos[0]["src"]["large"], headers={"User-Agent": "raw-to-reel"})
        with urllib.request.urlopen(req, timeout=60) as resp, open(path, "wb") as f:
            shutil.copyfileobj(resp, f)
        return path
    except Exception as exc:
        log(f"⚠️ صورة Pexels «{query}»: {str(exc)[:100]}")
        return None


def rounded_rect(bw, bh, r):
    bw, bh, r = int(bw), int(bh), int(min(r, bw / 2, bh / 2))
    return (f"m {r} 0 l {bw - r} 0 b {bw} 0 {bw} 0 {bw} {r} l {bw} {bh - r} b {bw} {bh} {bw} {bh} {bw - r} {bh} "
            f"l {r} {bh} b 0 {bh} 0 {bh} 0 {bh - r} l 0 {r} b 0 0 0 0 {r} 0")


def format_number(value, original):
    s = f"{int(round(value))}" if float(value).is_integer() or value >= 10 else f"{value:.1f}"
    if re.search("[٠-٩]", original):
        s = s.translate(str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩"))
    suffix = re.sub(r"[\d٠-٩۰-۹.,]+", "", original)
    return s + suffix if re.search(r"[\d٠-٩۰-۹]", original) else original


def highlight_events(hl, x, y, opts, w, h, context=""):
    """ASS events for one highlight: a pill that slams in with a burst of lines, plus the sentence under it."""
    text_c, box_c, hl_c, _ = PALETTES.get(opts["caption_color"], PALETTES["orange"])
    k = min(w, h) / 1080
    size = CAPTION_SIZES.get(opts["caption_size"], 92) * k * 1.6
    word = hl["word"] if hl["kind"] != "number" else format_number(hl["number"], hl["word"])
    size = min(size, 0.78 * w / max(1, len(word) * 0.55))
    bw, bh = len(word) * size * 0.55 + size * 0.9, size * 1.45
    s0, s1 = hl["start"], hl["end"]
    slam = "\\fscx20\\fscy20\\t(0,160,\\fscx112\\fscy112)\\t(160,270,\\fscx100\\fscy100)\\frz-4\\t(0,270,\\frz0)\\fad(0,220)"
    ev = [dialogue(s0, s1, "HL", f"{{\\an5\\pos({int(x)},{int(y)})\\p1\\bord0\\shad{int(5 * k)}\\1c{box_c}{slam}}}"
                                 + rounded_rect(bw, bh, bh * 0.28), 3)]
    if hl["kind"] == "number" and hl["number"]:
        steps = 14
        for i in range(steps):
            a, b = s0 + 0.05 * i, s0 + 0.05 * (i + 1)
            val = hl["number"] * (1 - (1 - (i + 1) / steps) ** 3)  # ease-out count up
            tags = f"\\an5\\pos({int(x)},{int(y)})\\fs{int(size)}" + (slam.replace("\\fad(0,220)", "") if i == 0 else "")
            ev.append(dialogue(a, b if i < steps - 1 else s1, "HL",
                               f"{{{tags}{chr(92)}fad(0,{220 if i == steps - 1 else 0})}}"
                               + ass_escape(format_number(val, hl["word"])), 4))
    else:
        ev.append(dialogue(s0, s1, "HL", f"{{\\an5\\pos({int(x)},{int(y)})\\fs{int(size)}{slam}}}" + ass_escape(word), 4))
    import math
    for i in range(8):
        ang = math.radians(22.5 + 45 * i)
        rx0, ry0, grow = bw / 2 + 8 * k, bh / 2 + 8 * k, 70 * k
        x0, y0 = x + math.cos(ang) * rx0, y + math.sin(ang) * ry0
        x1, y1 = x + math.cos(ang) * (rx0 + grow), y + math.sin(ang) * (ry0 + grow)
        length, thick = int(46 * k), max(4, int(9 * k))
        ev.append(dialogue(s0 + 0.08, s0 + 0.5, "HL",
                           f"{{\\an4\\move({int(x0)},{int(y0)},{int(x1)},{int(y1)},0,380)\\frz{-math.degrees(ang):.0f}"
                           f"\\p1\\bord0\\shad0\\1c{hl_c}\\t(180,380,\\alpha&HFF&)}}m 0 0 l {length} 0 l {length} {thick} l 0 {thick}", 3))
    if context:
        ev.append(dialogue(s0 + 0.15, s1, "HLSmall", f"{{\\an5\\pos({int(x)},{int(y + bh / 2 + size * 0.45)})\\fad(150,220)}}"
                           + ass_escape(context), 4))
    return ev


def punch_zoom_filter(windows, w, h):
    """zoompan expression: a quick 8% push-in towards the face during each highlight."""
    if not windows:
        return None
    env = "+".join(f"between(it,{a:.2f},{b:.2f})*min(1,(it-{a:.2f})/0.15)*min(1,({b:.2f}-it)/0.25)"
                   for a, b, _, _ in windows)
    cx = "+".join(f"between(it,{a:.2f},{b:.2f})*{fx:.3f}" for a, b, fx, _ in windows)
    cy = "+".join(f"between(it,{a:.2f},{b:.2f})*{fy:.3f}" for a, b, _, fy in windows)
    inside = "+".join(f"between(it,{a:.2f},{b:.2f})" for a, b, _, _ in windows)
    fx_e, fy_e = f"({cx}+(1-min(1,{inside}))*0.5)", f"({cy}+(1-min(1,{inside}))*0.4)"
    return (f"zoompan=z='1+0.08*({env})':x='clip(iw*{fx_e}-iw/zoom/2,0,iw-iw/zoom)'"
            f":y='clip(ih*{fy_e}-ih/zoom/2,0,ih-ih/zoom)':d=1:s={w}x{h}:fps={FPS}")


# ---------------------------------------------------------------- stickers & section titles

EMOJI = {
    "1f525": "نار قوي ناري حماس fire hot",
    "1f4a1": "فكره نصيحه اقتراح حل idea tip",
    "1f4b0": "فلوس مال سعر اسعار ربح ريال دولار درهم money price",
    "2705": "صح صحيح تمام اكيد نعم correct yes",
    "274c": "غلط خطا غلطه wrong",
    "26a0": "انتبه تحذير خطر احذر خطير warning careful",
    "2764": "حب قلب احب love",
    "1f602": "ضحك مضحك funny",
    "23f0": "وقت ساعه دقيقه دقايق time",
    "1f334": "نخل نخيل نخله نخلات palm",
    "1f331": "زراعه نبات بذر بذور شجر شجره مزرعه plant farm",
    "1f4a7": "ماء ماي مويه مي ري سقي water",
    "2600": "شمس صيف حر sun summer",
    "2744": "برد شتاء شتا ثلج winter cold",
    "2708": "سفر طياره رحله طيران travel flight",
    "1f3e0": "بيت منزل home house",
    "1f697": "سياره car",
    "1f4f1": "جوال تلفون هاتف موبايل phone",
    "1f4da": "تعلم كتاب دراسه درس learn book",
    "1f3c6": "نجاح فوز بطل success win",
    "1f680": "سريع صاروخ انطلاق fast launch",
    "2753": "سؤال ليش لماذا question",
    "1f4af": "١٠٠ 100 بالميه",
    "2b50": "مميز نجمه best star",
    "1f4aa": "قوه عضلات strong",
    "1f914": "تفكير فكر think",
    "1f631": "صدمه واو مستحيل wow",
    "1f604": "سعيد فرح فرحه happy",
    "1f4c8": "زياده نمو ارتفاع growth",
    "1f381": "هديه مجاني مجانا gift free",
    "1f4cd": "مكان موقع location",
    "1f37d": "اكل طعام اكله food",
    "1f44d": "حلو ممتاز رائع جميل good great",
    "1f64f": "شكرا الحمدلله دعاء thanks",
    "1f54c": "مسجد صلاه رمضان",
    "1f440": "شوف شوفوا انظر look",
    "1f389": "مبروك احتفال عيد congrats",
}
EMOJI_INDEX = {stem(k): code for code, keys in EMOJI.items() for k in keys.split()}

ORDINALS = {"اول": 1, "اولي": 1, "اولا": 1, "ثاني": 2, "ثانيه": 2, "ثانيا": 2, "ثالث": 3, "ثالثه": 3, "ثالثا": 3,
            "رابع": 4, "رابعه": 4, "رابعا": 4, "خامس": 5, "خامسه": 5, "خامسا": 5, "سادس": 6, "سادسه": 6,
            "سادسا": 6, "سابع": 7, "سابعه": 7, "ثامن": 8, "تاسع": 9, "عاشر": 10, "اخير": 0, "اخيره": 0, "اخيرا": 0}
COUNTS = {"واحد": 1, "اثنين": 2, "اثنان": 2, "ثلاثه": 3, "اربعه": 4, "خمسه": 5, "سته": 6, "سبعه": 7, "one": 1,
          "two": 2, "three": 3, "four": 4, "five": 5}
SECTION_NOUNS = {"خطوه", "نقطه", "طريقه", "سبب", "نصيحه", "قاعده", "مرحله", "سر", "غلطه", "خطا", "step", "tip"}
STANDALONE = {"اولا", "ثانيا", "ثالثا", "رابعا", "خامسا", "اخيرا"}


def emoji_for(text):
    t = stem(text)
    if len(t) < 2:
        return None
    if t in EMOJI_INDEX:
        return EMOJI_INDEX[t]
    for key, code in EMOJI_INDEX.items():
        if len(key) >= 4 and len(t) >= 4 and (t.startswith(key) or key.startswith(t)):
            return code
    return None


def find_stickers(words, highlights, content, busy=()):
    per = max(1, int(content // 6))
    hl_words = {hl["index"] for hl in highlights}
    cands = []
    for i, w in enumerate(words):
        code = emoji_for(w["text"])
        if code and os.path.exists(os.path.join(EMOJI_DIR, f"{code}.png")):
            cands.append((0 if i in hl_words else 1, w["start"], i, code))
    picked = []
    for _, start, i, code in sorted(cands):
        start = max(0.0, start - 0.05)
        if start + 1.5 > content or any(abs(start - p["start"]) < 4 for p in picked) \
                or any(start < b and start + 1.5 > a for a, b in busy):
            continue
        picked.append({"start": start, "end": start + 1.5, "index": i, "code": code})
        if len(picked) >= per:
            break
    return sorted(picked, key=lambda p: p["start"])


def find_sections(words):
    """'الخطوة الثانية', 'النقطة رقم ٣', 'أولاً', 'أخيراً'... -> titled sections."""
    stems = [stem(w["text"]) for w in words]
    out = []
    for i, t in enumerate(stems):
        label, num = None, None
        if t in STANDALONE:
            label = words[i]["text"]
        elif t in SECTION_NOUNS and i + 1 < len(words):
            nxt = stems[i + 1]
            if nxt == "رقم" and i + 2 < len(words):
                nxt = stems[i + 2]
            num = ORDINALS.get(nxt, COUNTS.get(nxt, parse_number(nxt)))
            if num is not None:
                noun = re.sub(r"[^\w\s]", "", words[i]["text"])
                label = f"{noun} {format_number(num, '٠')}" if num else words[i]["text"] + " " + words[i + 1]["text"]
        if label and (not out or words[i]["start"] - out[-1]["start"] > 4):
            nxt_words = [w["text"] for w in words[i + 1:i + 7] if stem(w["text"]) not in ORDINALS][:5]
            out.append({"start": max(0.0, words[i]["start"] - 0.1), "label": label.strip(), "sub": " ".join(nxt_words)})
    for s in out:
        s["end"] = s["start"] + 2.6
    return out


def section_events(sec, y, opts, w, h):
    text_c, box_c, hl_c, _ = PALETTES.get(opts["caption_color"], PALETTES["orange"])
    k = min(w, h) / 1080
    size = int(CAPTION_SIZES.get(opts["caption_size"], 92) * k * 1.05)
    bw, bh = max(len(sec["label"]) * size * 0.58, len(sec["sub"]) * size * 0.27) + size * 1.2, size * 2.1
    x_in, x_out = w + bw / 2, w - bw / 2 - 30 * k

    def mv(yy):  # slide in from the right edge, fade out at the end
        return f"\\move({int(x_in)},{int(yy)},{int(x_out)},{int(yy)},0,320)\\fad(0,300)"

    return [
        dialogue(sec["start"], sec["end"], "HL", f"{{\\an5{mv(y)}\\p1\\bord0\\shad{int(5 * k)}\\1c{box_c}}}"
                 + rounded_rect(bw, bh, bh * 0.22), 5),
        dialogue(sec["start"], sec["end"], "HL", f"{{\\an5{mv(y - bh * 0.18)}\\fs{size}\\1c{text_c}\\bord0\\shad0}}"
                 + ass_escape(sec["label"]), 6),
        dialogue(sec["start"] + 0.15, sec["end"], "HL", f"{{\\an5{mv(y + bh * 0.26)}\\fs{int(size * 0.48)}"
                 f"\\1c{text_c}\\alpha&H30&\\bord0\\shad0}}" + ass_escape(sec["sub"]), 6),
        dialogue(sec["start"], sec["end"], "HL", f"{{\\an5{mv(y - bh / 2 + 8 * k)}\\p1\\bord0\\shad0\\1c{hl_c}"
                 f"\\fscx0\\t(250,600,\\fscx100)}}" + rounded_rect(bw * 0.4, 8 * k, 4 * k), 6),
    ]


# ---------------------------------------------------------------- publishing: post text, translations

LANGS = {"fr": "Français", "tr": "Türkçe", "ur": "اردو", "fa": "فارسی", "es": "Español", "de": "Deutsch",
         "it": "Italiano", "pt": "Português", "ru": "Русский", "id": "Bahasa Indonesia", "hi": "हिन्दी", "zh": "中文"}


def sentences_of(words, gap=0.6):
    out, cur = [], []
    for w in words:
        if cur and (w["start"] - cur[-1]["end"] > gap or re.search(r"[.!?؟]$", cur[-1]["text"])):
            out.append(cur)
            cur = []
        if normalize_word(w["text"]) not in FILLERS:
            cur.append(w)
    if cur:
        out.append(cur)
    return [{"start": c[0]["start"], "text": " ".join(w["text"] for w in c)} for c in out if c]


def clock(t):
    t = int(t)
    return f"{t // 3600}:{t // 60 % 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60:02d}:{t % 60:02d}"


def build_post(words, hook, highlights, sections, stickers, opts, speed):
    """Caption for Instagram / TikTok / YouTube: title, key points, chapters, contact and hashtags."""
    sents = sentences_of(words)
    title = (opts["thumbnail_text"].strip() or hook
             or (" ".join(sents[0]["text"].split()[:10]) if sents else ""))
    code = stickers[0]["code"] if stickers else next((emoji_for(x) for x in title.split() if emoji_for(x)), None)
    icon = "".join(chr(int(c, 16)) for c in code.split("-")) if code else "🎬"
    keys = {stem(hl["word"]) for hl in highlights}
    points = [x for x in sents[1:] if keys & {stem(t) for t in x["text"].split()}][:3]
    if not points:
        points = sorted(sents[1:], key=lambda x: -len(x["text"]))[:3]
        points.sort(key=lambda x: x["start"])
    lines = [f"{icon} {title}".strip(), ""] + [f"• {x['text']}" for x in points]
    if sections:
        lines += ["", "📌 المحتوى:"]
        if sections[0]["start"] / speed > 1:
            lines.append("00:00 البداية")
        lines += [f"{clock(x['start'] / speed)} {x['label']}" for x in sections]
    if opts["end_card_contact"].strip():
        lines += ["", f"📲 {opts['end_card_title'] or 'تابعنا'}: {opts['end_card_contact'].strip()}"]
    forms = {}
    for w in words:
        t = stem(w["text"])
        if len(t) >= 3 and parse_number(t) is None and not any(
                t in group for group in (AR_STOP, STOPWORDS, FILLERS, CUES, ORDINALS, SECTION_NOUNS, STANDALONE)):
            forms.setdefault(t, Counter())[re.sub(r"[^\w]", "", w["text"])] += 1
    top = sorted(forms.items(), key=lambda kv: -sum(kv[1].values()))[:6]
    tags = [f"#{c.most_common(1)[0][0]}" for _, c in top] + ["#ريلز", "#اكسبلور"]
    lines += ["", " ".join(dict.fromkeys(tags))]
    return "\n".join(lines).strip() + "\n"


def translate_items(items, code, log):
    """English subtitle lines -> another language, fully offline after a one-time model download."""
    try:
        import argostranslate.package
        import argostranslate.translate
    except ImportError:
        log("تثبيت مكتبة الترجمة (مرة وحدة، حجمها كبير ~1-2 جيجا، يمكن ياخذ وقت)...")
        proc = subprocess.run([sys.executable, "-m", "pip", "install", "argostranslate"],
                              capture_output=True, encoding="utf-8", errors="replace")
        if proc.returncode:
            log(f"⚠️ تعذّر تثبيت مكتبة الترجمة: {proc.stderr[-200:]}")
            return None
        import argostranslate.package
        import argostranslate.translate

    def ready():
        langs = {lang.code: lang for lang in argostranslate.translate.get_installed_languages()}
        return "en" in langs and code in langs and langs["en"].get_translation(langs[code]) is not None

    if not ready():
        log(f"تنزيل نموذج الترجمة للإنجليزي ← {LANGS.get(code, code)} (مرة وحدة)...")
        argostranslate.package.update_package_index()
        pkg = next((p for p in argostranslate.package.get_available_packages()
                    if p.from_code == "en" and p.to_code == code), None)
        if not pkg:
            log(f"⚠️ ما فيه نموذج ترجمة لهذي اللغة ({code})")
            return None
        argostranslate.package.install_from_path(pkg.download())
    return [dict(it, text=argostranslate.translate.translate(it["text"], "en", code)) for it in items]


# ---------------------------------------------------------------- rendering

LOOKS = {
    "none": None,
    "auto": "eq=contrast=1.06:saturation=1.18:brightness=0.015:gamma=0.98",
    "warm": "colorbalance=rs=0.06:gs=0.01:bs=-0.06:rm=0.05:bm=-0.04,eq=saturation=1.12:contrast=1.04",
    "cool": "colorbalance=rs=-0.05:bs=0.07:rm=-0.03:bm=0.05,eq=saturation=1.05:contrast=1.04",
    "cinematic": "colorbalance=rs=-0.06:gs=-0.01:bs=0.08:rh=0.07:gh=0.02:bh=-0.07,"
                 "eq=contrast=1.12:saturation=0.95:gamma=0.96,vignette=PI/5",
    "bw": "hue=s=0,eq=contrast=1.15:brightness=0.02",
    "vivid": "eq=saturation=1.45:contrast=1.1,unsharp=5:5:0.6",
    "vintage": "curves=preset=vintage,eq=saturation=0.85,vignette=PI/4,noise=alls=6:allf=t",
}
TRANSITIONS = ("fade", "flash", "zoom", "whip")


def transition_filters(kind, n, count, dur, w, h):
    """Entrance/exit effects on one part, so cuts get a transition without changing the timeline."""
    mixed = kind == "mix"
    if mixed:
        kind = TRANSITIONS[n % len(TRANSITIONS)]
    first, last = n == 0, n == count - 1
    # in a mix, only dip to black when the next cut fades back in (a black dip into a white flash looks broken)
    last = last or (mixed and TRANSITIONS[(n + 1) % len(TRANSITIONS)] != "fade")
    f = []
    if kind == "fade":
        f += [] if first else ["fade=t=in:st=0:d=0.12"]
        f += [] if last else [f"fade=t=out:st={max(0.0, dur - 0.12):.2f}:d=0.12"]
    elif kind == "flash" and not first:
        f.append("fade=t=in:st=0:d=0.18:color=white")
    elif kind == "zoom" and not first:
        f.append(f"zoompan=z='if(lt(it,0.3),1.15-0.15*it/0.3,1)':x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2'"
                 f":d=1:s={w}x{h}:fps={FPS}")
    elif kind == "whip" and not first:
        f.append(f"zoompan=z='1+0.14*(1-min(1,it/0.25))':x='(iw-iw/zoom)*(1-min(1,it/0.25))'"
                 f":y='ih/2-ih/zoom/2':d=1:s={w}x{h}:fps={FPS}")
    return f + (["format=yuv420p"] if f else [])


def video_filter(opts, crop):
    vf = list(crop)
    look = LOOKS.get(opts.get("color_look") or ("auto" if opts["color"] else "none"))
    if look:
        vf.append(look)
    vf += [f"fps={FPS}", "setsar=1", "format=yuv420p"]
    return ",".join(vf)


# ---------------------------------------------------------------- face tracking

def track_faces(path, log, fps=2.0):
    """Face positions sampled `fps` times a second, normalised to the displayed frame; None if unavailable."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        log("⚠️ تتبّع الوجه يحتاج OpenCV: pip install opencv-python-headless")
        return None
    try:
        cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)
    except AttributeError:
        pass
    # exact frames every 1/fps s (the fps filter picks the nearest frame, up to half a step late)
    vf = f"select='isnan(prev_selected_t)+gte(t-prev_selected_t,{1 / fps - 0.001:.3f})',scale=480:-2"
    first = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-vf", vf, "-frames:v", "1",
                            "-f", "image2pipe", "-vcodec", "png", "-"], capture_output=True).stdout
    img = cv2.imdecode(np.frombuffer(first, np.uint8), cv2.IMREAD_COLOR) if first else None
    if img is None:
        return None
    fh, fw = img.shape[:2]
    try:
        # load from memory: OpenCV can't open files under non-ASCII paths (e.g. an Arabic Windows user name)
        with open(FACE_MODEL, "rb") as f:
            model = np.frombuffer(f.read(), np.uint8)
        try:
            detector = cv2.FaceDetectorYN.create("onnx", model, np.array([], np.uint8), (fw, fh), 0.6)
        except cv2.error:
            detector = cv2.FaceDetectorYN.create(FACE_MODEL, "", (fw, fh), 0.6)
    except Exception as exc:  # very old OpenCV without YuNet, or model missing
        log(f"⚠️ تعذّر تشغيل كاشف الوجه: {str(exc)[:120]}")
        return None

    proc = subprocess.Popen(["ffmpeg", "-v", "error", "-i", path, "-vf", vf, "-fps_mode", "passthrough", "-pix_fmt", "bgr24",
                             "-f", "rawvideo", "-"], stdout=subprocess.PIPE)
    size, samples, i = fw * fh * 3, [], 0
    while True:
        buf = proc.stdout.read(size)
        if len(buf) < size:
            break
        frame = np.frombuffer(buf, np.uint8).reshape(fh, fw, 3)
        _, faces = detector.detect(frame)
        box = None
        if faces is not None and len(faces):
            x, y, w, h = max(faces, key=lambda f: f[2] * f[3])[:4]
            box = (float((x + w / 2) / fw), float((y + h / 2) / fh), float(w / fw), float(h / fh))
        samples.append((i / fps, box))
        i += 1
    proc.wait()
    found = sum(1 for _, b in samples if b)
    if not found:
        return None
    return {"aspect": fw / fh, "samples": samples, "found": found / max(1, len(samples))}


def face_keyframes(track, start, end, step=0.5):
    """Smoothed face boxes for a segment: nearest detection fills gaps, then a moving average and a dead zone."""
    valid = [(t, b) for t, b in track["samples"] if b]
    raw, t = [], start
    while t <= end + 1e-6:
        raw.append((t - start, min(valid, key=lambda v: abs(v[0] - t))[1]))
        t += step
    # a jump in position or size is a shot change: never average across it
    shot, shots = 0, []
    for i, (_, b) in enumerate(raw):
        if i and (abs(b[0] - raw[i - 1][1][0]) > 0.15 or abs(b[1] - raw[i - 1][1][1]) > 0.15
                  or max(b[3], raw[i - 1][1][3]) > 1.6 * min(b[3], raw[i - 1][1][3])):
            shot += 1
        shots.append(shot)
    out, held = [], None
    for i in range(len(raw)):
        win = [b for j, (_, b) in enumerate(raw[max(0, i - 2):i + 3], max(0, i - 2)) if shots[j] == shots[i]]
        avg = tuple(sum(b[k] for b in win) / len(win) for k in range(4))
        if held is None or (i and shots[i] != shots[i - 1]) \
                or abs(avg[0] - held[0]) > 0.04 or abs(avg[1] - held[1]) > 0.06:
            held = avg
        out.append((raw[i][0], held))
    return out


def piecewise(points, ramp=0.6):
    """ffmpeg expression in t: hold each value, easing linearly into the next over `ramp` seconds."""
    expr = f"{points[-1][1]:.4f}"
    for (t0, v0), (t1, v1) in reversed(list(zip(points, points[1:]))):
        a = max(t0, t1 - ramp)
        expr = (f"if(lt(t,{a:.2f}),{v0:.4f},if(lt(t,{t1:.2f}),"
                f"{v0:.4f}+({v1 - v0:.4f})*(t-{a:.2f})/{t1 - a:.2f},{expr}))")
    return expr


def reframe(track, seg, w, h, zoom, follow):
    """Crop filters for one segment plus where the face lands in the output frame over time."""
    z = 1.15 if zoom else 1.0
    scale = f"scale={int(w * z) // 2 * 2}:{int(h * z) // 2 * 2}:force_original_aspect_ratio=increase"
    if not track:
        y = f"(ih-{h})/3" if zoom else f"(ih-{h})/2"
        return [scale, f"crop={w}:{h}:(iw-{w})/2:{y}"], []
    r = track["aspect"]
    sw, sh = (h * z * r, h * z) if r > w / h else (w * z, w * z / r)
    kfs = face_keyframes(track, seg["start"], seg["end"])
    if follow:
        cy = sorted(b[1] for _, b in kfs)[len(kfs) // 2]
        points = [kfs[0][:1] + (kfs[0][1][0],)]
        points += [(t, b[0]) for (t, b), (_, prev) in zip(kfs[1:], kfs) if b[0] != prev[0]]
        x_expr = f"clip(({piecewise(points)})*iw-ow/2,0,iw-ow)"
        y_expr = f"clip({cy:.4f}*ih-oh*0.4,0,ih-oh)"
        x_of = lambda b: min(max(b[0] * sw - w / 2, 0), sw - w)
        y_off = min(max(cy * sh - h * 0.4, 0), sh - h)
    else:
        x_expr, y_expr = f"(iw-{w})/2", (f"(ih-{h})/3" if zoom else f"(ih-{h})/2")
        x_of = lambda b: (sw - w) / 2
        y_off = (sh - h) / (3 if zoom else 2)
    faces = [(t, (b[0] * sw - x_of(b), b[1] * sh - y_off, b[2] * sw, b[3] * sh)) for t, b in kfs]
    return [scale, f"crop={w}:{h}:x='{x_expr}':y='{y_expr}'"], faces


# ---------------------------------------------------------------- smart text placement

ZONES = {"top": 0.17, "upper": 0.36, "lower": 0.66, "bottom": 0.78}  # bottom stays clear of the reels UI


def faces_in(faces, start, end):
    boxes = [b for t, b in faces if start - 0.15 <= t <= end + 0.15]
    if not boxes and faces:
        boxes = [min(faces, key=lambda f: abs(f[0] - (start + end) / 2))[1]]
    return boxes


def overlaps(y, half_h, half_w, boxes, w, h, body=True):
    """Does a text block centred at (w/2, y) cover a face (or, with body=True, the torso under it)?"""
    left, right = w / 2 - half_w, w / 2 + half_w
    for fx, fy, fw, fh in boxes:
        pad = 0.035 * h
        if y + half_h > fy - fh / 2 - pad and y - half_h < fy + fh / 2 + pad \
                and right > fx - fw / 2 - pad and left < fx + fw / 2 + pad:
            return True
        if body and y + half_h > fy + fh / 2 and right > fx - 1.4 * fw and left < fx + 1.4 * fw:
            return True
    return False


def text_block(text, size, w, wrap=0.84):
    width = len(text) * size * 0.5
    lines = max(1, -(-int(width) // int(w * wrap)))
    return lines * size * 0.72, min(w * wrap, width) / 2  # half height, half width


def choose_zone(boxes, half_h, half_w, w, h, order, prev=None, avoid=()):
    def free(name, body, face=True):
        y = ZONES[name] * h
        clash = any(abs(y - ay) < half_h + ah for ay, ah in avoid)
        return not clash and not (face and overlaps(y, half_h, half_w, boxes, w, h, body))
    if prev and free(prev, False):
        return prev
    # clear of face and torso > clear of face > clear of the face shown longest (text spanning a shot cut)
    # > at least not on top of other text
    for body, face in ((True, True), (False, True)):
        for name in order:
            if free(name, body, face):
                return name
    if len(boxes) > 1:
        main = max(boxes, key=lambda b: sum(abs(o[1] - b[1]) < 0.1 * h for o in boxes))
        for name in order:
            y = ZONES[name] * h
            if not any(abs(y - ay) < half_h + ah for ay, ah in avoid) and not overlaps(y, half_h, half_w, [main], w, h, False):
                return name
    for name in order:
        if free(name, False, False):
            return name
    return order[0]


def at(x, y):
    return f"{{\\an5\\pos({int(x)},{int(y)})}}"


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


def render_reel(job_dir, prefix, reel_no, segments, english, opts, assets, tracks, log, progress, extras=True):
    w, h = ASPECTS.get(opts["aspect"], ASPECTS["9:16"])
    _, _, _, box_hex = PALETTES.get(opts["caption_color"], PALETTES["orange"])
    parts_dir = os.path.join(job_dir, f"{prefix}_parts")
    os.makedirs(parts_dir, exist_ok=True)

    # 1. cut parts
    listing, boundaries, faces, offset = [], [], [], 0.0
    for n, seg in enumerate(segments):
        zoom = opts["zoom_cuts"] and n % 2 == 1
        if zoom and n:
            boundaries.append(offset)
        cmd = ["ffmpeg", "-y", "-v", "error", "-ss", str(seg["start"]), "-t", str(seg_len(seg)), "-i", seg["file"]]
        if not seg["has_audio"]:
            cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-shortest"]
        crop, seg_faces = reframe(tracks.get(seg["file"]), seg, w, h, zoom, opts["auto_reframe"])
        faces += [(offset + t, b) for t, b in seg_faces]
        vf = video_filter(opts, crop + [])
        trans = transition_filters(opts["transition"], n, len(segments), seg_len(seg), w, h)
        if trans:
            vf += "," + ",".join(trans)
        d = seg_len(seg)
        # 15 ms audio fades at every cut: no clicks where the waveform is chopped
        af = (f"aresample=48000,aformat=channel_layouts=stereo,afade=t=in:d=0.015,"
              f"afade=t=out:st={max(0.0, d - 0.015):.3f}:d=0.015")
        cmd += ["-vf", vf, "-af", af,
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

    brolls = plan_broll(assets.get("broll", []), content, reel_no)

    # 2. subtitles and motion highlights
    words = timeline_words(segments)
    chunks = caption_chunks(words) if opts["captions"] else []
    en_all = timeline_english(segments, english)
    en_items = en_all if opts["english_subs"] else []
    hook = ""
    if opts["text_hook"]:
        hook = opts["hook_text"].strip() or " ".join(w["text"] for w in words[:6])
    highlights = []
    if opts["motion_highlights"] and words:
        busy = [(b["start"], b["start"] + b["dur"]) for b in brolls] + ([(0.0, 3.2)] if hook else [])
        highlights = find_highlights(words, word_loudness(os.path.join(job_dir, joined), words), opts, content, busy)
        if opts["highlight_images"] and opts["pexels_key"].strip():
            orientation = {"16:9": "landscape", "1:1": "square"}.get(opts["aspect"], "landscape")
            for n, hl in enumerate(highlights):
                query = english_keyword(en_all, hl["start"]) if hl["kind"] == "word" and n % 2 == 0 else None
                photo = query and fetch_pexels_photo(query, opts["pexels_key"].strip(), orientation,
                                                     os.path.join(job_dir, "pexels"), log)
                if photo:
                    hl.update(kind="image", image=photo, end=hl["start"] + 2.6)
        n = len(highlights)
        label = {0: "ما لقيت كلمات مهمة", 1: "موشن على كلمة مهمة وحدة", 2: "موشن على كلمتين مهمتين"}.get(
            n, f"موشن على {n} كلمات مهمة" if n <= 10 else f"موشن على {n} كلمة مهمة")
        progress(label + (": " + "، ".join(hl["word"] for hl in highlights) if n else ""), 0.62)
    # the highlight card replaces the caption while it is on screen (the .srt keeps everything)
    shown = [c for c in chunks if not any(c["start"] < hl["end"] and c["end"] > hl["start"] for hl in highlights)]
    has_ass = bool(shown or en_items or hook or highlights)
    auto = opts["caption_position"] == "auto" and bool(faces)
    k = min(w, h) / 1080
    cap_size = CAPTION_SIZES.get(opts["caption_size"], 92) * k
    place, en_place, hook_place, cap_spots = None, [], "", []
    if auto:
        # captions: a zone clear of the face (and torso if possible), sticking with the last zone while it stays clear
        place, prev = [], None
        for c in shown:
            hh, hw = text_block(c["text"], cap_size, w)
            prev = choose_zone(faces_in(faces, c["start"], c["end"]), hh + 14 * k, hw, w, h,
                               ["lower", "upper", "top", "bottom"], prev)
            cap_spots.append((c["start"], c["end"], ZONES[prev] * h, hh + 14 * k))
            place.append(at(w / 2, ZONES[prev] * h))
        # english: just under the caption on screen at that moment, or above it if that covers the face
        en_size = cap_size * 0.58
        for e in en_items:
            hh, hw = text_block(e["text"], en_size, w, 0.8)
            mid = (e["start"] + e["end"]) / 2
            cy, ch = next(((y, ch) for s0, e0, y, ch in cap_spots if s0 <= mid <= e0 + 0.5), (ZONES["lower"] * h, cap_size))
            boxes = faces_in(faces, e["start"], e["end"])
            below, above = cy + ch + 12 * k + hh, cy - ch - 12 * k - hh
            y = below if below + hh < 0.93 * h and not overlaps(below, hh, hw, boxes, w, h, False) else above
            if overlaps(y, hh, hw, boxes, w, h, False) and not overlaps(below, hh, hw, boxes, w, h, False):
                y = below
            en_place.append(at(w / 2, y))
            cap_spots.append((e["start"], e["end"], y, hh))
        if hook:
            hh, hw = text_block(hook, 84 * k, w)
            busy = [(y, ch) for s0, _, y, ch in cap_spots if s0 < 3.0]
            zone = choose_zone(faces_in(faces, 0, 3.0), hh + 22 * k, hw, w, h, ["top", "upper", "bottom"], avoid=busy)
            hook_place = at(w / 2, ZONES[zone] * h)
    # highlights: a spot clear of the face and of any English line on screen at the time
    hl_size = cap_size * 1.6
    for hl in highlights:
        if hl["kind"] == "image":
            cw = int(w * (0.34 if w > h else 0.62)) // 2 * 2
            hl["card"] = (cw, int(cw * 0.72) // 2 * 2)
            half_h, half_w = (hl["card"][1] + 20 + hl_size * 1.5) / 2, (cw + 20) / 2
        else:
            half_h, half_w = hl_size * 0.73 + cap_size * 0.6, w * 0.4
        busy = [(y, hh) for s0, e0, y, hh in cap_spots if s0 < hl["end"] and e0 > hl["start"] and hh < cap_size]
        zone = choose_zone(faces_in(faces, hl["start"], hl["end"]), half_h, half_w, w, h,
                           ["upper", "top", "lower", "bottom"], avoid=busy) if faces else "upper"
        hl["y"] = min(max(ZONES[zone] * h, 0.08 * h + half_h), 0.88 * h - half_h)  # keep clear of the app UI
        hl["face"] = (faces_in(faces, hl["start"], hl["end"]) or [(w / 2, h * 0.4, 0, 0)])[0]

    # section titles ("الخطوة ٢"): slide in at a spot clear of the face and of the captions on screen
    sections = find_sections(words) if opts["section_titles"] and words else []
    for sec in sections:
        busy = [(y, hh) for s0, e0, y, hh in cap_spots if s0 < sec["end"] and e0 > sec["start"]]
        half_h = cap_size * 1.15
        zone = choose_zone(faces_in(faces, sec["start"], sec["end"]), half_h, w * 0.45, w, h,
                           ["top", "upper", "bottom", "lower"], avoid=busy) if faces else "top"
        sec["y"] = min(max(ZONES[zone] * h, 0.07 * h + half_h), 0.88 * h - half_h)

    # emoji stickers next to the words they illustrate, on the highlight card when there is one
    stickers, size_st = [], int(0.2 * min(w, h))
    if opts["stickers"] and words:
        default_y = {"top": 0.2, "middle": 0.5}.get(opts["caption_position"], 0.64) * h
        stickers = find_stickers(words, highlights, content, [(x["start"], x["end"]) for x in sections])
        for n, st in enumerate(stickers):
            hl = next((x for x in highlights if x["index"] == st["index"] and x["kind"] != "image"), None)
            if hl:
                fs = min(cap_size * 1.6, 0.78 * w / max(1, len(hl["word"]) * 0.55))
                bw, bh = len(hl["word"]) * fs * 0.55 + fs * 0.9, fs * 1.45
                spots = [(w / 2 + bw / 2 - size_st * 0.1, hl["y"] - cap_size * 0.3 - bh / 2 - size_st * 0.15)]
            else:
                cy, ch = next(((y, hh) for s0, e0, y, hh in cap_spots if s0 <= st["start"] + 0.2 <= e0 + 0.3),
                              (default_y, cap_size * 0.75))
                chunk = next((c for c in shown if c["start"] <= st["start"] + 0.2 <= c["end"] + 0.3), None)
                hw = text_block(chunk["text"], cap_size, w)[1] if chunk else w * 0.3
                side = 1 if n % 2 == 0 else -1
                above, below = cy - ch - size_st * 0.55, cy + ch + size_st * 0.55
                spots = [(w / 2 + side * hw * 0.8, above), (w / 2 - side * hw * 0.8, above),
                         (w / 2 + side * hw * 0.8, below), (w * 0.82, h * 0.22), (w * 0.18, h * 0.22)]
            boxes = faces_in(faces, st["start"], st["end"])

            def clear(x, y):
                inside = size_st / 2 <= x <= w - size_st / 2 and size_st / 2 <= y <= h - size_st / 2
                return inside and not any(abs(x - fx) < (size_st + fw) / 2 + 20 and abs(y - fy) < (size_st + fh) / 2 + 20
                                          for fx, fy, fw, fh in boxes)
            st["x"], st["y"] = next((p for p in spots if clear(*p)), spots[0])
            st["x"] = min(max(st["x"], size_st / 2), w - size_st / 2)
            st["y"] = min(max(st["y"], size_st / 2), h - size_st / 2)

    has_ass = has_ass or bool(sections)
    if has_ass:
        with open(os.path.join(job_dir, f"{prefix}.ass"), "w", encoding="utf-8") as f:
            f.write(ass_header(w, h, opts))
            f.writelines(caption_events(shown, opts, place))
            for hl in highlights:
                context = next((c["text"] for c in chunks if c["start"] <= hl["start"] + 0.1 <= c["end"] + 0.2), "")
                if hl["kind"] == "image":
                    ch = hl["card"][1] + 20
                    label_y = hl["y"] - (ch + hl_size * 1.5) / 2 + ch + hl_size * 0.75 + 6
                    f.writelines(highlight_events(dict(hl, kind="word"), w / 2, label_y, opts, w, h))
                else:
                    f.writelines(highlight_events(hl, w / 2, hl["y"] - cap_size * 0.3, opts, w, h,
                                                  context if context != hl["word"] else ""))
            f.writelines(dialogue(e["start"], e["end"], "English", (en_place[i] if en_place else "")
                                  + ass_escape(e["text"]), 1) for i, e in enumerate(en_items))
            if hook:
                f.write(dialogue(0, min(3.0, content), "Hook", hook_place + "{\\fad(0,250)}" + ass_escape(hook), 2))
            for sec in sections:
                f.writelines(section_events(sec, sec["y"], opts, w, h))
    if chunks:
        write_srt(os.path.join(job_dir, f"{prefix}.srt"), chunks)
    if en_items:
        write_srt(os.path.join(job_dir, f"{prefix}_en.srt"), en_items)
    srt_tr = None
    if extras and opts["translate_to"] and en_all:
        try:
            translated = translate_items(en_all, opts["translate_to"], log)
        except Exception as exc:  # network / model problems shouldn't stop the edit
            log(f"⚠️ الترجمة ({opts['translate_to']}) ما اشتغلت: {str(exc)[:150]}")
            translated = None
        if translated:
            srt_tr = f"{prefix}_{opts['translate_to']}.srt"
            write_srt(os.path.join(job_dir, srt_tr), translated)
    elif extras and opts["translate_to"]:
        log("⚠️ الترجمة للغة ثانية تحتاج تفريغ الصوت والترجمة الإنجليزية، وما توفروا")

    # 3. thumbnail (from the clean cut, before captions are burned in)
    thumb = None
    if opts["thumbnail"]:
        title = opts["thumbnail_text"].strip() or hook or " ".join(w["text"] for w in words[:5])
        vf = "thumbnail=120"
        if title:
            with open(os.path.join(job_dir, f"{prefix}_thumb.ass"), "w", encoding="utf-8") as f:
                f.write(ass_header(w, h, opts))
                pos = ""
                if faces:
                    hh, hw = text_block(title, 128 * k, w, 0.87)
                    zone = choose_zone(faces_in(faces, 0, min(12.0, content)), hh + 26 * k, hw, w, h,
                                       ["bottom", "top", "upper", "lower"])
                    pos = at(w / 2, ZONES[zone] * h)
                f.write(dialogue(0, 36000, "Thumb", pos + ass_escape(title)))
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
    zooms = [(hl["start"], hl["end"], hl["face"][0] / w, hl["face"][1] / h)
             for hl in highlights if hl["kind"] != "image"] if opts["punch_zoom"] else []
    if zooms:
        vchain.append(f"[{v}]{punch_zoom_filter(zooms, w, h)}[vz]")
        v = "vz"
    for j, b in enumerate(brolls):
        src = add_input("-loop", "1", "-t", str(b["dur"]), "-i", b["path"]) if is_image(b["path"]) \
            else add_input("-t", str(b["dur"]), "-i", b["path"])
        fade_out = max(0.0, b["dur"] - 0.2)
        vchain.append(f"[{src}:v]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1,"
                      f"fps={FPS},format=yuva420p,fade=in:st=0:d=0.2:alpha=1,fade=out:st={fade_out}:d=0.2:alpha=1,"
                      f"setpts=PTS-STARTPTS+{b['start']}/TB[b{j}]")
        vchain.append(f"[{v}][b{j}]overlay=eof_action=pass:enable='between(t,{b['start']},{b['start'] + b['dur']})'[vb{j}]")
        v = f"vb{j}"
    for j, hl in enumerate(hl for hl in highlights if hl["kind"] == "image"):
        cw, ch = hl["card"]
        d, t0 = hl["end"] - hl["start"], hl["start"]
        top = int(hl["y"] - (ch + 20 + hl_size * 1.5) / 2)
        src = add_input("-loop", "1", "-framerate", str(FPS), "-t", f"{d:.2f}", "-i", hl["image"])
        vchain.append(f"[{src}:v]scale={int(cw * 1.12) // 2 * 2}:{int(ch * 1.12) // 2 * 2}:force_original_aspect_ratio=increase,"
                      f"crop={cw}:{ch}:x='(iw-ow)*min(1,t/{d:.2f})':y='(ih-oh)/2',pad={cw + 20}:{ch + 20}:10:10:white,"
                      f"setsar=1,format=yuva420p,fade=in:st=0:d=0.2:alpha=1,fade=out:st={d - 0.25:.2f}:d=0.25:alpha=1,"
                      f"setpts=PTS-STARTPTS+{t0:.2f}/TB[hi{j}]")
        vchain.append(f"[{v}][hi{j}]overlay=x='(W-w)/2+W*0.7*pow(max(0,1-(t-{t0:.2f})/0.3),2)':y={top}"
                      f":eof_action=pass:enable='between(t,{t0:.2f},{t0 + d:.2f})'[vh{j}]")
        v = f"vh{j}"
    if has_ass:
        vchain.append(f"[{v}]ass={prefix}.ass:fontsdir=fonts[vs]")
        v = "vs"
    for j, st in enumerate(stickers):
        d, t0, r = st["end"] - st["start"], st["start"], int(size_st * 1.42)
        src = add_input("-loop", "1", "-framerate", str(FPS), "-t", f"{d:.2f}", "-i",
                        os.path.join(EMOJI_DIR, st["code"] + ".png"))
        vchain.append(f"[{src}:v]scale={size_st}:{size_st},format=rgba,rotate='0.14*sin(2*PI*1.6*t)':c=none"
                      f":ow={r}:oh={r},fade=in:st=0:d=0.15:alpha=1,fade=out:st={d - 0.3:.2f}:d=0.3:alpha=1,"
                      f"setpts=PTS-STARTPTS+{t0:.2f}/TB[sk{j}]")
        vchain.append(f"[{v}][sk{j}]overlay=x={int(st['x'] - r / 2)}:y='{int(st['y'] - r / 2)}"
                      f"+80*pow(max(0,1-(t-{t0:.2f})/0.3),2)':eof_action=pass"
                      f":enable='between(t,{t0:.2f},{t0 + d:.2f})'[vk{j}]")
        v = f"vk{j}"
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
    # apad to the exact length: amix (duration=first) otherwise drops the voice's last ~0.2 s
    achain.append(f"[0:a]{voice},apad=whole_dur={total:.3f}[a0]")
    a = "a0"
    if opts["sfx"]:
        # a different sound for each kind of moment, at most one every 0.8 s
        events = ([(t, "whoosh") for t in boundaries] + [(b["start"], "whoosh") for b in brolls]
                  + [(hl["start"], "ding" if hl["kind"] == "number" else "swoosh" if hl["kind"] == "image" else "boom")
                     for hl in highlights]
                  + [(st["start"], "pop") for st in stickers] + [(x["start"], "swoosh") for x in sections]
                  + ([(content, "whoosh")] if end_card else []))
        hits = []
        for t, name in sorted(events):
            if t > 0.3 and (not hits or t - hits[-1][0] > 0.8):
                hits.append((t, name))
        gain = {"whoosh": 0.35, "swoosh": 0.3, "boom": 0.22, "ding": 0.25, "pop": 0.35, "click": 0.3}
        lead = {"whoosh": 0.2, "swoosh": 0.15}
        mixed = []
        for name in sorted({n for _, n in hits}):
            times = [t for t, n in hits if n == name]
            src = add_input("-i", library.ensure_sfx(name, LIB_DIR))
            achain.append(f"[{src}:a]aformat=sample_rates=48000:channel_layouts=stereo,asplit={len(times)}"
                          + "".join(f"[{name}{i}]" for i in range(len(times))))
            for i, t in enumerate(times):
                ms = int(max(0.0, t - lead.get(name, 0.0)) * 1000)
                achain.append(f"[{name}{i}]adelay={ms}|{ms},volume={gain[name]}[d{name}{i}]")
                mixed.append(f"[d{name}{i}]")
        if mixed:
            achain.append(f"[{a}]" + "".join(mixed) + f"amix=inputs={len(mixed) + 1}:duration=first:normalize=0[asfx]")
            a = "asfx"
    music = assets.get("music")
    if opts["music"] and music:
        src = add_input("-stream_loop", "-1", "-i", music)
        fades = f"afade=t=in:d=1,afade=t=out:st={max(0.0, total - 1.5):.2f}:d=1.5"
        if opts["music_ducking"]:
            # music sits higher in the pauses and dips under the voice while you talk
            achain.append(f"[{a}]asplit=2[vmain][vkey]")
            achain.append(f"[{src}:a]aformat=sample_rates=48000:channel_layouts=stereo,volume=0.3,{fades}[mraw]")
            achain.append("[mraw][vkey]sidechaincompress=threshold=0.02:ratio=12:attack=40:release=600:makeup=1[m]")
            achain.append("[vmain][m]amix=inputs=2:duration=first:normalize=0[amus]")
        else:
            achain.append(f"[{src}:a]aformat=sample_rates=48000:channel_layouts=stereo,volume=0.12,{fades}[m]")
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

    post = None
    if extras and opts["post_text"] and words:
        post = build_post(words, hook, highlights, sections, stickers, opts, speed)
        with open(os.path.join(job_dir, f"{prefix}_post.txt"), "w", encoding="utf-8-sig") as f:
            f.write(post)

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
        "highlights": [hl["word"] for hl in highlights],
        "srt_translated": srt_tr,
        "post": post,
        "post_file": f"{prefix}_post.txt" if post else None,
        "stickers": len(stickers),
        "sections": [{"t": round(x["start"] / speed, 1), "label": x["label"]} for x in sections],
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

def normalize_options(options):
    opts = {**DEFAULT_OPTIONS, **(options or {})}
    for key in ("speed", "target_length", "reel_length"):
        opts[key] = float(opts[key] or 0)
    opts["speed"] = opts["speed"] or 1.0
    opts["reel_length"] = opts["reel_length"] or 60
    return opts


def load_dictionary():
    try:
        with open(DICT_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_dictionary(entries):
    os.makedirs(os.path.dirname(DICT_FILE), exist_ok=True)
    with open(DICT_FILE, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)


def learn_corrections(pairs):
    """Remember wrong -> right word fixes made on the review page."""
    entries = load_dictionary()
    for wrong, right in pairs:
        if norm_ar(wrong) and right.strip() and norm_ar(wrong) != norm_ar(right):
            entries[norm_ar(wrong)] = right.strip()
    save_dictionary(entries)


def make_emitter(log):
    def emit(msg, progress=None):
        log(msg, progress) if log is not print else print(msg)
    return emit


def analyze(job_dir, inputs, options, log=print):
    """Transcribe, plan the cuts and track faces. Saves plan.json for the review page and render_plan()."""
    opts = normalize_options(options)
    emit = make_emitter(log)
    os.makedirs(job_dir, exist_ok=True)
    emit("بدء التحليل...", 0.05)
    segments, english = plan_cuts(inputs, opts, emit)
    if not segments:
        raise RuntimeError("لم يتبقَّ أي جزء من الفيديو بعد القص. جرّب إيقاف حذف الصمت.")

    fixes = load_dictionary()
    n_fixed, next_id = 0, 0
    for seg in segments:
        for w in seg["words"]:
            w["id"] = next_id
            next_id += 1
            right = fixes.get(norm_ar(w["text"]))
            if right:
                w["text"], n_fixed = right, n_fixed + 1
    if n_fixed:
        emit(f"القاموس صحّح {n_fixed} كلمة")

    tracks = {}
    if opts["auto_reframe"] or opts["caption_position"] == "auto":
        for path in inputs:
            emit(f"تتبّع الوجه في {os.path.basename(path)}...")
            tracks[path] = track_faces(path, emit)
            if tracks[path]:
                emit(f"  الوجه ظاهر في {tracks[path]['found'] * 100:.0f}% من الفيديو")
            else:
                emit("  ما لقيت وجه، بيكون القص من النص والكتابة في مكانها العادي")

    plan = {"inputs": inputs, "segments": segments, "english": english, "tracks": tracks,
            "input_seconds": round(sum(probe(p)["duration"] for p in inputs), 2)}
    with open(os.path.join(job_dir, "plan.json"), "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False)
    emit(f"التحليل خلص: {len(segments)} لقطة، {plan['input_seconds']:.1f}ث ← "
         f"{sum(seg_len(s) for s in segments):.1f}ث", 0.3)
    return plan


def load_plan(job_dir):
    with open(os.path.join(job_dir, "plan.json"), encoding="utf-8") as f:
        plan = json.load(f)
    plan["english"] = {int(k): v for k, v in (plan.get("english") or {}).items()}
    return plan


def review_summary(plan):
    """What the review page shows: kept words per segment, with source times for the player."""
    return [{"seg": i, "file": os.path.basename(s["file"]), "index": s["index"], "start": s["start"], "end": s["end"],
             "words": [{"id": w["id"], "text": w["text"], "start": round(w["start"], 2)} for w in s["words"]]}
            for i, s in enumerate(plan["segments"])]


def apply_edits(segments, edits):
    """Review page edits: remove words (cut them out of the video) and correct word text."""
    removed = set(edits.get("removed") or [])
    fixes = {int(k): v for k, v in (edits.get("corrections") or {}).items()}
    removed_segs = set(edits.get("removed_segments") or [])
    out = []
    for i, seg in enumerate(segments):
        if i in removed_segs:
            continue
        words = [dict(w, text=fixes.get(w["id"], w["text"])) for w in seg["words"]]
        cuts = [(w["start"] - 0.03, w["end"] + 0.03) for w in words if w["id"] in removed]
        for s0, e0 in subtract((seg["start"], seg["end"]), cuts) if cuts else [(seg["start"], seg["end"])]:
            out.append(dict(seg, start=round(s0, 3), end=round(e0, 3),
                            words=[w for w in words if w["id"] not in removed
                                   and w["start"] >= s0 - 0.05 and w["end"] <= e0 + 0.05]))
    return out


def extra_aspects(opts):
    wanted = [a for key, a in (("also_4x5", "4:5"), ("also_1x1", "1:1"), ("also_16x9", "16:9")) if opts.get(key)]
    return [a for a in wanted if a != opts["aspect"]]


def render_plan(job_dir, plan, options, assets=None, edits=None, preview=False, log=print):
    opts = normalize_options(options)
    emit = make_emitter(log)
    assets = dict(assets or {})
    assets["broll"] = list(assets.get("broll") or []) if opts["broll"] else []
    os.makedirs(os.path.join(job_dir, "fonts"), exist_ok=True)
    if os.path.exists(FONT_FILE):
        shutil.copy(FONT_FILE, os.path.join(job_dir, "fonts"))
    if opts["music"] and opts["music_source"] in library.TRACKS:
        emit(f"موسيقى من المكتبة: {library.TRACKS[opts['music_source']]}")
        assets["music"] = library.ensure_track(opts["music_source"], LIB_DIR)
    for opt, asset, label in (("music", "music", "موسيقى"), ("logo", "logo", "شعار")):
        if opts[opt] and not assets.get(asset):
            emit(f"⚠️ مفعّل «{label}» بس ما رفعت ملف، بيتم التخطي")
    if opts["broll"] and not assets["broll"]:
        emit("⚠️ مفعّل «B-roll خاص» بس ما رفعت صور أو مقاطع")

    segments = apply_edits(plan["segments"], edits or {})
    if not segments:
        raise RuntimeError("حذفت كل شي! رجّع بعض الكلام وجرّب مرة ثانية.")
    english, tracks = plan["english"], plan["tracks"]
    if preview:
        trimmed, total = [], 0.0
        for seg in segments:
            if total >= PREVIEW_SECONDS:
                break
            if seg_len(seg) > PREVIEW_SECONDS - total:
                seg = split_segment(seg, seg["start"] + PREVIEW_SECONDS - total)[0]
            trimmed.append(seg)
            total += seg_len(seg)
        segments = trimmed
        opts.update(split_reels=False, thumbnail=False, end_card=False)
    else:
        write_resolve_exports(job_dir, segments)
    total_out = sum(seg_len(s) for s in segments)

    if opts["pexels"] and not preview:
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
    emit(("معاينة أول 15 ثانية..." if preview else f"المونتاج: {len(segments)} لقطة، {total_out:.1f}ث")
         + (f"، {len(reels)} ريلز" if len(reels) > 1 else ""), 0.32)

    results = []
    for k, reel in enumerate(reels):
        prefix = "preview" if preview else ("final" if len(reels) == 1 else f"reel_{k + 1}")
        base = 0.32 + 0.66 * k / len(reels)

        def progress(msg, frac, base=base, k=k):
            label = f"[ريل {k + 1}/{len(reels)}] " if len(reels) > 1 else ""
            emit(label + msg, base + 0.66 * frac / len(reels))

        main = render_reel(job_dir, prefix, k, reel, english, opts, assets, tracks, emit, progress)
        main["variants"] = []
        for aspect in ([] if preview else extra_aspects(opts)):
            emit(f"نسخة بمقاس {aspect}...")
            v = render_reel(job_dir, f"{prefix}_{aspect.replace(':', 'x')}", k, reel, english,
                            dict(opts, aspect=aspect), assets, tracks, emit, lambda *a: None, extras=False)
            main["variants"].append({"aspect": aspect, "final": v["final"], "thumbnail": v["thumbnail"]})
        results.append(main)

    result = {
        "preview": preview,
        "reels": results,
        "final": results[0]["final"],
        "edl": None if preview else "timeline.edl",
        "resolve_script": None if preview else "resolve_import.py",
        "captions_missing": bool(opts["captions"] and not any(r["srt"] for r in results)),
        "segments": len(segments),
        "input_seconds": plan["input_seconds"],
        "output_seconds": round(sum(r["seconds"] for r in results), 2),
        "options": {k: v for k, v in opts.items() if k != "pexels_key"},
    }
    with open(os.path.join(job_dir, "preview.json" if preview else "result.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    emit("✅ المعاينة جاهزة" if preview else
         ("✅ تم! الفيديو جاهز" if len(results) == 1 else f"✅ تم! {len(results)} ريلز جاهزة"), 1.0)
    return result


def process(job_dir, inputs, options, assets=None, log=print):
    """One click: analyse and render straight away."""
    return render_plan(job_dir, analyze(job_dir, inputs, options, log), options, assets, log=log)


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
