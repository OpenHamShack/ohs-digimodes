"""Slots of audio in, decodes out: the clock and ohsdigi.

FT8 and FT4 are synchronous: every station transmits in slots that start on
the UTC clock (FT8 every 15 s, FT4 every 7.5 s), and a decoder reads one
whole slot at once. So the receive side is two pieces:

- `SlotBuffer` takes PCM as it arrives, each chunk stamped with the wall
  time of its last sample, and hands back a slot's audio once it has it.
  The stamp is what places audio in a slot: a source that stalls leaves a
  gap (silence) rather than sliding the next audio into the wrong second,
  which would put every decode's DT out by the length of the stall.
- `decode_slot` is ohsdigi's one call (`ohsdigi.decode`), never a second
  decoder (GUIDE 1, "shared components, one of each").

A slot is decoded `EARLY_S` before it ends, as WSJT-X does: a transmission
ends 13.1 s into an FT8 slot (0.5 s start, 12.64 s long), so by 14.0 s the
signals are in and there is a second left to decide what to send in the
next slot. The tail is padded with silence (ohsdigi pads short audio).

Everything takes the time as an argument, so a WAV file or a test drives
the same code faster than real time.
"""

from __future__ import annotations

import array
import math
import time
from dataclasses import dataclass

RATE = 12000
PERIOD = {"ft8": 15.0, "ft4": 7.5}
#: When to decode, seconds into the slot (see the module docstring). FT4's
#: transmission is 0.5 s + 5.04 s long.
DECODE_AT = {"ft8": 14.0, "ft4": 6.4}
#: Occupied bandwidth (Hz) of one transmission, for the licence check:
#: 8 tones at 6.25 Hz (FT8), 4 tones at 20.833 Hz (FT4), rounded up.
WIDTH_HZ = {"ft8": 50, "ft4": 90}
MODES = tuple(PERIOD)


def slot_index(t: float, mode: str) -> int:
    return math.floor(t / PERIOD[mode] + 1e-9)


def slot_start(t: float, mode: str) -> float:
    return slot_index(t, mode) * PERIOD[mode]


def utc_label(t: float, mode: str) -> str:
    """HHMMSS of the slot, as WSJT-X labels a decode (FT4 slots at .5 s
    are labelled by the whole second they start in)."""
    return time.strftime("%H%M%S", time.gmtime(slot_start(t, mode)))


class Resampler:
    """Linear interpolation to 12 kHz from whatever rate a source gives.

    Good enough for FT8/FT4: their signals sit at 200-3000 Hz, far under
    every source's Nyquist, and ohsdigi measures SNR against the slot's own
    noise. A source already at 12 kHz (Icom's default) passes through.
    Keeps its phase across chunks, so chunk boundaries are seamless.
    """

    def __init__(self, src_rate: int, dst_rate: int = RATE):
        self.src, self.dst = int(src_rate), int(dst_rate)
        self.pos = 0.0           # position of the next output sample, in input samples
        self.prev = 0            # the last input sample of the previous chunk

    def __call__(self, samples: array.array) -> array.array:
        if self.src == self.dst:
            return samples
        step = self.src / self.dst
        out = array.array("h")
        n = len(samples)
        prev = self.prev
        pos = self.pos           # may be -1..0: between the last chunk's end and this one's start
        while pos < n - 1:
            i = math.floor(pos)
            frac = pos - i
            a = samples[i] if i >= 0 else prev
            b = samples[i + 1]
            out.append(int(a + (b - a) * frac))
            pos += step
        self.pos = pos - n
        if n:
            self.prev = samples[-1]
        return out


@dataclass
class Slot:
    mode: str
    index: int
    start: float
    audio: array.array


