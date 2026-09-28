"""The demo video's music, composed here, not downloaded: a calm, business-style instrumental in C major, 96 bpm, over
I–V–vi–IV (C, G, Am, F). Soft piano arpeggios, a warm pad, a round bass, light drums. Three pieces:

    bed(secs)    under the opening slides (fades in, loops the progression, fades out)
    sting(secs)  a section transition: a rising swell into a bright chord that rings out
    outro(secs)  under the closing summary: the progression, then a held final C chord that fades out

make_video.py renders each at the length it needs; music.py alone writes samples to recordings/music/."""
import wave
from pathlib import Path

import numpy as np

SR = 48000
BPM = 96
BEAT = 60 / BPM
BAR = 4 * BEAT
RNG = np.random.default_rng(7)                                      # the same music every build

# (bass, pad, arpeggio) as MIDI notes, one chord per bar
CHORDS = {
    "C": (36, [60, 64, 67], [72, 76, 79, 84]),
    "G": (43, [59, 62, 67], [71, 74, 79, 83]),
    "Am": (45, [57, 60, 64], [69, 72, 76, 81]),
    "F": (41, [57, 60, 65], [69, 72, 77, 81]),
}
PROG = ["C", "G", "Am", "F"]
ARP = [0, 1, 2, 3, 2, 1, 2, 1]                                      # eighth notes over the chord's four arpeggio tones


def hz(m):
    return 440.0 * 2 ** ((m - 69) / 12)


def t_(secs):
    return np.arange(int(secs * SR)) / SR


def lowpass(x, cutoff):
    a = np.exp(-2 * np.pi * cutoff / SR)
    y = np.empty_like(x); acc = 0.0
    for i in range(len(x)):                                         # one-pole: fine for the lengths used here
        acc = (1 - a) * x[i] + a * acc; y[i] = acc
    return y


def piano(m, secs=1.4, vel=0.22):
    t = t_(secs); f = hz(m)
    tone = sum(a * np.sin(2 * np.pi * f * k * t) for k, a in ((1, 1.0), (2, 0.42), (3, 0.18), (4, 0.08), (5, 0.04)))
    env = np.minimum(t / 0.006, 1) * np.exp(-t / 0.55)
    return vel * tone * env


def pad(notes, secs, vel=0.075):
    t = t_(secs)
    x = sum(np.sin(2 * np.pi * hz(m) * d * t) + 0.3 * np.sin(4 * np.pi * hz(m) * d * t)
            for m in notes for d in (0.9982, 1.0, 1.0018))
    env = np.minimum(t / 0.7, 1) * np.minimum((secs - t) / 0.5, 1).clip(0)
    return vel * x * env / 3


def bass(m, secs, vel=0.2):
    t = t_(secs); f = hz(m)
    x = np.sin(2 * np.pi * f * t) + 0.25 * np.sin(4 * np.pi * f * t)
    env = np.minimum(t / 0.01, 1) * np.exp(-t / 1.2) * np.minimum((secs - t) / 0.08, 1).clip(0)
    return vel * x * env


def kick(vel=0.32):
    t = t_(0.35)
    f = 48 + 70 * np.exp(-t / 0.03)
    return vel * np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t / 0.12)


def hat(vel=0.028):
    t = t_(0.06); n = RNG.standard_normal(len(t))
    n = np.diff(n, prepend=0)                                       # a crude high-pass: bright
    return vel * n * np.exp(-t / 0.018)


def snare(vel=0.05):
    t = t_(0.18); n = lowpass(RNG.standard_normal(len(t)), 5000)
    return vel * (n + 0.4 * np.sin(2 * np.pi * 190 * t)) * np.exp(-t / 0.06)


def add(buf, x, at):
    i = int(at * SR)
    if i >= len(buf):
        return
    n = min(len(x), len(buf) - i)
    buf[i:i + n] += x[:n]


def arrangement(secs, drums=True, bars=None):
    """The progression for `secs` seconds (or `bars` bars)."""
    n = int(np.ceil(secs / BAR)) + 1 if bars is None else bars
    buf = np.zeros(int((n * BAR + 2) * SR))
    for b in range(n):
        c = CHORDS[PROG[b % 4]]; t0 = b * BAR
        add(buf, pad(c[1], BAR + 0.6), t0)
        add(buf, bass(c[0], BAR * 0.98), t0)
        for i, k in enumerate(ARP):
            add(buf, piano(c[2][k], vel=0.2 if i % 2 == 0 else 0.15), t0 + i * BEAT / 2)
        if drums:
            for beat in range(4):
                if beat in (0, 2):
                    add(buf, kick(), t0 + beat * BEAT)
                if beat in (1, 3):
                    add(buf, snare(), t0 + beat * BEAT)
                add(buf, hat(), t0 + beat * BEAT + BEAT / 2)
    return buf


def fade(x, fin, fout):
    t = np.arange(len(x)) / SR; secs = len(x) / SR
    return x * np.minimum(t / max(fin, 1e-3), 1) * np.minimum((secs - t) / max(fout, 1e-3), 1).clip(0)


def finish(x, secs):
    x = x[:int(secs * SR)]
    if len(x) < int(secs * SR):
        x = np.pad(x, (0, int(secs * SR) - len(x)))
    peak = np.abs(x).max() or 1
    return (x / peak * 0.89).astype(np.float32)                      # -1 dBFS


def bed(secs):
    return finish(fade(arrangement(secs), 1.2, 2.0), secs)


def sting(secs):
    """A swell (noise rising through a filter) into a strummed C major chord that rings out."""
    buf = np.zeros(int((secs + 2) * SR))
    rise = 0.9
    t = t_(rise); n = RNG.standard_normal(len(t))
    sw = lowpass(n, 2500) * (t / rise) ** 2 * 0.12
    add(buf, sw, 0)
    c = CHORDS["C"]
    add(buf, pad(c[1] + [72], secs + 0.5, vel=0.09), rise - 0.3)
    add(buf, bass(c[0], secs, vel=0.22), rise)
    add(buf, kick(0.3), rise)
    for i, m in enumerate([60, 64, 67, 72, 76, 79]):
        add(buf, piano(m, secs=secs, vel=0.16), rise + i * 0.035)
    return finish(fade(buf, 0.05, 0.9), secs)


def outro(secs):
    """The progression until the last 4 s, then a held C chord that fades out."""
    hold = min(4.5, secs * 0.45)
    bars = max(1, int((secs - hold) / BAR))
    body = arrangement(0, bars=bars)[:int(bars * BAR * SR)]
    end = np.zeros(int((hold + 2) * SR))
    c = CHORDS["C"]
    add(end, pad(c[1] + [72], hold + 1, vel=0.09), 0)
    add(end, bass(c[0], hold, vel=0.22), 0)
    add(end, kick(0.3), 0)
    for i, m in enumerate([60, 64, 67, 72, 76, 79, 84]):
        add(end, piano(m, secs=hold + 1, vel=0.15), i * 0.04)
    x = np.concatenate([body, end])
    return finish(fade(x[:int(secs * SR)], 0.8, 2.5), secs)


def write(path, x):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
        w.writeframes((x * 32767).astype(np.int16).tobytes())


if __name__ == "__main__":
    out = Path(__file__).resolve().parent / "recordings" / "music"
    out.mkdir(parents=True, exist_ok=True)
    for name, x in (("bed", bed(40)), ("sting", sting(4)), ("outro", outro(12))):
        write(out / f"{name}.wav", x); print(out / f"{name}.wav", f"{len(x) / SR:.1f} s")
