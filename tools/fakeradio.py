#!/usr/bin/env python3
"""A pretend radio app: the family's audio and lease contract, and no radio.

    python3 tools/fakeradio.py [--port 18871] [--wav FILE] [--rate 12000]

What Digital Modes needs from the app that holds the rig, and nothing
else (app/radioapp.py says where each piece is written down):

- `GET api/state`: `tx.remote` (the lease's holder, or null), `onAir`;
- `GET api/audio/rx` (websocket): the format, then s16le mono at `rate`,
  20 ms per message, in real time - a WAV looped from slot boundaries of the
  wall clock, so its decodes land in the right slots, or silence;
- `POST api/ptt` / `api/ptt/heartbeat`: the lease, field for field: 5 s
  without a heartbeat unkeys, a heartbeat from anyone but the holder is
  409, a refused key-up is 200 with `onAir: false` and `txError`;
- `GET api/audio/tx` (websocket): the rate it wants, then PCM is taken
  only while keyed - audio arriving unkeyed closes the socket with an
  error, which is the bug it exists to catch.

`/fake/tx` reports what was transmitted (key-ups, unkeys, samples);
`/fake/tx.wav` is the transmitted audio, so a test can decode what was
"sent" with the real decoder. `POST /fake/config {"refuse": "why",
"noLease": true, "rate": 48000}` makes it a radio app that says no, one
from before the lease, or one at another rate.

It keys nothing. It is the only thing Digital Modes' transmit tests key.
"""

from __future__ import annotations

import argparse
import array
import asyncio
import io
import json
import time
import wave
from pathlib import Path

from aiohttp import WSMsgType, web

LEASE_S = 5.0


class Fake:
    def __init__(self, wav: Path | None = None, rate: int = 12000, clock=time.time):
        self.rate = rate
        self.clock = clock
        self.refuse = ""
        self.no_lease = False
        self.holder: str | None = None
        self.beat = 0.0
        self.events: list[dict] = []
        self.tx = array.array("h")
        self.tx_rate = rate
        self.rx_audio = array.array("h")
        if wav is not None:
            with wave.open(str(wav), "rb") as w:
                self.rx_audio.frombytes(w.readframes(w.getnframes()))
                self.rx_wav_rate = w.getframerate()

    def keyed(self) -> bool:
        if self.holder is not None and self.clock() - self.beat > LEASE_S:
            self.events.append({"event": "lease-expired", "remote": self.holder, "t": self.clock()})
            self.holder = None
        return self.holder is not None

    def state(self) -> dict:
        on = self.keyed()
        st = {"onAir": on, "txError": self.refuse or None}
        if not self.no_lease:
            st["tx"] = {"remote": {"id": self.holder, "leaseSeconds": LEASE_S} if on else None}
        return st


FAKE = web.AppKey("fake", Fake)


async def state(request):
    return web.json_response(request.app[FAKE].state())


async def ptt(request):
    f = request.app[FAKE]
    body = await request.json()
    if not body.get("on"):
        if f.holder is not None:
            f.events.append({"event": "unkey", "remote": f.holder, "t": f.clock()})
        f.holder = None
        return web.json_response(f.state())
    if f.refuse:
        return web.json_response({**f.state(), "onAir": False, "txError": f.refuse})
    if f.keyed() and f.holder != body.get("remote"):
        return web.json_response({**f.state(), "onAir": False, "txError": "keyed by another client"})
    f.holder, f.beat = str(body.get("remote") or "?"), f.clock()
    f.events.append({"event": "key", "remote": f.holder, "t": f.clock()})
    return web.json_response(f.state())


async def heartbeat(request):
    f = request.app[FAKE]
    body = await request.json()
    if not f.keyed() or body.get("remote") != f.holder:
        return web.json_response({"error": "not the lease holder"}, status=409)
    f.beat = f.clock()
    return web.json_response(f.state())


async def audio_rx(request):
    f = request.app[FAKE]
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    await ws.send_json({"direction": "rx", "rate": f.rate, "encoding": "s16le", "format": "pcm16",
                        "channels": 1, "flow": "ack"})
    step = f.rate // 50
    n = 0
    t0 = f.clock()
    # The recording repeats every 15 s from the wall clock's slot boundaries.
    src = f.rx_audio
    period_n = 15 * f.rate
    try:
        while not ws.closed:
            now = t0 + n / f.rate
            pos = round((now % 15.0) * f.rate)
            if src and getattr(f, "rx_wav_rate", f.rate) == f.rate:
                chunk = array.array("h", (src[(pos + i) % period_n] if (pos + i) % period_n < len(src) else 0
                                          for i in range(step)))
            else:
                chunk = array.array("h", bytes(2 * step))
            await ws.send_bytes(chunk.tobytes())
            n += step
            ahead = t0 + n / f.rate - f.clock()
            if ahead > 0:
                await asyncio.sleep(ahead)
    except (ConnectionResetError, RuntimeError):
        pass
    return ws


async def audio_tx(request):
    f = request.app[FAKE]
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    f.tx_rate = f.rate
    await ws.send_json({"direction": "tx", "rate": f.rate, "encoding": "s16le", "channels": 1})
    async for msg in ws:
        if msg.type == WSMsgType.BINARY:
            if not f.keyed():
                f.events.append({"event": "audio-while-unkeyed", "t": f.clock()})
                await ws.send_json({"error": "not keyed"})
                await ws.close()
                break
            a = array.array("h")
            a.frombytes(msg.data)
            f.tx.extend(a)
    return ws


async def fake_tx(request):
    f = request.app[FAKE]
    return web.json_response({"events": f.events, "samples": len(f.tx), "rate": f.tx_rate,
                              "keyed": f.keyed()})


async def fake_tx_wav(request):
    f = request.app[FAKE]
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(f.tx_rate)
        w.writeframes(f.tx.tobytes())
    return web.Response(body=out.getvalue(), content_type="audio/wav")


async def fake_config(request):
    f = request.app[FAKE]
    body = await request.json()
    f.refuse = str(body.get("refuse") or "")
    f.no_lease = bool(body.get("noLease"))
    f.rate = int(body.get("rate") or f.rate)
    if body.get("clear"):
        f.events.clear()
        f.tx = array.array("h")
    return web.json_response({"ok": True})


def make_app(fake: Fake) -> web.Application:
    app = web.Application()
    app[FAKE] = fake
    r = app.router
    r.add_get("/api/state", state)
    r.add_post("/api/ptt", ptt)
    r.add_post("/api/ptt/heartbeat", heartbeat)
    r.add_get("/api/audio/rx", audio_rx)
    r.add_get("/api/audio/tx", audio_tx)
    r.add_get("/fake/tx", fake_tx)
    r.add_get("/fake/tx.wav", fake_tx_wav)
    r.add_post("/fake/config", fake_config)
    return app


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=18871)
    ap.add_argument("--wav", type=Path)
    ap.add_argument("--rate", type=int, default=12000)
    ap.add_argument("--pid-file", type=Path)
    a = ap.parse_args()
    if a.pid_file:
        import os
        a.pid_file.write_text(str(os.getpid()))
    web.run_app(make_app(Fake(a.wav, a.rate)), host=a.host, port=a.port, print=None, access_log=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
