"""The server: proxy auth and levels on the platform, and a whole contact
through the HTTP API from decode to logbook."""

import array
import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import aiohttp
import ohsdigi
from aiohttp.test_utils import TestServer

from app import engine, main
from app.platform import PlatformClient
from app.settings import Installation
from helpers import FULL_GB, ROOT

SECRET = "test-proxy-secret"
T0 = 1_800_000_000.0


def installation(**env):
    base = {"DIGIMODES_DATA_DIR": tempfile.mkdtemp(prefix="digimodes-web-")}
    base.update(env)
    return Installation(base)


class Recorder:
    """A pretend controller's services: what this app posted to them."""

    def __init__(self, profile=None):
        self.calls = []
        self.profile = profile

    async def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path == "/profile":
            return (200, self.profile) if self.profile else (503, {})
        return 201, {}


def run(coro):
    return asyncio.run(coro)


async def serve(app):
    server = TestServer(main.build_app(app, start=False))
    await server.start_server()
    return server


def headers(level, user="brian"):
    return {"X-OpenHamShack-Proxy-Secret": SECRET, "X-OpenHamShack-User": user,
            "X-OpenHamShack-Level": level, "X-OpenHamShack-Device": "dev_TEST"}


def platform_app(monkeypatch, recorder=None, **env):
    monkeypatch.setenv("SHACK_PLATFORM", "dock360")
    monkeypatch.setenv("DOCK360_SHACK_PROXY_SECRET", SECRET)
    return main.App(installation(**env), platform=PlatformClient(recorder or Recorder()))


def test_on_the_platform_nothing_is_served_without_the_proxy_secret(monkeypatch):
    app = platform_app(monkeypatch)

    async def go():
        server = await serve(app)
        out = {}
        async with aiohttp.ClientSession() as s:
            for name, path, hdrs in (("none", "/api/state", {}), ("page", "/", {}),
                                     ("wrong", "/api/state", {**headers("transmit"),
                                                              "X-OpenHamShack-Proxy-Secret": "nope"}),
                                     ("health", "/healthz", {}), ("view", "/api/state", headers("view"))):
                async with s.get(server.make_url(path), headers=hdrs) as r:
                    out[name] = (r.status, r.content_type, await r.json(content_type=None) if
                                 r.content_type == "application/json" else None)
        await server.close()
        return out
    out = run(go())
    assert out["none"][0] == 401 and out["none"][2]["code"] == "proxy_required"
    assert out["page"][0] == 401 and out["wrong"][0] == 401
    assert out["health"][0] == 200
    status, ctype, body = out["view"]
    assert status == 200 and ctype == "application/json"
    assert body["platform"] is True and body["mode"] == "ft8" and isinstance(body["decodes"], list)


def test_levels_view_cannot_control_and_control_cannot_transmit(monkeypatch):
    app = platform_app(monkeypatch)

    async def go():
        server = await serve(app)
        res = {}
        async with aiohttp.ClientSession() as s:
            for name, level, path, body in (
                    ("view-config", "view", "/api/config", {"rxHz": 1000}),
                    ("control-config", "control", "/api/config", {"rxHz": 1000}),
                    ("control-cq", "control", "/api/cq", {}),
                    ("control-tx", "control", "/api/tx", {"on": True}),
                    ("control-halt", "control", "/api/halt", {}),
                    ("transmit-cq", "transmit", "/api/cq", {}),
                    ("view-release", "view", "/api/shack/release", {})):
                async with s.post(server.make_url(path), json=body, headers=headers(level)) as r:
                    res[name] = (r.status, await r.json())
        await server.close()
        return res
    r = run(go())
    assert r["view-config"][0] == 403 and r["view-config"][1]["code"] == "level_too_low"
    assert r["control-config"][0] == 200 and app.ops.value.rx_hz == 1000
    assert r["control-cq"][0] == 403 and r["control-tx"][0] == 403
    assert r["control-halt"][0] == 200
    # Transmit level, but this app's switch is off: refused, with the reason.
    assert r["transmit-cq"][0] == 409 and "switched off" in r["transmit-cq"][1]["error"]
    # The broker's release request is the platform's own call.
    assert r["view-release"][0] == 200


def test_the_page_is_served_under_the_front_doors_prefix(monkeypatch):
    app = platform_app(monkeypatch)

    async def go():
        server = await serve(app)
        async with aiohttp.ClientSession() as s:
            async with s.get(server.make_url("/"), headers={**headers("view"),
                                                            "X-Forwarded-Prefix": "/shack-digimodes"}) as r:
                html = await r.text()
            async with s.get(server.make_url("/static/i18n/en.json"), headers=headers("view")) as r:
                cat = await r.json(content_type=None)
        await server.close()
        return html, cat
    html, cat = run(go())
    assert '<base href="/shack-digimodes/">' in html
    assert cat["app"]["title"] == "OpenHamShack Digital Modes"


