"""Local web UI for Raw-to-Reel: http://127.0.0.1:4680"""

import json
import os
import queue
import re
import threading
import time
import traceback
import uuid
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

import editor
import navygold

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(HERE, "workspace")
UPLOADS = os.path.join(WORK, "uploads")
JOBS_DIR = os.path.join(WORK, "jobs")
PRESETS_FILE = os.path.join(WORK, "presets.json")
PORT = int(os.environ.get("PORT", "4680"))
JOBS = {}
UPLOAD_KINDS = ("video", "music", "logo", "broll", "font")
TASKS = queue.Queue()  # one worker: videos are edited one after another, never in parallel


def safe_name(name):
    name = os.path.basename(unquote(name))
    return re.sub(r"[^\w.\- ]", "_", name) or "file"


def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def worker():
    while True:
        job_id, step, payload = TASKS.get()
        job = JOBS[job_id]
        job["status"] = "running"

        def log(msg, progress=None):
            job["log"].append(msg)
            if progress is not None:
                job["progress"] = progress

        try:
            if step == "navygold":
                plan = editor.analyze(job["dir"], job["inputs"], job["options"], log)
                log("قص فيديو نظيف للتصميم...", 0.6)
                navygold.prepare(job["dir"], plan, job["options"], job["assets"], log=log)
                navygold.auto_scenes(job["dir"])
                step = "navyrender"
            if step == "navyrender":
                if payload.get("scenes"):
                    write_json(os.path.join(job["dir"], "scenes.json"), payload["scenes"])
                log("رسم التصميم الكحلي والذهبي...", 0.8)
                navygold.render(job["dir"], log=log)
                job["progress"] = 1.0
                job["version"] = job.get("version", 0) + 1
                job["result"] = navygold_result(job)
                job["status"] = "done"
                continue
            if step in ("analyze", "auto", "preview"):
                job["plan"] = editor.analyze(job["dir"], job["inputs"], job["options"], log)
                job["review"] = editor.review_summary(job["plan"])
            if step == "auto":
                job["result"] = editor.render_plan(job["dir"], job["plan"], job["options"], job["assets"], log=log)
            elif step == "preview" or (step == "render" and payload.get("preview")):
                job["preview"] = editor.render_plan(job["dir"], job["plan"], job["options"], job["assets"],
                                                    payload, preview=True, log=log)
            elif step == "render":
                job["result"] = editor.render_plan(job["dir"], job["plan"], job["options"], job["assets"],
                                                   payload, log=log)
            job["status"] = "done" if job["result"] else "review"
        except Exception as exc:  # report any pipeline failure to the UI
            traceback.print_exc()
            job["log"].append(f"❌ {exc}")
            job["status"] = "done" if job.get("result") else "review" if job.get("plan") else "error"
        finally:
            TASKS.task_done()


