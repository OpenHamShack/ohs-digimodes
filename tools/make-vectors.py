#!/usr/bin/env python3
"""Write the recorded test slot the decode test reads (tests/vectors/).

    python3 tools/make-vectors.py

ohsdigi carries no WAV recordings of its own (its tests are held to WSJT-X's
jt9 by a decode corpus, rust/ohsdigi/testdata), so this makes one the way a
receiver would hear it: three standard messages from ohsdigi's own encoder,
started 0.5 s into a 15 s slot as WSJT-X sends them, at different
frequencies and strengths, under Gaussian noise across the whole 6 kHz
band, quantised to 16-bit at 12 kHz. The noise is seeded, so the file is
the same every time it is made; it is committed rather than made at test
time so the test reads a file through the same WAV path a recording takes.

The strengths are chosen so the weakest is near -18 dB in WSJT-X's 2500 Hz
reference bandwidth: weak enough to need the real decoder, strong enough
that depth 1 decodes it on any machine.
"""

from __future__ import annotations

import array
import math
import random
import sys
import wave
from pathlib import Path

import ohsdigi

ROOT = Path(__file__).resolve().parent.parent
RATE = 12000
SIGNALS = [("CQ DL1ABC JO62", 800.0, 0.030), ("GM4BRI F4XYZ JN18", 1500.0, 0.012),
           ("CQ DX EA8AB IL18", 2200.0, 0.006)]


def slot(mode: str, signals, seconds: float, noise: float = 0.05, seed: int = 9) -> array.array:
    n = int(RATE * seconds)
    acc = [0.0] * n
    start = int(0.5 * RATE)
    for msg, freq, amp in signals:
        w = array.array("f")
        w.frombytes(ohsdigi.encode_audio(mode, msg, freq=freq))
        for i, s in enumerate(w):
            if start + i < n:
                acc[start + i] += amp * s
    rnd = random.Random(seed)
    return array.array("h", (max(-32767, min(32767, int((a + rnd.gauss(0.0, noise)) * 32767))) for a in acc))


def main() -> int:
    out = ROOT / "tests" / "vectors" / "ft8-three-signals.wav"
    samples = slot("ft8", SIGNALS, 15.0)
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(samples.tobytes())
    rms = math.sqrt(sum(s * s for s in samples) / len(samples))
    print(f"{out}: {len(samples)} samples, rms {rms:.0f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
