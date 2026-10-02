"""Build a self-contained Raw to Reel folder (+ zip) for Windows PCs with no internet.

Run on a 64-bit Windows PC where Raw to Reel already works (double-click make-portable.bat).
The result needs nothing installed on the other PC: it carries its own Python, packages,
FFmpeg, the speech-to-text model and the face model.

    python tools/make_portable.py [--model small] [--no-zip]
"""

import argparse
import glob
import os
import platform
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "RawToReel-Portable")
PACKAGES = ["faster-whisper", "opencv-python-headless"]
VC_DLLS = ["msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll", "vcruntime140.dll", "vcruntime140_1.dll", "concrt140.dll"]

LAUNCHER = r'''@echo off
cd /d "%~dp0"
set "PATH=%~dp0ffmpeg;%~dp0python;%PATH%"
set PYTHONUTF8=1
set HF_HUB_OFFLINE=1
title Raw to Reel
echo Starting Raw to Reel at http://127.0.0.1:4680 ...
echo Keep this window open while you use it.
"%~dp0python\python.exe" raw-to-reel\server.py
pause
'''

README = """Raw to Reel - نسخة محمولة

١. فك الضغط عن الملف في أي مكان (مثلاً سطح المكتب).
٢. دبل كليك على "Start Raw to Reel.bat".
٣. تنفتح الصفحة في المتصفح على http://127.0.0.1:4680
   إذا ما انفتحت، افتح كروم واكتب العنوان بنفسك.

ما تحتاج إنترنت ولا تثبيت أي شي. خلّ النافذة السوداء مفتوحة طول ما أنت تستخدم الأداة.
(ميزة B-roll و الصور من Pexels بس هي اللي تحتاج إنترنت.)

🎨 التصميم الكحلي والذهبي: ارفع الفيديو واضغط زر "تصميم كحلي وذهبي". يشتغل كله بدون نت.
المهارات (raw-to-reel و navy-gold-reel) موجودة في مجلد .claude\skills. إذا ثبّت Claude Code وفتحته
في هالمجلد تقدر تكتب /navy-gold-reel عشان يخطط الرسوم من كلامك.
"""


def step(msg):
    print(f"\n==> {msg}", flush=True)


def download(url, path):
    with urllib.request.urlopen(url, timeout=120) as resp, open(path, "wb") as f:
        shutil.copyfileobj(resp, f)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="small", help="Whisper model to bundle (base / small / medium)")
    ap.add_argument("--no-zip", action="store_true")
    args = ap.parse_args()

    if os.name != "nt" or platform.machine().lower() not in ("amd64", "x86_64"):
        sys.exit("Run this on 64-bit Windows (the portable copy is for Windows PCs).")
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        sys.exit("FFmpeg not found. Run start-windows.bat once first.")

    shutil.rmtree(OUT, ignore_errors=True)
    py_dir = os.path.join(OUT, "python")
    site = os.path.join(py_dir, "Lib", "site-packages")
    os.makedirs(site)

    # same minor version as this Python so pip's wheels (cp3XX) match the embedded interpreter;
    # late security releases have no embeddable build, so fall back to the newest patch that has one
    major, minor, micro = sys.version_info[:3]
    zpath = os.path.join(OUT, "python-embed.zip")
    for patch in range(micro, -1, -1):
        ver = f"{major}.{minor}.{patch}"
        try:
            step(f"Downloading embeddable Python {ver}")
            download(f"https://www.python.org/ftp/python/{ver}/python-{ver}-embed-amd64.zip", zpath)
            break
        except urllib.error.HTTPError:
            print("   not available, trying an older patch release")
    else:
        sys.exit(f"No embeddable Python {major}.{minor} found on python.org")
    with zipfile.ZipFile(zpath) as z:
        z.extractall(py_dir)
    os.remove(zpath)
    pth = glob.glob(os.path.join(py_dir, "python*._pth"))[0]
    stdlib = os.path.basename(pth).replace("._pth", ".zip")
    with open(pth, "w") as f:  # a ._pth file replaces sys.path entirely, so list everything
        f.write(f"{stdlib}\n.\nLib\\site-packages\n..\\raw-to-reel\nimport site\n")

    step("Installing packages: " + ", ".join(PACKAGES))
    subprocess.check_call([sys.executable, "-m", "pip", "install", "--no-warn-script-location",
                           "--target", site, *PACKAGES])

    step("Copying Visual C++ runtime DLLs")
    system32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
    for dll in VC_DLLS:
        src = os.path.join(system32, dll)
        if os.path.exists(src) and not os.path.exists(os.path.join(py_dir, dll)):
            shutil.copy2(src, py_dir)

    step("Copying FFmpeg")
    os.makedirs(os.path.join(OUT, "ffmpeg"))
    for exe in (ffmpeg, ffprobe):
        shutil.copy2(os.path.realpath(exe), os.path.join(OUT, "ffmpeg", os.path.basename(exe).lower()))

    step("Copying Raw to Reel")
    shutil.copytree(os.path.join(ROOT, "raw-to-reel"), os.path.join(OUT, "raw-to-reel"),
                    ignore=shutil.ignore_patterns("workspace", "__pycache__", "whisper"))
    step("Copying the Claude Code skills")
    shutil.copytree(os.path.join(ROOT, ".claude", "skills"), os.path.join(OUT, ".claude", "skills"),
                    ignore=shutil.ignore_patterns("__pycache__"))

    python = os.path.join(py_dir, "python.exe")
    whisper_dir = os.path.join(OUT, "raw-to-reel", "models", "whisper")
    step(f"Downloading the speech-to-text model ({args.model})")
    subprocess.check_call([python, "-c",
                           "import sys; from faster_whisper import WhisperModel; "
                           "WhisperModel(sys.argv[1], device='cpu', compute_type='int8', download_root=sys.argv[2])",
                           args.model, whisper_dir])

    step("Checking the portable Python")
    subprocess.check_call([python, "-c", "import cv2, numpy, faster_whisper, editor, server, navygold, designs; print('   all imports OK')"],
                          cwd=os.path.join(OUT, "raw-to-reel"))

    with open(os.path.join(OUT, "Start Raw to Reel.bat"), "w", encoding="ascii", newline="\r\n") as f:
        f.write(LAUNCHER)
    with open(os.path.join(OUT, "اقرأني.txt"), "w", encoding="utf-8-sig") as f:
        f.write(README)

    if not args.no_zip:
        step("Zipping (this takes a few minutes)")
        shutil.make_archive(OUT, "zip", OUT)
        size = os.path.getsize(OUT + ".zip") / 1e6
        print(f"\nDone: {OUT}.zip ({size:.0f} MB)")
        print("Copy the zip to the other PC, unzip it, and double-click 'Start Raw to Reel.bat'.")
    else:
        print(f"\nDone: {OUT}")


if __name__ == "__main__":
    main()