def navygold_result(job):
    meta = read_json(os.path.join(job["dir"], "navygold.json"), {})
    try:
        with open(os.path.join(job["dir"], "transcript.txt"), encoding="utf-8") as f:
            transcript = f.read()
    except OSError:
        transcript = ""
    return {"navygold": True, "final": "navygold.mp4", "seconds": round(meta.get("duration", 0), 1),
            "scenes": read_json(os.path.join(job["dir"], "scenes.json"), {"scenes": []}),
            "transcript": transcript, "version": job.get("version", 0)}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=HERE, **kwargs)

    def log_message(self, fmt, *args):
        pass

    def send_json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_body(self):
        return json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            self.path = "/index.html"
        elif url.path.startswith("/api/status/"):
            job = JOBS.get(url.path.rsplit("/", 1)[-1])
            if not job:
                return self.send_json({"error": "not found"}, 404)
            out = {k: job.get(k) for k in ("id", "name", "status", "progress", "log", "result", "preview")}
            out["review"] = job.get("review") if job["status"] == "review" else None
            out["ahead"] = sum(1 for j, *_ in list(TASKS.queue) if JOBS[j]["created"] < job["created"])
            return self.send_json(out)
        elif url.path == "/api/dictionary":
            return self.send_json(editor.load_dictionary())
        elif url.path == "/api/presets":
            return self.send_json(read_json(PRESETS_FILE, {}))
        elif url.path == "/api/fonts":
            return self.send_json({k: {"file": f, "family": fam, "label": label} for k, (f, fam, label, _, _)
                                   in editor.FONTS.items()})
        elif not (url.path in ("/index.html", "/app.js")
                  or url.path.startswith(("/workspace/jobs/", "/workspace/uploads/video/", "/workspace/uploads/font/",
                                          "/fonts/"))):
            return self.send_error(404)
        return super().do_GET()

    def do_PUT(self):
        url = urlparse(self.path)
        if url.path != "/api/upload":
            return self.send_error(404)
        qs = parse_qs(url.query)
        kind = qs.get("kind", ["video"])[0]
        if kind not in UPLOAD_KINDS:
            return self.send_json({"error": "نوع ملف غير معروف"}, 400)
        folder = os.path.join(UPLOADS, kind)
        os.makedirs(folder, exist_ok=True)
        name = f"{int(time.time() * 1000)}-{safe_name(qs.get('name', ['file'])[0])}"
        path = os.path.join(folder, name)
        remaining = int(self.headers.get("Content-Length", 0))
        with open(path, "wb") as f:
            while remaining > 0:
                chunk = self.rfile.read(min(1 << 20, remaining))
                if not chunk:
                    break
                f.write(chunk)
                remaining -= len(chunk)
        if remaining > 0:
            os.remove(path)
            return self.send_json({"error": "انقطع الرفع قبل ما يكتمل، جرّب مرة ثانية"}, 400)
        if kind == "font":
            try:
                family, weight = editor.font_info(path)
            except Exception:  # not a font file
                family = None
            if not family:
                os.remove(path)
                return self.send_json({"error": "الملف مو خط (لازم .ttf أو .otf)"}, 400)
            return self.send_json({"id": name, "kind": kind, "family": family})
        try:
            info = editor.probe(path)
        except Exception as exc:  # not a readable media file, or ffprobe missing
            traceback.print_exc()
            os.remove(path)
            return self.send_json({"error": f"تعذّر قراءة الملف: {str(exc)[-300:]}"}, 400)
        self.send_json({"id": name, "kind": kind, "duration": info["duration"]})

    def do_DELETE(self):
        url = urlparse(self.path)
        if url.path != "/api/presets":
            return self.send_error(404)
        presets = read_json(PRESETS_FILE, {})
        presets.pop(parse_qs(url.query).get("name", [""])[0], None)
        write_json(PRESETS_FILE, presets)
        self.send_json(presets)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/edit":
            return self.start_jobs(self.read_body())
        if path.startswith("/api/render/"):
            job = JOBS.get(path.rsplit("/", 1)[-1])
            if not job or job["status"] != "review":
                return self.send_json({"error": "المونتاج مو جاهز للمراجعة"}, 400)
            edits = self.read_body()
            if edits.get("save_dictionary") and edits.get("corrections"):
                words = {w["id"]: w["text"] for s in job["plan"]["segments"] for w in s["words"]}
                editor.learn_corrections((words[int(i)], t) for i, t in edits["corrections"].items()
                                         if int(i) in words)
            job["status"] = "queued"
            TASKS.put((job["id"], "render", edits))
            return self.send_json({"id": job["id"]})
        if path.startswith("/api/navygold/"):
            job = JOBS.get(path.rsplit("/", 1)[-1])
            if not job or not os.path.exists(os.path.join(job["dir"], "navygold.json")):
                return self.send_json({"error": "سوّ التصميم الكحلي أول"}, 400)
            if job["status"] in ("queued", "running"):
                return self.send_json({"error": "انتظر، الرسم شغّال"}, 400)
            scenes = self.read_body().get("scenes")
            if not isinstance(scenes, dict) or not isinstance(scenes.get("scenes"), list):
                return self.send_json({"error": "الخطة لازم فيها \"scenes\": [...]"}, 400)
            job["status"] = "queued"
            TASKS.put((job["id"], "navyrender", {"scenes": scenes}))
            return self.send_json({"id": job["id"]})
        if path == "/api/dictionary":
            entries = {editor.norm_ar(k): v.strip() for k, v in self.read_body().items()
                       if editor.norm_ar(k) and str(v).strip()}
            editor.save_dictionary(entries)
            return self.send_json(entries)
        if path == "/api/presets":
            body = self.read_body()
            name = (body.get("name") or "").strip()[:60]
            if not name:
                return self.send_json({"error": "اكتب اسم للقالب"}, 400)
            presets = read_json(PRESETS_FILE, {})
            presets[name] = {k: v for k, v in (body.get("options") or {}).items() if k != "pexels_key"}
            write_json(PRESETS_FILE, presets)
            return self.send_json(presets)
        return self.send_error(404)

    def start_jobs(self, body):
        videos = [os.path.join(UPLOADS, "video", safe_name(v)) for v in body.get("videos", [])]
        videos = [p for p in videos if os.path.exists(p)]
        if not videos:
            return self.send_json({"error": "اختر فيديو واحد على الأقل"}, 400)

        def uploaded(kind, name):
            path = os.path.join(UPLOADS, kind, safe_name(name)) if name else None
            return path if path and os.path.exists(path) else None

        assets = {
            "music": uploaded("music", body.get("music")),
            "logo": uploaded("logo", body.get("logo")),
            "broll": [p for p in (uploaded("broll", b) for b in body.get("broll", [])) if p],
            "font": uploaded("font", body.get("font")),
        }
        options = body.get("options", {})
        mode = body.get("mode", "auto")  # auto | review | preview
        batch = bool(body.get("batch")) and len(videos) > 1
        ids = []
        for group in ([[v] for v in videos] if batch else [videos]):
            job_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
            job_dir = os.path.join(JOBS_DIR, job_id)
            write_json(os.path.join(job_dir, "job.json"),
                       {"inputs": group, "assets": assets,
                        "options": {k: v for k, v in options.items() if k != "pexels_key"}})
            JOBS[job_id] = {"id": job_id, "dir": job_dir, "status": "queued", "progress": 0.0, "log": [],
                            "result": None, "preview": None, "review": None, "plan": None, "created": time.time(),
                            "inputs": group, "assets": assets, "options": options,
                            "name": "، ".join(os.path.basename(v).split("-", 1)[-1] for v in group)}
            step = "navygold" if mode == "navygold" else "auto" if batch else {"review": "analyze"}.get(mode, mode)
            TASKS.put((job_id, step, {}))
            ids.append(job_id)
        self.send_json({"id": ids[0], "ids": ids})


class Server(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        # the browser closing a video mid-stream is normal; don't print a scary traceback for it
        import sys
        if not isinstance(sys.exc_info()[1], (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)):
            super().handle_error(request, client_address)


def main():
    os.makedirs(UPLOADS, exist_ok=True)
    os.makedirs(JOBS_DIR, exist_ok=True)
    threading.Thread(target=worker, daemon=True).start()
    server = Server(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}"
    print(f"Raw to Reel is running at {url}")
    if os.environ.get("NO_BROWSER") != "1":
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    server.serve_forever()


if __name__ == "__main__":
    main()
