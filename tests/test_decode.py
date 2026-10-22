"""Receive: a recorded slot through the file source, the slot clock and ohsdigi."""

import array
import asyncio

import ohsdigi

from app import audio, engine
from conftest import VECTORS

T0 = 1_800_000_000.0        # a slot boundary for both modes (divisible by 15)


def _decode_file(path, start=T0, depth=1):
    async def run():
        src = audio.WavSource(path, start=start)
        buf = engine.SlotBuffer("ft8")
        dec = engine.Decoder(depth)
        out = []
        async for chunk, t_end in src:
            for slot in buf.feed(chunk, t_end):
                out.append((slot, dec.decode(slot)))
        return out
    return asyncio.run(run())


def test_the_recorded_ft8_slot_decodes_all_three_messages():
    got = _decode_file(VECTORS / "ft8-three-signals.wav")
    assert len(got) == 1
    slot, rows = got[0]
    assert slot.start == T0 and slot.index % 2 == 0
    by_freq = {r["freq"]: r for r in rows}
    assert [by_freq[f]["message"] for f in (800, 1500, 2200)] == [
        "CQ DL1ABC JO62", "GM4BRI F4XYZ JN18", "CQ DX EA8AB IL18"]
    # A signal sent on time decodes with DT near zero; the weakest is weak.
    assert all(abs(r["dt"]) <= 0.2 for r in rows)
    assert by_freq[2200]["snr"] < -12 < by_freq[800]["snr"]
    assert rows[0]["utc"] == "080000"


def test_a_late_start_places_audio_by_its_clock_not_by_arrival():
    # The same file started 1 s late: the signals are 1 s late in the slot
    # (a stall upstream would do the same), so DT reads 1 s rather than 0.
    got = _decode_file(VECTORS / "ft8-three-signals.wav", start=T0 + 1.0)
    rows = got[0][1]
    assert rows and all(0.8 <= r["dt"] <= 1.2 for r in rows)


def test_a_gap_in_the_source_is_silence_in_the_right_place():
    buf = engine.SlotBuffer("ft8")
    one = array.array("h", [100] * 1200)                    # 0.1 s
    assert buf.feed(one, T0 + 0.1) == []
    # Nothing for 13.9 s, then the audio resumes: the slot is complete at
    # 14.0 s and its samples between are zeros, not the next audio slid up.
    slots = buf.feed(array.array("h", [200] * 1200), T0 + 14.1)
    assert len(slots) == 1
    a = slots[0].audio
    assert a[0] == 100 and a[1199] == 100 and a[1200] == 0 and a[engine.RATE * 13] == 0
    assert a[engine.RATE * 14 + 10] == 200


def test_a_slot_joined_part_way_through_is_not_decoded():
    buf = engine.SlotBuffer("ft8")
    # Started 9 s into a slot: too little of it to be worth decoding.
    assert buf.feed(array.array("h", [0] * engine.RATE * 6), T0 + 15.0) == []
    # The next slot is whole and comes out.
    out = buf.feed(array.array("h", [0] * engine.RATE * 14), T0 + 29.0)
    assert [s.start for s in out] == [T0 + 15.0]


def test_ft4_on_its_75_second_cycle():
    msg = "CQ GM4BRI IO75"
    w = array.array("f")
    w.frombytes(ohsdigi.encode_audio("ft4", msg, freq=1000.0))
    n = int(engine.RATE * 7.5)
    pcm = array.array("h", [0] * n)
    for i, s in enumerate(w):
        if 6000 + i < n:
            pcm[6000 + i] = int(s * 8000)
    buf = engine.SlotBuffer("ft4")
    # Two slots' worth, 7.5 s apart; only the second carries the signal.
    first = buf.feed(array.array("h", [0] * n), T0 + 7.5)
    second = buf.feed(pcm, T0 + 15.0)
    assert [s.start for s in first + second] == [T0, T0 + 7.5]
    rows = engine.Decoder(1).decode(second[0])
    assert [r["message"] for r in rows] == [msg] and rows[0]["parity"] == 1


def test_the_resampler_keeps_a_tone_and_its_rate():
    import math
    src = array.array("h", (int(8000 * math.sin(2 * math.pi * 1000 * i / 48000)) for i in range(48000)))
    r = engine.Resampler(48000)
    out = array.array("h")
    for i in range(0, len(src), 960):            # 20 ms chunks, as a sound card gives
        out.extend(r(src[i:i + 960]))
    assert abs(len(out) - 12000) <= 2
    # Zero crossings of 1000 Hz over a second: 2000, whatever the chunking.
    crossings = sum(1 for a, b in zip(out, out[1:]) if (a < 0) != (b < 0))
    assert 1990 <= crossings <= 2010


def test_encode_then_decode_round_trip_at_the_transmit_level():
    dec = engine.Decoder(1)
    pcm = engine.float_to_s16(dec.encode("ft8", "DL1ABC GM4BRI IO75", 1234.0), gain=0.5)
    a = array.array("h")
    a.frombytes(pcm)
    assert max(abs(s) for s in a) <= 16384          # half scale: never clips the radio's codec
    slot_audio = array.array("h", [0] * 6000) + a
    rows = dec.decode(engine.Slot("ft8", 0, 0.0, slot_audio))
    assert [(r["message"], r["freq"]) for r in rows] == [("DL1ABC GM4BRI IO75", 1234)]


def test_arrival_jitter_does_not_reach_the_slot():
    import random
    rate, raw = audio.read_wav(VECTORS / "ft8-three-signals.wav")
    rnd = random.Random(3)
    clock = audio.StreamClock()
    buf = engine.SlotBuffer("ft8")
    slots = []
    step = 240
    for i in range(0, len(raw), step):
        chunk = raw[i:i + step]
        # Each 20 ms message arrives up to 30 ms late, in no order of lateness.
        arrived = T0 + (i + len(chunk)) / engine.RATE + rnd.uniform(0.0, 0.03)
        slots += buf.feed(chunk, clock.stamp(len(chunk), arrived))
    rows = engine.Decoder(1).decode(slots[0])
    assert sorted(r["message"] for r in rows) == ["CQ DL1ABC JO62", "CQ DX EA8AB IL18", "GM4BRI F4XYZ JN18"]
    assert sorted(r["freq"] for r in rows) == [800, 1500, 2200]


def test_a_real_stall_re_anchors_the_stream_clock():
    clock = audio.StreamClock(slack=0.25)
    t1 = clock.stamp(240, 100.02)
    t2 = clock.stamp(240, 100.04)
    assert abs(t2 - t1 - 0.02) < 1e-9
    # Two seconds of nothing, then audio again: placed by the wall clock.
    assert abs(clock.stamp(240, 102.06) - 102.06) < 1e-9
