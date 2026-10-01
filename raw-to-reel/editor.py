"""Raw-to-Reel editing pipeline (ffmpeg based).

Turns one or more raw talking-head clips into a vertical 1080x1920 reel:
cuts silences / filler words / bad takes, adds zoom cuts, color, studio
sound, burned-in captions, a text hook and background music, and exports
a cut list for DaVinci Resolve (EDL + a Resolve scripting file).
"""

import json
import os
import re
import shutil
import subprocess

W, H, FPS = 1080, 1920, 30
HERE = os.path.dirname(os.path.abspath(__file__))
FONT_FILE = os.path.join(HERE, "fonts", "Qatar2022Arabic-Bold.ttf")
FONT_NAME = "Qatar2022 Arabic"

FILLERS = {
    "um", "umm", "uh", "uhh", "uhm", "er", "erm", "ah", "hmm", "mm",
    "امم", "ام", "اه", "آه", "ااه", "يعني", "إمم", "اممم",
}

DEFAULT_OPTIONS = {
    "remove_bad_takes": True,
    "remove_fillers": True,
    "cut_silences": True,
    "captions": True,
    "zoom_cuts": True,
    "music": False,
    "studio_sound": True,
    "color": True,
    "text_hook": False,
    "hook_text": "",
    "target_length": 0,  # seconds, 0 = auto
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
        fps = float(num) / float(den)
    return {
        "duration": float(info["format"]["duration"]),
        "fps": fps,
        "has_audio": has_audio,
        "width": int(video["width"]) if video else 0,
        "height": int(video["height"]) if video else 0,
    }


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


def transcribe(path, model_name, language, log):
    """Word-level transcript, or None if no Whisper backend is usable."""
    try:
        return _transcribe(path, model_name, language or None, log)
    except Exception as exc:  # model download / decode failures shouldn't kill the edit
        log(f"⚠️ تعذّر تفريغ الصوت ({type(exc).__name__}): {str(exc)[:160]}")
        return None


def _transcribe(path, model_name, lang, log):
    try:
        from faster_whisper import WhisperModel

        log(f"تفريغ الصوت بـ faster-whisper ({model_name})...")
        model = WhisperModel(model_name, device="auto", compute_type="int8")
        segments, _ = model.transcribe(path, language=lang, word_timestamps=True)
        return [
            {"start": w.start, "end": w.end, "text": w.word.strip()}
            for seg in segments for w in (seg.words or [])
        ]
    except ImportError:
        pass
    try:
        import whisper

        log(f"تفريغ الصوت بـ whisper ({model_name})...")
        model = whisper.load_model(model_name)
        result = model.transcribe(path, language=lang, word_timestamps=True)
        return [
            {"start": w["start"], "end": w["end"], "text": w["word"].strip()}
            for seg in result["segments"] for w in seg.get("words", [])
        ]
    except ImportError:
        return None


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
    """Return the ordered list of kept segments across all inputs."""
    segments = []
    for idx, path in enumerate(inputs):
        info = probe(path)
        name = os.path.basename(path)
        log(f"تحليل {name} ({info['duration']:.1f} ث)")

        words = None
        needs_words = opts["captions"] or opts["remove_fillers"] or opts["remove_bad_takes"] \
            or (opts["text_hook"] and not opts["hook_text"])
        if needs_words and info["has_audio"]:
            words = transcribe(path, opts["whisper_model"], opts["language"], log)
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
            segments.append({"file": path, "index": idx, "start": round(s, 3),
                             "end": round(e, 3), "fps": info["fps"], "words": seg_words})

    target = float(opts.get("target_length") or 0)
    if target > 0:
        total, trimmed = 0.0, []
        for seg in segments:
            if total >= target:
                break
            room = target - total
            if seg["end"] - seg["start"] > room:
                seg = dict(seg, end=round(seg["start"] + room, 3))
                seg["words"] = [w for w in seg["words"] if w["end"] <= seg["end"]]
            trimmed.append(seg)
            total += seg["end"] - seg["start"]
        segments = trimmed
    return segments


def ass_time(t):
    cs = int(round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def srt_time(t):
    ms = int(round(t * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def timeline_words(segments):
    """Map each kept word onto the output timeline."""
    out, offset = [], 0.0
    for seg in segments:
        for w in seg["words"]:
            out.append({"text": w["text"],
                        "start": offset + max(0.0, w["start"] - seg["start"]),
                        "end": offset + min(seg["end"], w["end"]) - seg["start"]})
        offset += seg["end"] - seg["start"]
    return out


def caption_chunks(words, size=3):
    chunks = []
    for i in range(0, len(words), size):
        group = [w for w in words[i:i + size] if normalize_word(w["text"]) not in FILLERS]
        if group:
            chunks.append({"text": " ".join(w["text"] for w in group),
                           "start": group[0]["start"], "end": group[-1]["end"]})
    for a, b in zip(chunks, chunks[1:]):
        a["end"] = min(max(a["end"], a["start"] + 0.3), b["start"])
    return chunks


def ass_escape(text):
    return text.replace("\\", "\\\\").replace("{", "(").replace("}", ")")


def write_subtitles(job_dir, chunks, hook, duration):
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,{FONT_NAME},92,&H00FFFFFF,&H00FFFFFF,&H001E50E8,&H001E50E8,1,0,0,0,100,100,0,0,3,14,0,2,80,80,620,1
Style: Hook,{FONT_NAME},84,&H001E50E8,&H001E50E8,&H00FFFFFF,&H00FFFFFF,1,0,0,0,100,100,0,0,3,22,0,8,80,80,260,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    for c in chunks:
        lines.append(f"Dialogue: 0,{ass_time(c['start'])},{ass_time(c['end'])},Caption,,0,0,0,,"
                     f"{{\\fscx85\\fscy85\\t(0,90,\\fscx100\\fscy100)}}{ass_escape(c['text'])}")
    if hook:
        lines.append(f"Dialogue: 1,{ass_time(0)},{ass_time(min(3.0, duration))},Hook,,0,0,0,,"
                     f"{{\\fad(0,250)}}{ass_escape(hook)}")
    with open(os.path.join(job_dir, "captions.ass"), "w", encoding="utf-8") as f:
        f.write(header + "\n".join(lines) + "\n")
    with open(os.path.join(job_dir, "captions.srt"), "w", encoding="utf-8") as f:
        for i, c in enumerate(chunks, 1):
            f.write(f"{i}\n{srt_time(c['start'])} --> {srt_time(c['end'])}\n{c['text']}\n\n")


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
        dur = seg["end"] - seg["start"]
        name = os.path.basename(seg["file"])
        lines.append(f"{n:03d}  AX       AA/V  C        {tc(seg['start'], fps)} {tc(seg['end'], fps)} "
                     f"{tc(rec, fps)} {tc(rec + dur, fps)}")
        lines.append(f"* FROM CLIP NAME: {name}")
        lines.append("")
        rec += dur
    with open(os.path.join(job_dir, "timeline.edl"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    cuts = [{"file": os.path.abspath(s["file"]), "start": s["start"], "end": s["end"], "fps": s["fps"]}
            for s in segments]
    with open(os.path.join(job_dir, "cuts.json"), "w", encoding="utf-8") as f:
        json.dump(cuts, f, ensure_ascii=False, indent=2)
    shutil.copy(os.path.join(HERE, "resolve_import.py"), os.path.join(job_dir, "resolve_import.py"))


def video_filter(opts, zoom):
    vf = [f"scale={W}:{H}:force_original_aspect_ratio=increase", f"crop={W}:{H}"]
    if zoom:
        vf += [f"scale={int(W * 1.15)}:{int(H * 1.15)}", f"crop={W}:{H}:(iw-{W})/2:(ih-{H})/3"]
    if opts["color"]:
        vf.append("eq=contrast=1.06:saturation=1.18:brightness=0.015:gamma=0.98")
    vf += [f"fps={FPS}", "setsar=1", "format=yuv420p"]
    return ",".join(vf)


def render(job_dir, segments, opts, music, log):
    parts_dir = os.path.join(job_dir, "parts")
    os.makedirs(parts_dir, exist_ok=True)
    listing = []
    for n, seg in enumerate(segments):
        part = os.path.join(parts_dir, f"{n:04d}.mp4")
        zoom = opts["zoom_cuts"] and n % 2 == 1
        cmd = ["ffmpeg", "-y", "-v", "error", "-ss", str(seg["start"]), "-t",
               str(seg["end"] - seg["start"]), "-i", seg["file"]]
        if not probe(seg["file"])["has_audio"]:
            cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-shortest"]
        cmd += ["-vf", video_filter(opts, zoom), "-af", "aresample=48000,aformat=channel_layouts=stereo",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "19",
                "-c:a", "aac", "-b:a", "192k", part]
        run(cmd)
        listing.append(f"file 'parts/{n:04d}.mp4'")  # relative: avoids Windows backslash/encoding issues
        log(f"قص المقطع {n + 1}/{len(segments)}", progress=0.3 + 0.5 * (n + 1) / len(segments))

    with open(os.path.join(job_dir, "parts.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(listing))
    joined = os.path.join(job_dir, "joined.mp4")
    run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", "parts.txt",
         "-c", "copy", joined], cwd=job_dir)

    log("المعالجة النهائية (ترجمة، صوت، موسيقى)...", progress=0.85)
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", "joined.mp4"]
    if music:
        cmd += ["-stream_loop", "-1", "-i", music]

    voice = "anull"
    if opts["studio_sound"]:
        voice = ("highpass=f=80,lowpass=f=14000,afftdn=nf=-25,"
                 "acompressor=threshold=-20dB:ratio=3:attack=5:release=80,"
                 "loudnorm=I=-14:TP=-1.5:LRA=9")
    if music:
        afilter = (f"[0:a]{voice}[v];[1:a]volume=0.12,afade=t=in:d=1[m];"
                   "[v][m]amix=inputs=2:duration=first:normalize=0[a]")
    else:
        afilter = f"[0:a]{voice}[a]"

    vfilter = "[0:v]null[vo]"
    if os.path.exists(os.path.join(job_dir, "captions.ass")):
        vfilter = "[0:v]ass=captions.ass:fontsdir=fonts[vo]"
    cmd += ["-filter_complex", f"{vfilter};{afilter}", "-map", "[vo]", "-map", "[a]",
            "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart", "final.mp4"]
    run(cmd, cwd=job_dir)
    shutil.rmtree(parts_dir, ignore_errors=True)
    os.remove(joined)
    os.remove(os.path.join(job_dir, "parts.txt"))


def process(job_dir, inputs, options, music=None, log=print):
    opts = {**DEFAULT_OPTIONS, **(options or {})}
    os.makedirs(job_dir, exist_ok=True)
    os.makedirs(os.path.join(job_dir, "fonts"), exist_ok=True)
    if os.path.exists(FONT_FILE):
        shutil.copy(FONT_FILE, os.path.join(job_dir, "fonts"))

    def emit(msg, progress=None):
        log(msg, progress) if log is not print else print(msg)

    emit("بدء التحليل...", 0.05)
    segments = plan_cuts(inputs, opts, emit)
    if not segments:
        raise RuntimeError("لم يتبقَّ أي جزء من الفيديو بعد القص. جرّب إيقاف حذف الصمت.")
    total_in = sum(probe(p)["duration"] for p in inputs)
    total_out = sum(s["end"] - s["start"] for s in segments)
    emit(f"الخطة: {len(segments)} مقطع، {total_in:.1f}ث ← {total_out:.1f}ث", 0.3)

    words = timeline_words(segments)
    hook = ""
    if opts["text_hook"]:
        hook = opts["hook_text"].strip()
        if not hook and words:
            hook = " ".join(w["text"] for w in words[:6])
    chunks = caption_chunks(words) if opts["captions"] else []
    if chunks or hook:
        write_subtitles(job_dir, chunks, hook, total_out)

    if opts["music"] and not music:
        emit("⚠️ لم يتم رفع ملف موسيقى، سيتم التخطي")
    write_resolve_exports(job_dir, segments)
    render(job_dir, segments, opts, music if opts["music"] else None, emit)

    result = {
        "final": "final.mp4",
        "edl": "timeline.edl",
        "srt": "captions.srt" if chunks else None,
        "resolve_script": "resolve_import.py",
        "segments": len(segments),
        "input_seconds": round(total_in, 2),
        "output_seconds": round(total_out, 2),
        "options": opts,
    }
    with open(os.path.join(job_dir, "result.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    emit("✅ تم! الفيديو جاهز", 1.0)
    return result


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Raw-to-Reel CLI")
    ap.add_argument("inputs", nargs="+")
    ap.add_argument("--out", default="workspace/jobs/cli")
    ap.add_argument("--music")
    ap.add_argument("--options", default="{}", help="JSON options override")
    a = ap.parse_args()
    print(json.dumps(process(a.out, [os.path.abspath(p) for p in a.inputs],
                             json.loads(a.options), a.music and os.path.abspath(a.music)),
                     ensure_ascii=False, indent=2))
