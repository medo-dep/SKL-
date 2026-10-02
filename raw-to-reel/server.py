"""Local web UI for Raw-to-Reel: http://127.0.0.1:4680"""

import json
import os
import re
import threading
import time
import traceback
import uuid
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

import editor

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(HERE, "workspace")
UPLOADS = os.path.join(WORK, "uploads")
JOBS_DIR = os.path.join(WORK, "jobs")
PORT = int(os.environ.get("PORT", "4680"))
JOBS = {}
UPLOAD_KINDS = ("video", "music", "logo", "broll")


def safe_name(name):
    name = os.path.basename(unquote(name))
    return re.sub(r"[^\w.\- ]", "_", name) or "file"


def run_job(job_id, inputs, options, assets):
    job = JOBS[job_id]

    def log(msg, progress=None):
        job["log"].append(msg)
        if progress is not None:
            job["progress"] = progress

    try:
        job["result"] = editor.process(job["dir"], inputs, options, assets, log)
        job["status"] = "done"
    except Exception as exc:  # report any pipeline failure to the UI
        traceback.print_exc()
        job["log"].append(f"❌ {exc}")
        job["status"] = "error"


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

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            self.path = "/index.html"
        elif url.path.startswith("/api/status/"):
            job = JOBS.get(url.path.rsplit("/", 1)[-1])
            if not job:
                return self.send_json({"error": "not found"}, 404)
            return self.send_json({k: job[k] for k in ("status", "progress", "log", "result", "id")})
        elif not (url.path in ("/index.html", "/app.js") or url.path.startswith("/workspace/jobs/")):
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
        name = f"{int(time.time())}-{safe_name(qs.get('name', ['file'])[0])}"
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
        try:
            info = editor.probe(path)
        except Exception as exc:  # not a readable media file, or ffprobe missing
            traceback.print_exc()
            os.remove(path)
            return self.send_json({"error": f"تعذّر قراءة الملف: {str(exc)[-300:]}"}, 400)
        self.send_json({"id": name, "kind": kind, "duration": info["duration"]})

    def do_POST(self):
        if urlparse(self.path).path != "/api/edit":
            return self.send_error(404)
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        inputs = [os.path.join(UPLOADS, "video", safe_name(v)) for v in body.get("videos", [])]
        inputs = [p for p in inputs if os.path.exists(p)]
        if not inputs:
            return self.send_json({"error": "اختر فيديو واحد على الأقل"}, 400)

        def uploaded(kind, name):
            path = os.path.join(UPLOADS, kind, safe_name(name)) if name else None
            return path if path and os.path.exists(path) else None

        assets = {
            "music": uploaded("music", body.get("music")),
            "logo": uploaded("logo", body.get("logo")),
            "broll": [p for p in (uploaded("broll", b) for b in body.get("broll", [])) if p],
        }
        options = body.get("options", {})

        job_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
        job_dir = os.path.join(JOBS_DIR, job_id)
        os.makedirs(job_dir, exist_ok=True)
        with open(os.path.join(job_dir, "job.json"), "w", encoding="utf-8") as f:
            json.dump({"inputs": inputs, "assets": assets,
                       "options": {k: v for k, v in options.items() if k != "pexels_key"}},
                      f, ensure_ascii=False, indent=2)
        JOBS[job_id] = {"id": job_id, "dir": job_dir, "status": "running", "progress": 0.0,
                        "log": [], "result": None}
        threading.Thread(target=run_job, args=(job_id, inputs, options, assets),
                         daemon=True).start()
        self.send_json({"id": job_id})


class Server(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        # the browser closing a video mid-stream is normal; don't print a scary traceback for it
        import sys
        if not isinstance(sys.exc_info()[1], (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)):
            super().handle_error(request, client_address)


def main():
    os.makedirs(UPLOADS, exist_ok=True)
    os.makedirs(JOBS_DIR, exist_ok=True)
    server = Server(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}"
    print(f"Raw to Reel is running at {url}")
    if os.environ.get("NO_BROWSER") != "1":
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    server.serve_forever()


if __name__ == "__main__":
    main()
