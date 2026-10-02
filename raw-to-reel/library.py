"""Built-in, royalty-free background music and sound effects, synthesised from code (numpy).

Nothing is downloaded: tracks are generated on first use and cached as .m4a files.
"""

import os
import subprocess

import numpy as np

SR = 44100
TRACKS = {
    "calm": "هادئ",
    "upbeat": "حماسي",
    "lofi": "لوفاي",
    "inspiring": "ملهم",
}
SFX = ("whoosh", "pop", "ding", "swoosh", "boom", "click")

# I - V - vi - IV in C, voiced around middle C
PROGRESSION = [[48, 60, 64, 67], [43, 59, 62, 67], [45, 60, 64, 69], [41, 60, 65, 69]]


def hz(midi):
    return 440.0 * 2 ** ((midi - 69) / 12)


def lowpass(x, cutoff):
    spec = np.fft.rfft(x, axis=0)
    freqs = np.fft.rfftfreq(len(x), 1 / SR)
    spec *= (1 / (1 + (freqs / cutoff) ** 4))[:, None] if x.ndim == 2 else 1 / (1 + (freqs / cutoff) ** 4)
    return np.fft.irfft(spec, n=len(x), axis=0)


def highpass(x, cutoff):
    return x - lowpass(x, cutoff)


def envelope(n, attack, release):
    env = np.ones(n)
    a, r = min(n, int(attack * SR)), min(n, int(release * SR))
    env[:a] = np.linspace(0, 1, a)
    if r:
        env[-r:] *= np.linspace(1, 0, r)
    return env


def pad_note(midi, dur, bright=4):
    t = np.arange(int(dur * SR)) / SR
    f = hz(midi)
    out = np.zeros_like(t)
    for detune in (-0.004, 0.004):
        for k in range(1, bright + 1):
            out += np.sin(2 * np.pi * f * (1 + detune) * k * t + k) / k
    return out * envelope(len(t), 0.9, 1.2)


def pluck(midi, dur, decay=3.0, harmonics=4):
    t = np.arange(int(dur * SR)) / SR
    f = hz(midi)
    out = sum(np.sin(2 * np.pi * f * k * t) * np.exp(-t * decay * k) / k for k in range(1, harmonics + 1))
    return out * envelope(len(t), 0.004, 0.05)


def kick(dur=0.35):
    t = np.arange(int(dur * SR)) / SR
    freq = 45 + 110 * np.exp(-t * 30)
    return np.sin(2 * np.pi * np.cumsum(freq) / SR) * np.exp(-t * 9)


def noise_hit(dur, decay, lo=None, hi=None, seed=0):
    rng = np.random.default_rng(seed)
    t = np.arange(int(dur * SR)) / SR
    x = rng.standard_normal(len(t))
    if hi:
        x = highpass(x, hi)
    if lo:
        x = lowpass(x, lo)
    return x * np.exp(-t * decay)


def place(track, sound, at, gain=1.0):
    i = int(at * SR)
    if i >= len(track):
        return
    j = min(len(track), i + len(sound))
    track[i:j] += sound[: j - i] * gain


def normalize(x, peak=0.8):
    m = np.max(np.abs(x)) or 1.0
    return x / m * peak


