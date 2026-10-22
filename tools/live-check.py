#!/usr/bin/env python3
"""The app in real time against the pretend radio app: hear, reply, transmit.

    python3 tools/live-check.py [--port 18872] [--radio-port 18871]

Starts tools/fakeradio.py playing the test slot (tests/vectors) on the wall
clock's FT8 boundaries, and the app standalone on 127.0.0.1 listening to
it with transmitting allowed; sets a station profile; waits for a real
slot's decodes; replies to "CQ DL1ABC JO62"; and checks the reply went out
in the next odd slot under the fake's lease - keyed, the audio while
keyed, unkeyed - and decodes as "DL1ABC GM4BRI IO75". About a minute.

It keys only the pretend radio app. Both processes are stopped by the PIDs
this script started, never by name.
"""

from __future__ import annotations

import argparse
import array
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def call(url, body=None):
    req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if r.headers.get_content_type() == "application/json" else raw)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def wait_for(fn, limit, what):
    end = time.time() + limit
    while time.time() < end:
        got = fn()
        if got:
            return got
        time.sleep(0.5)
    raise SystemExit(f"FAIL: {what} within {limit} s")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=18872)
    ap.add_argument("--radio-port", type=int, default=18871)
    a = ap.parse_args()
    work = Path(tempfile.mkdtemp(prefix="digimodes-live-", dir=os.path.expanduser("~/build")))
    radio = f"http://127.0.0.1:{a.radio_port}/"
    app = f"http://127.0.0.1:{a.port}/"
    env = {**os.environ, "DIGIMODES_DATA_DIR": str(work / "data"), "DIGIMODES_HTTP_PORT": str(a.port),
           "RADIO_URL": radio, "ALLOW_TRANSMIT": "1", "SHACK_STATION_DB": str(work / "station.db")}
    env.pop("SHACK_PLATFORM", None)
    procs = [subprocess.Popen([sys.executable, str(ROOT / "tools" / "fakeradio.py"), "--port", str(a.radio_port),
                               "--wav", str(ROOT / "tests" / "vectors" / "ft8-three-signals.wav")],
                              stdout=open(work / "fake.log", "w"), stderr=subprocess.STDOUT),
             subprocess.Popen([sys.executable, "-m", "app.main"], cwd=ROOT, env=env,
                              stdout=open(work / "app.log", "w"), stderr=subprocess.STDOUT)]
    try:
        wait_for(lambda: _up(app), 15, "the app answering")
        status, _ = call(app + "api/profile", {"callsign": "GM4BRI", "locator": "IO75RL", "country": "GB",
                                               "licence": {"class": "full"}})
        assert status == 200, status
        print("  waiting for a whole FT8 slot from the radio app...")
        cq = wait_for(lambda: next((d for d in call(app + "api/state")[1]["decodes"]
                                    if d["message"] == "CQ DL1ABC JO62"), None), 40, "a decode of the test slot")
        st = call(app + "api/state")[1]
        print(f"  ok    decoded {len(st['decodes'])} message(s) from the radio app, e.g. {cq['message']} "
              f"at {cq['freq']} Hz, {cq['snr']} dB, slot {cq['utc']}")
        status, body = call(app + "api/reply", {"id": cq["id"]})
        assert status == 200, body
        print(f"  ok    replied; Tx1 '{body['sequencer']['messages']['1']}' in the other half from the CQ")
        call(radio + "fake/config", {"clear": True})
        rep = wait_for(lambda: (lambda r: r if [e["event"] for e in r["events"]][-1:] == ["unkey"] else None)(
            call(radio + "fake/tx")[1]), 45, "the transmission to finish")
        kinds = [e["event"] for e in rep["events"]]
        assert kinds == ["key", "unkey"], kinds
        key_t = rep["events"][0]["t"]
        into_slot = key_t % 15.0
        assert int(key_t // 15) % 2 == (cq["parity"] + 1) % 2 and 0.0 <= into_slot < 1.0, (key_t, into_slot)
        _st, raw = call(radio + "fake/tx.wav")
        with wave.open(io.BytesIO(raw)) as w:
            pcm = array.array("h")
            pcm.frombytes(w.readframes(w.getnframes()))
        from app import engine
        rows = engine.Decoder(1).decode(engine.Slot("ft8", 1, 0.0, array.array("h", [0] * 6000) + pcm))
        assert [r["message"] for r in rows] == ["DL1ABC GM4BRI IO75"], rows
        print(f"  ok    keyed {into_slot:.2f} s into the other half's slot, {len(pcm) / 12000:.2f} s of audio under the "
              f"lease, unkeyed; the fake's recording decodes as {rows[0]['message']!r}")
        call(app + "api/halt", {})
        print("live-check: PASS")
        return 0
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()


def _up(base):
    try:
        return call(base + "healthz")[0] == 200
    except OSError:
        return False


if __name__ == "__main__":
    sys.exit(main())
