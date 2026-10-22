"""Transmit: every refusal before a key-up, and a real transmission into the fake.

Nothing here can key a radio: the only radio app these tests reach is
tools/fakeradio.py on a loopback socket.
"""

import array
import asyncio
import io
import time
import wave

import aiohttp

from app import engine
from app.audio import RadioAppSource, SoundCardSource
from app.radioapp import RadioApp
from app.tx import Transmitter
from helpers import FULL_GB, fake_radio, station

MSG = "DL1ABC GM4BRI IO75"


def tx_for(st, allow=True):
    return Transmitter(station=st, decoder=engine.Decoder(1), allow=allow)


async def _send(fake_kw=None, profile=FULL_GB, allow=True, dial=14074000, audio=1500.0, msg=MSG, config=None):
    async with fake_radio(**(fake_kw or {})) as (fake, url):
        if config:
            async with aiohttp.ClientSession() as s:
                await s.post(url + "fake/config", json=config)
        radio = RadioApp(url, client_id="digimodes-test")
        t = tx_for(station(profile), allow)
        why = await t.send(RadioAppSource(radio), "ft8", dial, audio, msg, slot_start=time.time() - 0.5,
                           realtime=False)
        async with aiohttp.ClientSession() as s:
            async with s.get(url + "fake/tx") as r:
                report = await r.json()
            async with s.get(url + "fake/tx.wav") as r:
                wav = await r.read()
        await radio.close()
        return why, report, wav


def _decode_wav(raw: bytes) -> list[str]:
    with wave.open(io.BytesIO(raw)) as w:
        rate = w.getframerate()
        a = array.array("h")
        a.frombytes(w.readframes(w.getnframes()))
    a = engine.Resampler(rate)(a)
    rows = engine.Decoder(1).decode(engine.Slot("ft8", 0, 0.0, array.array("h", [0] * 6000) + a))
    return [r["message"] for r in rows]


def test_a_transmission_goes_out_under_the_lease_and_decodes():
    why, report, wav = asyncio.run(_send())
    assert why == ""
    kinds = [e["event"] for e in report["events"]]
    assert kinds == ["key", "unkey"] and report["events"][0]["remote"] == "digimodes-test"
    assert report["keyed"] is False
    # 12.64 s of FT8 at 12 kHz, all of it while keyed.
    assert abs(report["samples"] - int(12.64 * 12000)) < 2000
    assert _decode_wav(wav) == [MSG]


def test_the_waveform_is_made_at_the_rate_the_radio_app_asks_for():
    why, report, wav = asyncio.run(_send(config={"rate": 48000}))
    assert why == "" and report["rate"] == 48000
    assert abs(report["samples"] - int(12.64 * 48000)) < 8000
    assert _decode_wav(wav) == [MSG]


def test_refused_with_the_switch_off_and_nothing_keyed():
    why, report, _ = asyncio.run(_send(allow=False))
    assert "switched off" in why and report["events"] == []


def test_refused_without_a_callsign():
    why, report, _ = asyncio.run(_send(profile={"locator": "IO75RL"}))
    assert "callsign" in why and report["events"] == []


def test_the_licence_check_is_the_datasets_at_dial_plus_audio():
    # A Foundation licence has no 60 m: the dataset says so, not this app.
    found = {**FULL_GB, "licence": {"class": "foundation"}}
    why, report, _ = asyncio.run(_send(profile=found, dial=5357000))
    assert "60 m" in why and report["events"] == []
    # The same dial is fine on 20 m, where the audio offset puts the signal.
    st = station(found)
    assert st.refusal(14074000, 1500.0, "ft8") is None
    # And a signal pushed past a band edge by its audio offset is refused.
    assert st.refusal(14350000 - 1000, 1500.0, "ft8") is not None


def test_no_licence_class_is_no_transmitting():
    why, report, _ = asyncio.run(_send(profile={"callsign": "GM4BRI", "country": "GB"}))
    assert "licence class" in why and report["events"] == []


def test_the_radio_apps_own_refusal_is_reported_and_nothing_is_sent():
    why, report, _ = asyncio.run(_send(config={"refuse": "outside your licence at 14.350 MHz"}))
    assert why == "outside your licence at 14.350 MHz"
    assert report["samples"] == 0


def test_a_radio_app_without_the_lease_is_never_keyed():
    why, report, _ = asyncio.run(_send(config={"noLease": True}))
    assert "lease" in why and report["events"] == []


def test_a_sound_card_cannot_transmit():
    t = tx_for(station(FULL_GB))
    assert "receive only" in t.refusal(SoundCardSource(), "ft8", 14074000, 1500.0, MSG)
    assert "receive only" in t.refusal(None, "ft8", 14074000, 1500.0, MSG)


def test_a_message_ohsdigi_will_not_send_is_refused():
    t = tx_for(station(FULL_GB))

    class Src:
        radio = object()
    assert "cannot be sent" in t.refusal(Src(), "ft8", 14074000, 1500.0, "THIS IS FAR TOO LONG FOR FT8 MESSAGES")


def test_losing_the_lease_stops_the_heartbeat_and_the_audio():
    async def run():
        async with fake_radio() as (fake, url):
            radio = RadioApp(url, client_id="digimodes-test")
            await radio.key()
            assert radio.keyed
            # Somebody else unkeys (any unkey ends the lease).
            async with aiohttp.ClientSession() as s:
                await s.post(url + "api/ptt", json={"on": False})
            await asyncio.sleep(1.3)
            assert radio.keyed is False
            try:
                await radio.play(b"\0\0" * 24000, realtime=False)
                raised = False
            except Exception:       # noqa: BLE001 - RadioRefused
                raised = True
            await radio.close()
            return raised, fake.events
    raised, events = asyncio.run(run())
    assert raised
    assert "audio-while-unkeyed" not in [e["event"] for e in events]


def test_halt_unkeys_mid_transmission():
    async def run():
        async with fake_radio() as (fake, url):
            radio = RadioApp(url, client_id="digimodes-test")
            t = tx_for(station(FULL_GB))
            t.start(RadioAppSource(radio), "ft8", 14074000, 1500.0, MSG, time.time() - 0.5, realtime=True)
            await asyncio.sleep(1.0)
            assert fake.keyed()
            await t.halt()
            keyed_after = fake.keyed()
            await radio.close()
            return keyed_after, [e["event"] for e in fake.events], len(fake.tx)
    keyed_after, kinds, samples = asyncio.run(run())
    assert keyed_after is False and kinds == ["key", "unkey"]
    assert 0 < samples < 12000 * 3
