"""The demo MP4: slides from the deck's PDF between the recorded walkthroughs (demo.py), 1920x1080, H.264.

Needs ffmpeg with libx264: FFMPEG=/path/to/ffmpeg, or ffmpeg on the PATH, or the imageio-ffmpeg package's build.

    python docs/presentation/make_video.py              # captions only: c8000v-dmvpn-portal-demo.mp4
    python docs/presentation/make_video.py --narrated   # with the voice-over: c8000v-dmvpn-portal-demo-narrated.mp4

--narrated uses the recordings demo.py --narrated made (paced to the voice) and the lines narration.py synthesized.
Music (music.py, composed here) plays under the opening slides, on the section transitions and under the closing
summary; under narration it ducks. --no-music leaves it out.
"""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

D = Path(__file__).resolve().parent
NARRATED = "--narrated" in sys.argv
MUSIC = "--no-music" not in sys.argv
VOICE_DIR = D / "recordings" / "voice"
REC = D / "recordings" / ("narrated" if NARRATED else "")
BUILD = REC / "_build"
PDF = D / "c8000v-dmvpn-portal.pdf"
OUT = D / ("c8000v-dmvpn-portal-demo-narrated.mp4" if NARRATED else "c8000v-dmvpn-portal-demo.mp4")
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

# (slide title as it appears in the PDF, seconds, its voice-over line) or ("rec", segment, None)
PLAN = [
    ("C8000v DMVPN Portal", 6, "s_title"), ("Running a DMVPN service by hand does not scale", 11, "s_problem"),
    ("What the portal does", 10, "s_what"), ("Capabilities at a glance", 11, "s_caps"),
    ("A tour of the portal", 3.5, "s_tour"), ("rec", "portal", None),
    ("Customers, access and the API", 3.5, "s_customers"), ("rec", "customer", None),
    ("Source of truth: Nautobot", 3.5, "s_nautobot"), ("Where the customers and the VPN live in Nautobot", 10, "s_nbmodel"),
    ("rec", "nautobot", None),
    ("How the lab is monitored", 10, "s_monitoring"), ("rec", "grafana", None),
    ("Measured, not assumed", 10, "s_results"), ("In short", 9, "s_summary"),
]
LEAD = 0.6                                                      # a slide's voice starts this long after it appears
INTRO = ["C8000v DMVPN Portal", "Running a DMVPN service by hand does not scale", "What the portal does", "Capabilities at a glance"]
TRANSITIONS = ["A tour of the portal", "Customers, access and the API", "Source of truth: Nautobot", "How the lab is monitored"]
SUMMARY = "In short"
GAIN = {"bed": 0.32, "sting": 0.45, "outro": 0.38}                # music level alone; under the voice it ducks further
if not NARRATED:                                                  # no voice to sit under: the music carries the slides
    GAIN = {k: round(v * 1.9, 2) for k, v in GAIN.items()}


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


def audio_args(lines, secs, music=None):
    """ffmpeg inputs and filter for a clip's sound: each (seconds, key) voice line delayed into place, and the music
    (wav, offset, gain) under it, ducked by the voice; silence when there is neither."""
    lines = lines if NARRATED else []
    if not lines and not music:
        return SILENCE, ["-map", "0:v", "-map", "1:a"]
    ins, parts = [], []
    for i, (t, key) in enumerate(lines):
        ins += ["-i", str(VOICE_DIR / f"{key}.wav")]
        ms = max(0, int(t * 1000))
        parts.append(f"[{i + 1}:a]aresample=48000,aformat=channel_layouts=stereo,adelay={ms}|{ms}[a{i}]")
    if lines:
        parts.append("".join(f"[a{i}]" for i in range(len(lines))) + f"amix=inputs={len(lines)}:normalize=0:dropout_transition=0,apad=whole_dur={secs:.2f}[vox]")
    out = "[vox]"
    if music:
        wav, offset, gain = music
        ins += ["-ss", f"{offset:.3f}", "-t", f"{secs:.3f}", "-i", str(wav)]
        parts.append(f"[{len(lines) + 1}:a]aresample=48000,aformat=channel_layouts=stereo,volume={gain}[mus]")
        if lines:
            parts.append("[vox]asplit=2[vx][sc];[mus][sc]sidechaincompress=threshold=0.012:ratio=10:attack=15:release=600[md];"
                         "[vx][md]amix=inputs=2:normalize=0:dropout_transition=0[mixed]")
            out = "[mixed]"
        else:
            out = "[mus]"
    graph = ";".join(parts) + f";{out}apad,atrim=0:{secs:.2f}[aout]"
    return ins, ["-filter_complex", graph, "-map", "0:v", "-map", "[aout]"]


