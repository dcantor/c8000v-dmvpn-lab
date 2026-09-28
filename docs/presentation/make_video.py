"""The demo MP4: slides from the deck's PDF between the recorded walkthroughs (demo.py), 1920x1080, H.264.

Needs ffmpeg with libx264: FFMPEG=/path/to/ffmpeg, or ffmpeg on the PATH, or the imageio-ffmpeg package's build.

    python docs/presentation/make_video.py
"""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

D = Path(__file__).resolve().parent
REC = D / "recordings"
BUILD = REC / "_build"
PDF = D / "c8000v-dmvpn-portal.pdf"
OUT = D / "c8000v-dmvpn-portal-demo.mp4"
FPS = 30


def ffmpeg_exe():
    if os.environ.get("FFMPEG"):
        return os.environ["FFMPEG"]
    if shutil.which("ffmpeg"):
        return "ffmpeg"
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        sys.exit("no ffmpeg: set FFMPEG, install ffmpeg, or pip install imageio-ffmpeg")


FF = ffmpeg_exe()
X264 = ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p", "-r", str(FPS)]
SILENCE = ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
AUDIO = ["-c:a", "aac", "-b:a", "96k", "-shortest"]

# (slide title as it appears in the PDF, seconds) or ("rec", segment)
PLAN = [
    ("C8000v DMVPN Portal", 6), ("Running a DMVPN service by hand does not scale", 11), ("What the portal does", 10),
    ("Capabilities at a glance", 11),
    ("A tour of the portal", 3.5), ("rec", "portal"),
    ("Customers, access and the API", 3.5), ("rec", "customer"),
    ("Source of truth: Nautobot", 3.5), ("Where the customers and the VPN live in Nautobot", 10), ("rec", "nautobot"),
    ("How the lab is monitored", 10), ("rec", "grafana"),
    ("Measured, not assumed", 10), ("In short", 9),
]


def run(args):
    r = subprocess.run([FF, "-hide_banner", "-loglevel", "error", "-y", *args], capture_output=True, text=True)
    if r.returncode:
        sys.exit(r.stderr)


def duration(path):
    out = subprocess.run([FF, "-hide_banner", "-i", str(path)], capture_output=True, text=True).stderr
    h, m, s = re.search(r"Duration: (\d+):(\d+):([\d.]+)", out).groups()
    return int(h) * 3600 + int(m) * 60 + float(s)


def page_titles():
    """{title: page number}: a slide's title is its first line or two of text."""
    out, n = {}, 1
    for page in subprocess.run(["pdftotext", "-layout", str(PDF), "-"], capture_output=True, text=True).stdout.split("\f"):
        lines = [l.strip() for l in page.splitlines() if l.strip()]
        for l in lines[:4]:
            out.setdefault(re.sub(r"\s{2,}.*", "", l), n)
        n += 1
    return out


def slide_clip(page, secs, out):
    png = BUILD / f"p{page:02d}"
    subprocess.run(["pdftoppm", "-png", "-r", "144", "-scale-to-x", "1920", "-scale-to-y", "1080", "-f", str(page), "-l", str(page),
                    "-singlefile", str(PDF), str(png)], check=True)
    run(["-loop", "1", "-framerate", str(FPS), "-t", str(secs), "-i", f"{png}.png", *SILENCE,
         "-vf", f"scale=1920:1080,fade=in:st=0:d=0.4,fade=out:st={secs - 0.4}:d=0.4", *X264, *AUDIO, str(out)])


def rec_clip(name, out, marks):
    src = REC / marks[name]["file"]
    start = marks[name]["start"]
    secs = duration(src) - start - 0.7                          # the end: the caption cleared, the context closing
    run(["-ss", str(start), "-t", str(secs), "-i", str(src), *SILENCE,
         "-vf", f"fps={FPS},scale=1920:1080,fade=in:st=0:d=0.5,fade=out:st={secs - 0.5}:d=0.5", *X264, *AUDIO, str(out)])


def main():
    marks = json.loads((REC / "marks.json").read_text())
    shutil.rmtree(BUILD, ignore_errors=True); BUILD.mkdir(parents=True)
    titles = page_titles()
    clips = []
    for i, (what, arg) in enumerate(PLAN):
        out = BUILD / f"{i:02d}.mp4"
        if what == "rec":
            if arg not in marks:
                print(f"  no recording of {arg}: skipped"); continue
            rec_clip(arg, out, marks)
        else:
            if what not in titles:
                sys.exit(f"no slide titled {what!r} in {PDF.name}")
            slide_clip(titles[what], arg, out)
        clips.append(out)
        print(f"{out.name}: {what if what != 'rec' else arg} ({duration(out):.1f} s)")
    (BUILD / "list.txt").write_text("".join(f"file '{c.name}'\n" for c in clips))
    run(["-f", "concat", "-safe", "0", "-i", str(BUILD / "list.txt"), "-c", "copy", "-movflags", "+faststart", str(OUT)])
    shutil.rmtree(BUILD)
    print(f"{OUT}: {duration(OUT):.0f} s, {OUT.stat().st_size / 1e6:.1f} MB")


main()
