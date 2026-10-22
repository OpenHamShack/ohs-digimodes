"""Where receive audio comes from: a radio app, a sound card, or a file.

Every source is an async iterator of `(samples, t_end)`: 16-bit mono PCM
already at 12 kHz (engine.RATE) and the wall-clock time of its last sample,
which is what engine.SlotBuffer places it in a slot by.

- **A radio app** (`RadioAppSource`, radioapp.py): the app holding the rig
  serves `api/audio/rx`, and this is the normal source on a controller and
  on a desktop beside a radio app (PLATFORM-PLAN 1, "works with ANY radio
  app"). It is also the only way to transmit (tx.py).
- **A sound card** (`SoundCardSource`, the desktop form only): a radio's
  USB codec or line in, through PortAudio (python-sounddevice). Receive
  only: a sound card has no PTT and no lease, and keying a radio by VOX
  from a decoder's automatic sequence is exactly the transmission nobody
  is holding (CONTROLLER-CONTRACT 6). It is imported only when chosen, so
  the store image needs neither PortAudio nor the module.
- **A file** (`WavSource`): a recording, for the tests and for anyone
  checking the decoder on known audio. Timed from a slot boundary, played
  as fast as it can be read unless `realtime`.
"""

from __future__ import annotations

import array
import asyncio
import time
import wave
from pathlib import Path

from . import engine
from .i18n import _

CHUNK_S = 0.02


def read_wav(path: str | Path) -> tuple[int, array.array]:
    """(rate, mono int16 samples) from a 16-bit WAV; stereo is mixed down."""
    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != 2:
            raise ValueError(_("only 16-bit WAV files can be read"))
        rate, channels = w.getframerate(), w.getnchannels()
        raw = array.array("h")
        raw.frombytes(w.readframes(w.getnframes()))
    if channels > 1:
        raw = array.array("h", (sum(raw[i:i + channels]) // channels for i in range(0, len(raw), channels)))
    return rate, raw


def write_wav(path: str | Path, samples: array.array, rate: int = engine.RATE) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(samples.tobytes())


class StreamClock:
    """Wall-clock stamps for a continuous stream, without the jitter.

    Stamping each network message (or sound-card block) with the time it
    arrived puts the arrival jitter into the audio: SlotBuffer places chunks
    by their stamps, so a message 5 ms late leaves 5 ms of silence and one
    5 ms early loses 5 ms of samples, and FT8 decodes fall apart (measured
    against tools/fakeradio.py: one decode of three, 20 Hz off). So the
    stream keeps its own clock - the first stamp plus the samples since -
    and re-anchors to the wall clock only when the two disagree by more
    than `slack`: a real stall (silence, in the right place) or a skip.
    """

    def __init__(self, slack: float = 0.25):
        self.slack = slack
        self.t: float | None = None

    def stamp(self, n: int, now: float) -> float:
        dur = n / engine.RATE
        if self.t is None or abs(now - (self.t + dur)) > self.slack:
            self.t = now - dur
        self.t += dur
        return self.t


class WavSource:
    kind = "wav"

    def __init__(self, path: str | Path, start: float | None = None, realtime: bool = False,
                 clock=time.time, mode: str = "ft8"):
        self.path = Path(path)
        self.realtime = realtime
        self.clock = clock
        # A file starts on a slot boundary: the next one after now, so its
        # first slot is a whole one.
        now = clock()
        self.start = start if start is not None else engine.slot_start(now, mode) + engine.PERIOD[mode]
        self.error = ""

    def describe(self) -> str:
        return self.path.name

    async def __aiter__(self):
        rate, raw = read_wav(self.path)
        samples = engine.Resampler(rate)(raw)
        step = round(engine.RATE * CHUNK_S)
        for i in range(0, len(samples), step):
            chunk = samples[i:i + step]
            t_end = self.start + (i + len(chunk)) / engine.RATE
            if self.realtime:
                wait = t_end - self.clock()
                if wait > 0:
                    await asyncio.sleep(wait)
            else:
                await asyncio.sleep(0)
            yield chunk, t_end


class SoundCardSource:
    kind = "soundcard"

    def __init__(self, device: str | int | None = None, clock=time.time):
        self.device = device if device not in ("", None) else None
        self.clock = clock
        self.error = ""

    def describe(self) -> str:
        return str(self.device) if self.device is not None else _("default input")

    @staticmethod
    def devices() -> list[dict]:
        """The machine's input devices, or [] when PortAudio is missing."""
        try:
            import sounddevice as sd
            return [{"id": i, "name": d["name"]} for i, d in enumerate(sd.query_devices())
                    if d.get("max_input_channels", 0) > 0]
        except Exception:               # noqa: BLE001 - no PortAudio: no list, said so on the page
            return []

    async def __aiter__(self):
        import sounddevice as sd

        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue(maxsize=200)

        def callback(data, frames, _time, _status):
            stamp = self.clock()
            try:
                loop.call_soon_threadsafe(q.put_nowait, (bytes(data), stamp))
            except RuntimeError:        # the loop has gone: the stream is closing
                pass

        # 12 kHz is asked for first; PortAudio's ALSA, WASAPI and Core Audio
        # hosts mostly convert. When a device will not, 48 kHz and our own
        # resampler.
        last = None
        for rate in (engine.RATE, 48000):
            try:
                stream = sd.RawInputStream(samplerate=rate, channels=1, dtype="int16", device=self.device,
                                           blocksize=int(rate * CHUNK_S), callback=callback)
                break
            except Exception as e:      # noqa: BLE001 - try the next rate
                last = e
        else:
            raise OSError(str(last))
        resample = engine.Resampler(rate)
        clock = StreamClock()
        with stream:
            while True:
                raw, stamp = await q.get()
                s = array.array("h")
                s.frombytes(raw)
                out = resample(s)
                yield out, clock.stamp(len(out), stamp)


class RadioAppSource:
    kind = "radio"

    def __init__(self, radio, clock=time.time):
        self.radio = radio
        self.clock = clock
        self.error = ""

    def describe(self) -> str:
        return self.radio.base

    async def __aiter__(self):
        resample = None
        clock = StreamClock()
        async for rate, raw in self.radio.rx():
            if resample is None or resample.src != rate:
                resample = engine.Resampler(rate)
            s = array.array("h")
            s.frombytes(raw[: len(raw) - len(raw) % 2])
            out = resample(s)
            yield out, clock.stamp(len(out), self.clock())