def slide_secs(secs, key):
    return max(secs, LEAD + VOICE[key]["secs"] + 1.0) if NARRATED and key else secs


def render_music(kind, secs):
    """music.py's piece, `secs` long, as a wav in the build folder."""
    sys.path.insert(0, str(D))
    import music
    path = BUILD / f"music-{kind}-{len(list(BUILD.glob('music-*')))}.wav"
    music.write(path, getattr(music, kind)(secs))
    return path


def slide_clip(page, secs, out, key=None, music=None):
    png = BUILD / f"p{page:02d}"
    subprocess.run(["pdftoppm", "-png", "-r", "144", "-scale-to-x", "1920", "-scale-to-y", "1080", "-f", str(page), "-l", str(page),
                    "-singlefile", str(PDF), str(png)], check=True)
    secs = slide_secs(secs, key)
    ains, amap = audio_args([(LEAD, key)] if key else [], secs, music)
    run(["-loop", "1", "-framerate", str(FPS), "-t", str(secs), "-i", f"{png}.png", *ains,
         "-vf", f"scale=1920:1080,fade=in:st=0:d=0.4,fade=out:st={secs - 0.4}:d=0.4", *amap, *X264, *AUDIO, str(out)])


def rec_clip(name, out, marks):
    src = REC / marks[name]["file"]
    start = marks[name]["start"]
    secs = duration(src) - start - 0.7                          # the end: the caption cleared, the context closing
    ains, amap = audio_args([(t - start, k) for t, k in marks[name].get("events", [])], secs)
    run(["-ss", str(start), "-t", str(secs), "-i", str(src), *ains,
         "-vf", f"fps={FPS},scale=1920:1080,fade=in:st=0:d=0.5,fade=out:st={secs - 0.5}:d=0.5", *amap, *X264, *AUDIO, str(out)])


VOICE = json.loads((VOICE_DIR / "durations.json").read_text()) if NARRATED else {}


def main():
    marks = json.loads((REC / "marks.json").read_text())
    shutil.rmtree(BUILD, ignore_errors=True); BUILD.mkdir(parents=True)
    titles = page_titles()
    clips = []
    tracks = {}                                                  # slide title -> (wav, offset, gain)
    if MUSIC:
        intro = [slide_secs(secs, key) for what, secs, key in PLAN if what in INTRO]
        bed = render_music("bed", sum(intro))
        at = 0.0
        for (what, secs, key) in [x for x in PLAN if x[0] in INTRO]:
            tracks[what] = (bed, at, GAIN["bed"]); at += slide_secs(secs, key)
        for what, secs, key in PLAN:
            if what in TRANSITIONS:
                tracks[what] = (render_music("sting", min(slide_secs(secs, key), 5.0)), 0.0, GAIN["sting"])
            elif what == SUMMARY:
                tracks[what] = (render_music("outro", slide_secs(secs, key)), 0.0, GAIN["outro"])
    for i, (what, arg, key) in enumerate(PLAN):
        out = BUILD / f"{i:02d}.mp4"
        if what == "rec":
            if arg not in marks:
                print(f"  no recording of {arg}: skipped"); continue
            rec_clip(arg, out, marks)
        else:
            if what not in titles:
                sys.exit(f"no slide titled {what!r} in {PDF.name}")
            slide_clip(titles[what], arg, out, key, tracks.get(what))
        clips.append(out)
        print(f"{out.name}: {what if what != 'rec' else arg} ({duration(out):.1f} s)")
    (BUILD / "list.txt").write_text("".join(f"file '{c.name}'\n" for c in clips))
    run(["-f", "concat", "-safe", "0", "-i", str(BUILD / "list.txt"), "-c", "copy", "-movflags", "+faststart", str(OUT)])
    shutil.rmtree(BUILD)
    print(f"{OUT}: {duration(OUT):.0f} s, {OUT.stat().st_size / 1e6:.1f} MB")


main()
