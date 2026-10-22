"""OpenHamShack Digital Modes: the server, in both forms.

One process, one event loop. The receive loop reads the chosen source,
cuts it into slots (engine.SlotBuffer) and decodes each slot in a worker
thread (ohsdigi releases the GIL); the slot loop wakes just before each slot
boundary and asks the sequencer what, if anything, to send in it; the web
layer serves the page and its JSON. The same code runs as the store image
(`SHACK_PLATFORM=dock360`, behind the controller's proxy) and inside the
desktop window (app/desktop.py, on loopback behind a per-launch token).

The page polls `api/state` once a second rather than holding a socket:
decodes arrive every 15 s (7.5 s for FT4), a poll is one small JSON, and a
poll survives every proxy between a browser and a controller without the
read-timeout care a long-lived socket needs.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import os
import time
from pathlib import Path

from aiohttp import web

from . import VERSION, engine, httputil
from . import platform as P
from .audio import RadioAppSource, SoundCardSource, WavSource
from .i18n import _
from .pad import Pad
from .radioapp import RadioApp
from .sequencer import Sequencer
from .settings import Installation, Operating, Store
from .station import Station, store_path
from .tx import Transmitter

log = logging.getLogger("digimodes")
STATIC = Path(__file__).resolve().parent / "static"
#: How long before a slot boundary the slot loop decides what to send:
#: after the previous slot's decode (engine.DECODE_AT), before the key-up.
DECIDE_LEAD_S = 0.4


def json_error(message: str, status: int, code: str = "", **extra) -> web.Response:
    body = {"error": message, **extra}
    if code:
        body["code"] = code
    return web.json_response(body, status=status)


class App:
    def __init__(self, installation: Installation | None = None, *, clock=time.time, platform=None):
        self.installation = inst = installation or Installation()
        self.clock = clock
        self.platform_mode = P.platform_mode()
        self.secret = os.environ.get(P.PROXY_SECRET_ENV, "")
        self.platform = platform if platform is not None else (P.PlatformClient.from_env() if self.platform_mode else None)
        inst.data_dir.mkdir(parents=True, exist_ok=True)
        seed = Operating(radio_url=inst.radio_url, source="radio" if inst.radio_url else "none")
        self.ops = Store(inst.data_dir / "settings.json", seed)
        self.station = Station(store_path(inst.data_dir), platform=self.platform, app_version=VERSION,
                               outbox=inst.data_dir / "logbook-outbox.json")
        self.decoder_error = engine.decoder_available()
        self.decoder = engine.Decoder(inst.depth) if not self.decoder_error else None
        self.sequencer = Sequencer()
        self.tx = Transmitter(station=self.station, decoder=self.decoder, allow=self.transmit_allowed, clock=clock)
        self.pad = Pad(self)
        self.buffer = engine.SlotBuffer(self.ops.value.mode)
        self.decodes: collections.deque = collections.deque(maxlen=400)
        self.seq_no = 0
        self.pending = None          # a finished contact waiting to be logged
        self.logged: list[dict] = []
        self.source = None
        self.source_error = ""
        self.source_state = "stopped"
        self.last_slot: dict | None = None
        self.tx_history: collections.deque = collections.deque(maxlen=20)
        self._rx_task: asyncio.Task | None = None
        self._tasks: list[asyncio.Task] = []
        self._radio: RadioApp | None = None

    # -- the source -------------------------------------------------------------

    def make_source(self):
        op = self.ops.value
        if op.source == "radio" and op.radio_url:
            self._radio = RadioApp(op.radio_url, token=self.installation.radio_token,
                                   password=self.installation.radio_password,
                                   client_id=f"digimodes-{self.station.callsign() or 'station'}")
            return RadioAppSource(self._radio, clock=self.clock)
        if op.source == "soundcard" and self.installation.form == "desktop":
            return SoundCardSource(op.sound_device or None, clock=self.clock)
        if op.source == "wav" and op.wav_path and not self.platform_mode:
            return WavSource(op.wav_path, realtime=True, clock=self.clock, mode=op.mode)
        return None

    async def rx_loop(self) -> None:
        while True:
            src = self.make_source()
            self.source, self.source_error = src, ""
            if src is None:
                self.source_state = "none"
                return
            self.source_state = "connecting"
            try:
                async for chunk, t_end in src:
                    self.source_state = "receiving"
                    for slot in self.buffer.feed(chunk, t_end):
                        await self.on_slot(slot)
                self.source_state = "ended"
                if src.kind == "wav":
                    return
            except asyncio.CancelledError:
                raise
            except Exception as e:      # noqa: BLE001 - shown on the page, retried
                self.source_error = str(e) or type(e).__name__
                self.source_state = "error"
                log.info("receive source %s: %s", src.describe(), self.source_error)
            await asyncio.sleep(3)

    def restart_rx(self) -> None:
        if self._rx_task is not None:
            self._rx_task.cancel()
        self.buffer.reset(self.ops.value.mode)
        self._rx_task = asyncio.ensure_future(self.rx_loop())

    async def on_slot(self, slot: engine.Slot) -> list[dict]:
        if self.decoder is None:
            return []
        seq = self.sequencer
        dx = seq.contact.call if seq.contact else ""
        op = self.ops.value
        loop = asyncio.get_running_loop()
        rows = await loop.run_in_executor(None, lambda: self.decoder.decode(
            slot, freq_qso=op.rx_hz, mycall=self.station.callsign(), dxcall=dx))
        my = self.station.callsign()
        for r in rows:
            self.seq_no += 1
            r["id"] = self.seq_no
            words = r["message"].split()
            r["toMe"] = bool(my) and bool(words) and words[0].strip("<>") == my
            r["cq"] = bool(words) and words[0] == "CQ"
            self.decodes.append(r)
        self.last_slot = {"slot": slot.start, "mode": slot.mode, "count": len(rows)}
        seq.mycall, seq.mygrid = my, self.station.locator()
        seq.heard(rows, self.clock())
        done = seq.take_complete()
        if done is not None:
            self.pending = done
        if self.platform is not None and rows:
            asyncio.ensure_future(self.platform.post_spots(self.spots(rows)))
        return rows

    def spots(self, rows: list[dict]) -> list[dict]:
        from .messages import parse
        op = self.ops.value
        out = []
        for r in rows:
            p = parse(r["message"])
            if not p.de:
                continue
            out.append({"source": "decode", "heardAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(r["slot"])),
                        "call": p.de, "freqHz": int(op.dial_hz + r["freq"]), "mode": r["mode"].upper(),
                        "snrDb": r["snr"], "gridsquare": p.grid or None, "message": r["message"],
                        "spotter": self.station.callsign(), "app": "shack-digimodes"})
        return out

    # -- transmitting -----------------------------------------------------------

    async def slot_loop(self) -> None:
        while True:
            op = self.ops.value
            period = engine.PERIOD[op.mode]
            now = self.clock()
            nxt = engine.slot_start(now, op.mode) + period
            await asyncio.sleep(max(0.0, nxt - DECIDE_LEAD_S - now))
            op = self.ops.value
            if self.tx.busy:
                await asyncio.sleep(DECIDE_LEAD_S + 0.05)
                continue
            seq = self.sequencer
            seq.mycall, seq.mygrid = self.station.callsign(), self.station.locator()
            idx = engine.slot_index(nxt + 0.01, op.mode)
            msg = seq.next_message(idx)
            if msg:
                self.tx.start(self.source, op.mode, op.dial_hz, op.tx_hz, msg, idx * engine.PERIOD[op.mode])
                self.tx_history.append({"slot": idx * engine.PERIOD[op.mode], "message": msg})
                done = seq.take_complete()
                if done is not None:
                    self.pending = done
            await asyncio.sleep(DECIDE_LEAD_S + 0.05)

    def tx_refusal(self, message: str) -> str | None:
        op = self.ops.value
        return self.tx.refusal(self.source, op.mode, op.dial_hz, op.tx_hz, message)

    def transmit_allowed(self) -> bool:
        """The transmit switch: the manifest's on a controller, the person's
        own (in Settings) in the desktop window."""
        if self.installation.form == "desktop" and not self.platform_mode:
            return self.installation.allow_transmit or self.ops.value.allow_transmit
        return self.installation.allow_transmit

    def probe_message(self) -> str:
        """A CQ from this station: what the checks that do not depend on the
        message (switch, source, licence) are asked about before a sequence
        starts. The real message is checked again when it is sent."""
        from .messages import tx_messages
        return tx_messages(self.station.callsign() or "N0CALL", self.station.locator(), "", "")[6]

    async def halt(self) -> None:
        self.sequencer.halt()
        await self.tx.halt()

    # -- settings -----------------------------------------------------------------

    async def configure(self, changes: dict) -> Operating:
        before = self.ops.value.public()
        if self.platform_mode:
            changes = {k: v for k, v in changes.items() if k not in ("wavPath",)}
        if self.installation.form != "desktop":
            # Only the desktop's own person may switch transmitting on here.
            changes = {k: v for k, v in changes.items() if k != "allowTransmit"}
        if self.installation.form != "desktop" and changes.get("source") == "soundcard":
            raise ValueError(_("a sound card is a source in the desktop app only"))
        op = self.ops.update(changes)
        after = op.public()
        if any(before[k] != after[k] for k in ("source", "radioUrl", "soundDevice", "wavPath", "mode")):
            if before["mode"] != after["mode"] or before["dialHz"] != after["dialHz"]:
                await self.halt()
            self.restart_rx()
        elif before["dialHz"] != after["dialHz"]:
            # A band change ends whatever was being sent on the old one.
            await self.halt()
        return op

    # -- logging --------------------------------------------------------------------

    async def log_pending(self, edits: dict) -> tuple[int, dict]:
        c = self.pending
        if c is None:
            return 409, {"error": _("there is no finished contact to log"), "code": "nothing_to_log"}
        op = self.ops.value
        fields = self.station.qso_fields(c, op.mode, op.dial_hz, op.tx_hz, self.clock())
        for k in ("call", "rst_sent", "rst_rcvd", "gridsquare", "comment", "name"):
            if isinstance(edits.get(k), str) and edits[k].strip():
                fields[k] = edits[k].strip()
        try:
            rec = await self.station.log(fields)
        except (ValueError, RuntimeError) as e:
            return 400, {"error": str(e), "code": "bad_request"}
        self.pending = None
        return 200, rec

    # -- what the page reads ----------------------------------------------------------

    def state(self, since: int = 0) -> dict:
        op = self.ops.value
        now = self.clock()
        period = engine.PERIOD[op.mode]
        return {
            "version": VERSION, "now": now, "mode": op.mode, "period": period,
            "slotSecond": round(now - engine.slot_start(now, op.mode), 2),
            "slotParity": engine.slot_index(now, op.mode) % 2,
            "operating": op.public(), "installation": self.installation.public(),
            "source": {"kind": self.source.kind if self.source else "none", "state": self.source_state,
                       "error": self.source_error, "what": self.source.describe() if self.source else ""},
            "decoder": {"ok": self.decoder is not None, "error": self.decoder_error},
            "decodes": [d for d in self.decodes if d["id"] > since],
            "lastId": self.seq_no, "lastSlot": self.last_slot,
            "sequencer": self.sequencer.public(),
            "tx": {"sending": self.tx.sending, "error": self.tx.last_error,
                   "refusal": self.tx_refusal(self.probe_message())},
            "pending": self.pending.public() if self.pending else None,
            "station": self.station.public(),
            "platform": self.platform_mode,
        }

    async def start(self) -> None:
        self.restart_rx()
        self._tasks = [asyncio.ensure_future(self.slot_loop()), asyncio.ensure_future(self.refresh_loop())]

    async def refresh_loop(self) -> None:
        while True:
            try:
                await self.station.refresh()
            except Exception as e:      # noqa: BLE001 - the profile is retried; never fatal
                log.info("profile refresh: %s", e)
            await asyncio.sleep(20)

    async def stop(self) -> None:
        await self.halt()
        for t in self._tasks + ([self._rx_task] if self._rx_task else []):
            t.cancel()
        if self._radio is not None:
            await self._radio.close()
        self.station.close()


# -- the web layer -------------------------------------------------------------------

APP_KEY = web.AppKey("digimodes", App)


@web.middleware
async def guard(request: web.Request, handler):
    """Platform mode: the proxy's secret and the level, on every request but
    /healthz. Standalone: nothing here (the desktop shell's token guard runs
    outside this, and a developer's run binds 127.0.0.1)."""
    app: App = request.app[APP_KEY]
    if request.path == "/healthz" or not app.platform_mode:
        return await handler(request)
    who = P.identity(request.headers, app.secret)
    if who is None:
        return json_error(_("This app is reached through the OpenHamShack controller."), 401, "proxy_required")
    needs = P.required_level(request.method, request.path)
    if needs != "none" and not P.level_at_least(who.level, needs):
        return web.json_response(P.refusal_body(who.level, needs), status=403)
    request["identity"] = who
    return await handler(request)


async def _body(request: web.Request) -> dict:
    try:
        data = await request.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


async def page(request: web.Request) -> web.Response:
    html = (STATIC / "index.html").read_text(encoding="utf-8").replace("{{BASE}}", httputil.base(request))
    return web.Response(text=html, content_type="text/html", headers={
        "Cache-Control": "no-cache",
        # pywebview's bridge on WebKitGTK needs unsafe-eval (GUIDE 5).
        "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-eval'; "
                                   "style-src 'self'; img-src 'self' data:; connect-src 'self'"})


async def healthz(request):
    return web.json_response({"ok": True, "version": VERSION})


async def me(request):
    app: App = request.app[APP_KEY]
    who = request.get("identity")
    return web.json_response({"form": app.installation.form, "platform": app.platform_mode,
                              "identity": who.public() if who else None,
                              "level": who.level if who else "transmit"})


async def state(request):
    try:
        since = int(request.query.get("since", "0"))
    except ValueError:
        since = 0
    return web.json_response(request.app[APP_KEY].state(since))


async def frequencies(request):
    return web.json_response({"frequencies": request.app[APP_KEY].station.frequencies()})


async def config(request):
    app: App = request.app[APP_KEY]
    try:
        op = await app.configure(await _body(request))
    except (ValueError, TypeError) as e:
        return json_error(str(e), 400, "bad_request")
    return web.json_response({"ok": True, "operating": op.public()})


async def profile_post(request):
    app: App = request.app[APP_KEY]
    try:
        app.station.update(await _body(request))
    except PermissionError as e:
        return json_error(str(e), 403, "forbidden")
    except (ValueError, RuntimeError) as e:
        return json_error(str(e), 400, "bad_request")
    return web.json_response({"ok": True, "station": app.station.public()})


async def cq(request):
    app: App = request.app[APP_KEY]
    why = app.tx_refusal(app.probe_message())
    if why:
        return json_error(why, 409, "tx_refused")
    body = await _body(request)
    op = app.ops.value
    parity = int(body["parity"]) % 2 if "parity" in body else (engine.slot_index(app.clock(), op.mode) + 1) % 2
    app.sequencer.mycall, app.sequencer.mygrid = app.station.callsign(), app.station.locator()
    app.sequencer.call_cq(parity)
    return web.json_response({"ok": True, "sequencer": app.sequencer.public()})


async def reply(request):
    app: App = request.app[APP_KEY]
    body = await _body(request)
    d = next((x for x in app.decodes if x["id"] == body.get("id")), None)
    if d is None:
        return json_error(_("that decode is no longer in the list"), 404, "not_found")
    seq = app.sequencer
    seq.mycall, seq.mygrid = app.station.callsign(), app.station.locator()
    why = app.tx_refusal(app.probe_message())
    if why:
        return json_error(why, 409, "tx_refused")
    result = seq.reply(d, app.clock())
    if result:
        texts = {"nocall": _("no callsign is set in the station profile"),
                 "finished": _("that contact is already finished"),
                 "unanswerable": _("that message is not a CQ or a call to this station")}
        return json_error(texts.get(result, result), 409, result)
    if body.get("moveTx"):
        await app.configure({"txHz": d["freq"]})
    await app.configure({"rxHz": d["freq"]})
    return web.json_response({"ok": True, "sequencer": seq.public()})


async def tx_enable(request):
    app: App = request.app[APP_KEY]
    body = await _body(request)
    if not body.get("on"):
        await app.halt()
        return web.json_response({"ok": True})
    seq = app.sequencer
    if seq.step is None:
        return json_error(_("there is nothing to send: call CQ or reply to a decode first"), 409, "nothing_to_send")
    why = app.tx_refusal(seq.public()["messages"].get(str(seq.step)) or app.probe_message())
    if why:
        return json_error(why, 409, "tx_refused")
    seq.resume()
    return web.json_response({"ok": True, "sequencer": seq.public()})


async def halt(request):
    await request.app[APP_KEY].halt()
    return web.json_response({"ok": True})


async def log_post(request):
    status, body = await request.app[APP_KEY].log_pending(await _body(request))
    return web.json_response(body, status=status)


async def log_dismiss(request):
    request.app[APP_KEY].pending = None
    return web.json_response({"ok": True})


async def log_get(request):
    return web.json_response({"qsos": request.app[APP_KEY].station.recent()})


async def devices(request):
    app: App = request.app[APP_KEY]
    return web.json_response({"devices": SoundCardSource.devices() if app.installation.form == "desktop" else []})


async def busy(request):
    app: App = request.app[APP_KEY]
    return web.json_response({"busy": app.tx.sending is not None, "onAir": app.tx.sending is not None})


async def release(request):
    await request.app[APP_KEY].halt()
    return web.json_response({"released": True})


async def pad_action(request):
    body = await _body(request)
    status, out = await request.app[APP_KEY].pad.run(str(body.get("action", "")), str(body.get("kind", "press")),
                                                     int(body.get("step", 1) or 1))
    return web.json_response(out, status=status)


async def pad_faces(request):
    return web.json_response(request.app[APP_KEY].pad.faces())


def build_app(app: App | None = None, outer: list | None = None, start: bool = True) -> web.Application:
    core = app or App()
    w = web.Application(middlewares=[*(outer or []), guard])
    w[APP_KEY] = core
    r = w.router
    r.add_get("/", page)
    r.add_get("/healthz", healthz)
    r.add_get("/api/me", me)
    r.add_get("/api/state", state)
    r.add_get("/api/frequencies", frequencies)
    r.add_get("/api/log", log_get)
    r.add_get("/api/devices", devices)
    r.add_post("/api/config", config)
    r.add_post("/api/profile", profile_post)
    r.add_post("/api/cq", cq)
    r.add_post("/api/reply", reply)
    r.add_post("/api/tx", tx_enable)
    r.add_post("/api/halt", halt)
    r.add_post("/api/log", log_post)
    r.add_post("/api/log/dismiss", log_dismiss)
    r.add_get("/api/shack/busy", busy)
    r.add_post("/api/shack/release", release)
    r.add_post("/api/shack/pad/action", pad_action)
    r.add_get("/api/shack/pad/faces", pad_faces)
    r.add_static("/static/", STATIC, append_version=False)
    if start:
        async def on_start(_w):
            await core.start()

        async def on_stop(_w):
            await core.stop()
        w.on_startup.append(on_start)
        w.on_cleanup.append(on_stop)
    return w


def main() -> int:
    inst = Installation()
    logging.basicConfig(level=getattr(logging, inst.log_level, logging.INFO),
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    host = "0.0.0.0" if P.platform_mode() else "127.0.0.1"
    web.run_app(build_app(App(inst)), host=host, port=inst.port, access_log=None, print=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