class SlotBuffer:
    """PCM in, whole slots out (see the module docstring)."""

    #: How much of a slot must have arrived for it to be worth decoding: a
    #: slot that started before the source did has no useful beginning.
    MIN_COVER = 0.6

    def __init__(self, mode: str):
        self.mode = mode
        self.samples = array.array("h")
        self.first = None          # absolute sample number of samples[0]
        self.done = None           # index of the last slot handed out

    def reset(self, mode: str | None = None) -> None:
        self.__init__(mode or self.mode)

    def feed(self, chunk: array.array, t_end: float) -> list[Slot]:
        n_end = round(t_end * RATE)
        n_start = n_end - len(chunk)
        if self.first is None:
            self.first = n_start
        have_end = self.first + len(self.samples)
        if n_start > have_end:
            # A gap: silence where the source said nothing.
            self.samples.extend(array.array("h", bytes(2 * (n_start - have_end))))
        elif n_start < have_end:
            # Overlap (a clock stepping back): keep what we had.
            chunk = chunk[have_end - n_start:]
        self.samples.extend(chunk)
        return self._ready()

    def _ready(self) -> list[Slot]:
        out = []
        period = PERIOD[self.mode]
        end_n = self.first + len(self.samples)
        t_have = end_n / RATE
        index = slot_index(self.first / RATE, self.mode)
        if self.done is not None:
            index = max(index, self.done + 1)
        while (index * period + DECODE_AT[self.mode]) <= t_have + 1e-9:
            s0 = round(index * period * RATE)
            s1 = round((index * period + period) * RATE)
            lo, hi = max(s0, self.first), min(s1, end_n)
            cover = (hi - lo) / (s1 - s0)
            self.done = index
            if cover >= self.MIN_COVER * DECODE_AT[self.mode] / period:
                audio = array.array("h", bytes(2 * (lo - s0)))
                audio.extend(self.samples[lo - self.first:hi - self.first])
                out.append(Slot(self.mode, index, index * period, audio))
            index += 1
        # Keep from the start of the next slot not yet handed out.
        keep_from = round((self.done + 1) * period * RATE) if self.done is not None else self.first
        if keep_from > self.first:
            drop = min(keep_from - self.first, len(self.samples))
            del self.samples[:drop]
            self.first += drop
        return out


def decoder_available() -> str:
    """'' when ohsdigi imports, else why not (the page says so)."""
    try:
        import ohsdigi  # noqa: F401
    except Exception as e:      # noqa: BLE001 - a missing native module on an odd platform
        return str(e) or type(e).__name__
    return ""


class Decoder:
    """ohsdigi, with the callsign hashes it learns kept across slots."""

    def __init__(self, depth: int = 2):
        import ohsdigi
        self.ohsdigi = ohsdigi
        self.depth = depth
        self.hashes = ohsdigi.CallHashes()

    def decode(self, slot: Slot, freq_qso: float = 1500.0, mycall: str = "", dxcall: str = "",
               freq_min: float = 200.0, freq_max: float = 3000.0) -> list[dict]:
        rows = self.ohsdigi.decode(slot.mode, slot.audio, time=slot.start, depth=self.depth,
                                  freq_min=freq_min, freq_max=freq_max, freq_qso=freq_qso,
                                  mycall=mycall, dxcall=dxcall, hashes=self.hashes)
        out = []
        for r in rows:
            out.append({"slot": slot.start, "utc": utc_label(slot.start, slot.mode), "mode": slot.mode,
                        "parity": slot.index % 2, "snr": int(r["snr"]), "dt": round(float(r["dt"]), 1),
                        "freq": int(round(float(r["freq"]))), "message": str(r["message"]).strip()})
        out.sort(key=lambda d: d["freq"])
        return out

    def sent(self, mode: str, message: str) -> str:
        """What a transmission of `message` would really carry: ohsdigi (as
        WSJT-X) cuts free text to 13 characters rather than refusing it."""
        return str(self.ohsdigi.encode(mode, message).sent).strip()

    def encode(self, mode: str, message: str, freq: float, rate: int = RATE) -> bytes:
        """The transmission as float32 samples at full scale +-1.0.

        Raises ohsdigi.BadMessage for a message WSJT-X would not send."""
        return self.ohsdigi.encode_audio(mode, message, freq=freq, sample_rate=float(rate))


def float_to_s16(raw: bytes, gain: float = 0.5) -> bytes:
    """float32 +-1.0 to s16le, at `gain` of full scale.

    Half scale by default: a sound card or a radio's USB codec driven to
    full scale clips, and FT8 is a constant-envelope signal, so the level
    into the radio is set by its own gain control, never by overdriving.
    """
    f = array.array("f")
    f.frombytes(raw)
    g = max(0.0, min(1.0, gain)) * 32767
    return array.array("h", (int(max(-1.0, min(1.0, s)) * g) for s in f)).tobytes()