def _slot_with(messages, index, mode="ft8"):
    n = int(engine.RATE * engine.PERIOD[mode])
    acc = array.array("h", [0] * n)
    for k, (msg, freq) in enumerate(messages):
        w = array.array("f")
        w.frombytes(ohsdigi.encode_audio(mode, msg, freq=freq))
        for i, s in enumerate(w):
            if 6000 + i < n:
                acc[6000 + i] = max(-32767, min(32767, acc[6000 + i] + int(s * 6000)))
    return engine.Slot(mode, index, index * engine.PERIOD[mode], acc)


def test_a_whole_contact_from_decode_to_the_logbook(monkeypatch):
    rec = Recorder(profile=FULL_GB)
    app = platform_app(monkeypatch, rec, ALLOW_TRANSMIT="1")
    idx = int(T0 // 15)

    async def go():
        await app.station.refresh(force=True)
        server = await serve(app)
        async with aiohttp.ClientSession() as s:
            async def post(path, body, level="transmit"):
                async with s.post(server.make_url(path), json=body, headers=headers(level)) as r:
                    return r.status, await r.json()

            async def get(path):
                async with s.get(server.make_url(path), headers=headers("view")) as r:
                    return await r.json()

            await app.on_slot(_slot_with([("CQ DL1ABC JO62", 1200.0), ("CQ F4XYZ JN18", 1800.0)], idx))
            st = await get("/api/state?since=0")
            cq = next(d for d in st["decodes"] if d["message"] == "CQ DL1ABC JO62")
            assert cq["cq"] and cq["parity"] == 0
            # No radio app: replying is refused before anything starts.
            refused = await post("/api/reply", {"id": cq["id"]})
            app.source = type("Src", (), {"radio": object(), "kind": "radio", "describe": lambda self: "x"})()
            ok = await post("/api/reply", {"id": cq["id"]})
            seq_state = (await get("/api/state"))["sequencer"]
            # Tx1 goes in the odd slot; the slot loop would send this:
            first = app.sequencer.next_message(idx + 1)
            await app.on_slot(_slot_with([("GM4BRI DL1ABC -08", 1200.0)], idx + 2))
            second = app.sequencer.next_message(idx + 3)
            await app.on_slot(_slot_with([("GM4BRI DL1ABC RR73", 1200.0)], idx + 4))
            third = app.sequencer.next_message(idx + 5)
            pending = (await get("/api/state"))["pending"]
            view_log = await post("/api/log", {}, level="view")
            logged = await post("/api/log", {"comment": "first FT8 contact"}, level="control")
            again = await post("/api/log", {}, level="control")
            qsos = (await get("/api/log"))["qsos"]
        await server.close()
        return refused, ok, seq_state, (first, second, third), pending, view_log, logged, again, qsos
    refused, ok, seq_state, sent, pending, view_log, logged, again, qsos = run(go())
    assert refused[0] == 409 and "radio app" in refused[1]["error"]
    assert ok[0] == 200 and seq_state["contact"]["call"] == "DL1ABC" and seq_state["parity"] == 1
    # Our report is their SNR as decoded (a clean synthetic signal: strong).
    assert sent == ("DL1ABC GM4BRI IO75", f"DL1ABC GM4BRI R{pending['rstSent']}", "DL1ABC GM4BRI 73")
    assert pending["call"] == "DL1ABC" and pending["grid"] == "JO62" and pending["rstRcvd"] == "-08"
    assert view_log[0] == 403
    assert logged[0] == 200 and logged[1]["sent"] is True
    f = logged[1]["fields"]
    assert (f["call"], f["mode"], f["band"], f["gridsquare"], f["rst_rcvd"], f["station_callsign"]) == (
        "DL1ABC", "FT8", "20m", "JO62", "-08", "GM4BRI")
    assert f["freq_hz"] == 14074000 + 1500 and f["comment"] == "first FT8 contact"
    assert again[0] == 409
    assert [q["call"] for q in qsos] == ["DL1ABC"]
    # The controller got the contact (idempotent by uuid) and the spots.
    posts = [(m, p) for m, p, _b in rec.calls if m == "POST"]
    assert ("POST", "/logbook/qsos") in posts
    qso_body = next(b for m, p, b in rec.calls if p == "/logbook/qsos")
    assert qso_body["uuid"] == logged[1]["uuid"] and qso_body["fields"]["call"] == "DL1ABC"


def test_a_contact_logged_while_the_controller_is_away_is_sent_later(monkeypatch):
    class Away(Recorder):
        up = False

        async def __call__(self, method, path, body=None):
            if not self.up and path == "/logbook/qsos":
                raise OSError("connection refused")
            return await super().__call__(method, path, body)
    rec = Away(profile=FULL_GB)
    app = platform_app(monkeypatch, rec)

    async def go():
        await app.station.refresh(force=True)
        from app.sequencer import Contact
        app.pending = Contact("DL1ABC", "JO62", "-10", "-12", T0)
        status, body = await app.log_pending({})
        assert status == 200 and body["sent"] is False
        rec.up = True
        await app.station.refresh(force=True)
        return body["uuid"]
    uid = run(go())
    assert [b["uuid"] for m, p, b in rec.calls if p == "/logbook/qsos"] == [uid]
    assert app.station._unsent == []


def test_standalone_without_a_controller_logs_locally_and_edits_the_profile():
    app = main.App(installation())

    async def go():
        server = await serve(app)
        async with aiohttp.ClientSession() as s:
            async with s.post(server.make_url("/api/profile"), json=FULL_GB) as r:
                prof = await r.json()
            async with s.post(server.make_url("/api/config"), json={"source": "soundcard"}) as r:
                sc = r.status, await r.json()
        await server.close()
        return prof, sc
    prof, sc = run(go())
    assert prof["station"]["callsign"] == "GM4BRI" and prof["station"]["editable"] is True
    # A sound card is a desktop-form source only.
    assert sc[0] == 400


def test_the_frequency_list_comes_from_the_dataset_and_is_judged_by_it():
    app = main.App(installation())
    app.station.update({**FULL_GB, "licence": {"class": "foundation"}})
    rows = app.station.frequencies()
    ft8 = {r["band"]: r for r in rows if r["mode"] == "ft8"}
    assert ft8["20m"]["dialHz"] == 14074000 and ft8["20m"]["tx"] is True
    assert ft8["60m"]["tx"] is False and "Foundation" in ft8["60m"]["why"]
    assert {r["mode"] for r in rows} == {"ft8", "ft4"}


def test_the_pad_declarations_are_valid_and_the_default_layout_resolves():
    from shack_pad import actions, mapping
    from shack_pad.drivers.ajazz import AKP03E
    from shack_pad.drivers.keypad import Keypad
    decl = json.loads((ROOT / "store" / "pad-actions.json").read_text())
    actions.validate(decl)
    acts, notes = actions.load(decl, kinds=["digimodes", "flex"])
    assert notes == [] and not actions.tx_actions(acts)       # nothing on the pad keys
    layout = json.loads((ROOT / "store" / "pad-layout.json").read_text())
    for drv in (AKP03E, Keypad):
        pages, notes = mapping.resolve(drv, layout, None, acts)
        assert notes == []
    pages, _ = mapping.resolve(AKP03E, layout, None, acts)
    assert pages[0]["K1"]["press"] == "halt" and pages[0]["D2"]["turn"] == "tune"


def test_pad_actions_do_what_they_say():
    app = main.App(installation())
    app.station.update(FULL_GB)

    async def go():
        out = {}
        out["tune"] = await app.pad.run("tune", "turn", 3)
        out["rx"] = app.ops.value.rx_hz
        out["down"] = await app.pad.run("tune:down", "press", 1)
        out["rx2"] = app.ops.value.rx_hz
        out["tx2rx"] = await app.pad.run("tx-to-rx", "press", 1)
        out["band"] = await app.pad.run("band:next", "press", 1)
        out["ptt"] = await app.pad.run("ptt", "press", 1)
        out["log"] = await app.pad.run("log", "press", 1)
        out["mode"] = await app.pad.run("mode:next", "press", 1)
        return out
    o = run(go())
    assert o["rx"] == 1530 and o["rx2"] == 1520 and app.ops.value.tx_hz == 1520
    assert app.ops.value.mode == "ft4"
    assert o["ptt"][0] == 400 and o["log"][0] == 409
    faces = app.pad.faces()["faces"]
    assert faces["mode:next"]["l1"] == "FT4" and faces["log"]["tone"] == "disabled"


def test_no_user_visible_string_is_outside_the_catalogs():
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "i18n.py"), "check"], capture_output=True, text=True,
                       cwd=ROOT)
    assert r.returncode == 0, r.stdout + r.stderr
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "i18n.py"), "extract", "--check"],
                       capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stdout + r.stderr


def test_the_desktops_own_transmit_switch_counts_only_in_the_desktop(monkeypatch):
    desk = main.App(installation(SHACK_FORM="desktop"))
    server_app = main.App(installation())

    async def go():
        await desk.configure({"allowTransmit": True})
        await server_app.configure({"allowTransmit": True})
    run(go())
    assert desk.transmit_allowed() is True
    # A server's switch is the administrator's (ALLOW_TRANSMIT), never the page's.
    assert server_app.transmit_allowed() is False and server_app.ops.value.allow_transmit is False