def song(style, seconds=64):
    """Loopable instrumental: 4-chord progression with a style-specific arrangement."""
    bpm = {"calm": 70, "upbeat": 104, "lofi": 78, "inspiring": 92}[style]
    beat = 60 / bpm
    bar = beat * 4
    n = int(seconds * SR)
    music, drums = np.zeros(n), np.zeros(n)
    bars = int(seconds // bar)
    kick_s, snare_s, hat_s = kick(), noise_hit(0.25, 18, lo=6000, hi=900, seed=1), noise_hit(0.08, 60, hi=7000, seed=2)
    for b in range(bars):
        chord = PROGRESSION[b % 4]
        t0 = b * bar
        if style in ("calm", "lofi", "inspiring"):
            for note in chord[1:]:
                place(music, pad_note(note, bar + 0.6, 3 if style == "lofi" else 5), t0, 0.12)
        if style in ("upbeat", "inspiring", "lofi"):
            pattern = [1, 2, 3, 2, 1, 2, 3, 2] if style != "lofi" else [1, 3, 2, 3]
            step = bar / len(pattern)
            for i, idx in enumerate(pattern):
                place(music, pluck(chord[idx] + 12, step * 2, 4 if style == "upbeat" else 2.5), t0 + i * step,
                      0.22 if style == "upbeat" else 0.16)
        if style != "calm":
            place(music, pluck(chord[0] - 12, bar * 0.9, 1.2, 2), t0, 0.35)  # bass on the bar
            for i in range(4):
                swing = 0.03 if style == "lofi" and i % 2 else 0.0
                if i in (0, 2) or (style == "upbeat"):
                    place(drums, kick_s, t0 + i * beat, 0.9 if i in (0, 2) else 0.5)
                if i in (1, 3):
                    place(drums, snare_s, t0 + i * beat + swing, 0.35 if style == "lofi" else 0.5)
                for half in (0, 0.5):
                    place(drums, hat_s, t0 + (i + half) * beat + (swing if half else 0), 0.12)
    if style == "lofi":
        music = lowpass(music, 2200)
        drums = lowpass(drums, 5000) + noise_hit(seconds, 0, lo=3000, hi=1500, seed=3)[:n] * 0.004  # vinyl hiss
    mix = normalize(music) * 0.75 + (normalize(drums) * 0.55 if np.any(drums) else 0)
    # fade the loop seam
    edge = int(0.4 * SR)
    mix[:edge] *= np.linspace(0, 1, edge)
    mix[-edge:] *= np.linspace(1, 0, edge)
    return normalize(mix, 0.7)


def effect(name):
    if name == "whoosh":
        x = noise_hit(0.55, 0, lo=5000, hi=350, seed=4)
        return x * np.hanning(len(x)) * 0.8
    if name == "swoosh":
        t = np.arange(int(0.45 * SR)) / SR
        x = noise_hit(0.45, 0, seed=5)
        sweep = np.sin(2 * np.pi * np.cumsum(300 + 2500 * t / t[-1]) / SR)
        return lowpass(x * (0.5 + 0.5 * sweep), 6000) * np.hanning(len(t))
    if name == "pop":
        t = np.arange(int(0.12 * SR)) / SR
        return np.sin(2 * np.pi * np.cumsum(900 - 500 * t / t[-1]) / SR) * np.exp(-t * 35)
    if name == "ding":
        t = np.arange(int(1.2 * SR)) / SR
        return sum(np.sin(2 * np.pi * f * t) * np.exp(-t * d) * a
                   for f, d, a in ((1318.5, 3.5, 1.0), (2637, 6, 0.4), (3955, 9, 0.2)))
    if name == "boom":
        return kick(0.9) * 0.9 + noise_hit(0.9, 7, lo=400, seed=6) * 0.3
    if name == "click":
        return noise_hit(0.03, 150, hi=2000, seed=7)
    raise ValueError(name)


def _encode(samples, path, reverb=False):
    pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()
    af = "aecho=0.8:0.6:70|140:0.22|0.12," if reverb else ""
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "s16le", "-ar", str(SR), "-ac", "1", "-i", "-",
                    "-af", af + "aformat=channel_layouts=stereo", "-c:a", "aac", "-b:a", "160k", path],
                   input=pcm, check=True)


def ensure_track(style, cache_dir):
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, f"music-{style}.m4a")
    if not os.path.exists(path):
        _encode(song(style), path, reverb=True)
    return path


def ensure_sfx(name, cache_dir):
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, f"sfx-{name}.m4a")
    if not os.path.exists(path):
        _encode(normalize(effect(name), 0.8), path)
    return path


if __name__ == "__main__":
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "workspace", "library")
    for s in TRACKS:
        print(ensure_track(s, out))
    for e in SFX:
        print(ensure_sfx(e, out))
