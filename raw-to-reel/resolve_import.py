"""Build the Raw-to-Reel cut as a timeline inside DaVinci Resolve.

Run with Resolve open (Studio, or free with external scripting enabled in
Preferences > System > General):
    python3 resolve_import.py            # from inside a job folder
Reads cuts.json + captions.srt next to this file.
"""

import json
import os
import sys
import time

RESOLVE_PATHS = {
    "darwin": "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting/Modules",
    "win32": os.path.join(os.environ.get("PROGRAMDATA", r"C:\ProgramData"),
                          r"Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting\Modules"),
    "linux": "/opt/resolve/Developer/Scripting/Modules",
}


def get_resolve():
    sys.path.append(RESOLVE_PATHS.get(sys.platform, RESOLVE_PATHS["linux"]))
    try:
        import DaVinciResolveScript as dvr
    except ImportError:
        sys.exit("DaVinci Resolve scripting module not found. Is Resolve installed?")
    resolve = dvr.scriptapp("Resolve")
    if not resolve:
        sys.exit("Could not connect to Resolve. Open Resolve first and enable external scripting.")
    return resolve


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "cuts.json"), encoding="utf-8") as f:
        cuts = json.load(f)

    resolve = get_resolve()
    pm = resolve.GetProjectManager()
    name = "Raw to Reel " + time.strftime("%Y-%m-%d %H-%M-%S")
    project = pm.CreateProject(name) or pm.GetCurrentProject()
    project.SetSetting("timelineResolutionWidth", "1080")
    project.SetSetting("timelineResolutionHeight", "1920")
    project.SetSetting("timelineFrameRate", str(round(cuts[0]["fps"])) if cuts else "30")

    pool = project.GetMediaPool()
    files = sorted({c["file"] for c in cuts})
    items = {os.path.abspath(i.GetClipProperty("File Path")): i for i in pool.ImportMedia(files)}
    final = os.path.join(here, "final.mp4")
    if os.path.exists(final):
        pool.ImportMedia([final])

    timeline = pool.CreateEmptyTimeline("Reel - cut")
    clips = [{
        "mediaPoolItem": items[c["file"]],
        "startFrame": int(round(c["start"] * c["fps"])),
        "endFrame": int(round(c["end"] * c["fps"])) - 1,
    } for c in cuts if c["file"] in items]
    pool.AppendToTimeline(clips)

    srt = os.path.join(here, "captions.srt")
    if os.path.exists(srt):
        pool.ImportMedia([srt])

    resolve.OpenPage("edit")
    print(f"Created project '{name}' with {len(clips)} clips on timeline '{timeline.GetName()}'.")


if __name__ == "__main__":
    main()
