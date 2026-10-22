"""The QSO sequencer: WSJT-X's basic flow, step by step, with no audio."""

from app import messages as M
from app.sequencer import Sequencer


def d(message, snr=-10, parity=0, freq=1200):
    return {"message": message, "snr": snr, "parity": parity, "freq": freq}


def seq():
    return Sequencer(mycall="GM4BRI", mygrid="IO75RL")


def test_messages_parse_the_standard_forms():
    assert M.parse("CQ DL1ABC JO62").public() == {"kind": "cq", "to": "", "de": "DL1ABC", "grid": "JO62", "report": ""}
    assert M.parse("CQ DX EA8AB IL18").de == "EA8AB"
    assert M.parse("CQ POTA K1ABC FN42").de == "K1ABC"
    assert M.parse("GM4BRI DL1ABC JO62").kind == "grid"
    assert M.parse("GM4BRI DL1ABC -07").public()["report"] == "-07"
    assert M.parse("GM4BRI DL1ABC R+02").kind == "rreport"
    # RR73 looks like a locator; it is not one.
    assert M.parse("GM4BRI DL1ABC RR73").kind == "rr73"
    assert M.parse("GM4BRI <PJ4/K1ABC> 73").de == "PJ4/K1ABC"
    assert M.parse("TNX BOB 73 GL").kind == "other"
    assert M.report(-35) == "-30" and M.report(3) == "+03"


def test_answering_a_cq_runs_tx1_to_tx5_and_completes_once():
    s = seq()
    assert s.reply(d("CQ DL1ABC JO62", snr=-12, parity=0), now=100.0) == ""
    # We transmit in the other half from the station we answer.
    assert s.parity == 1 and s.next_message(0) is None
    assert s.next_message(1) == "DL1ABC GM4BRI IO75"                   # Tx1
    s.heard([d("GM4BRI DL1ABC -05")], now=115.0)
    assert s.next_message(3) == "DL1ABC GM4BRI R-12"                   # Tx3 (their report in hand)
    assert s.contact.rst_rcvd == "-05" and s.contact.rst_sent == "-12"
    s.heard([d("GM4BRI DL1ABC RR73")], now=130.0)
    assert s.next_message(5) == "DL1ABC GM4BRI 73"                     # Tx5
    done = s.take_complete()
    assert done.call == "DL1ABC" and done.grid == "JO62" and done.rst_rcvd == "-05"
    assert s.take_complete() is None and s.enabled is False
    assert s.next_message(7) is None


def test_calling_cq_works_the_strongest_caller_and_signs_with_rr73():
    s = seq()
    s.call_cq(parity=0)
    assert s.next_message(0) == "CQ GM4BRI IO75"                       # Tx6
    s.heard([d("GM4BRI F4XYZ JN18", snr=-20), d("GM4BRI DL1ABC JO62", snr=-3),
             d("CQ EA8AB IL18", snr=0)], now=15.0)
    assert s.contact.call == "DL1ABC"
    assert s.next_message(2) == "DL1ABC GM4BRI -03"                    # Tx2
    s.heard([d("GM4BRI DL1ABC R-09")], now=45.0)
    assert s.next_message(4) == "DL1ABC GM4BRI RR73"                   # Tx4
    # RR73 sent: the contact is complete (WSJT-X's prompt to log).
    assert s.take_complete().rst_rcvd == "-09"
    # They did not hear the RR73 and send R-09 again: RR73 again, no second log.
    s.heard([d("GM4BRI DL1ABC R-09")], now=75.0)
    assert s.next_message(6) == "DL1ABC GM4BRI RR73" and s.take_complete() is None
    s.heard([d("GM4BRI DL1ABC 73")], now=105.0)
    assert s.enabled is False and s.next_message(8) is None


def test_other_stations_and_other_calls_change_nothing():
    s = seq()
    s.reply(d("CQ DL1ABC JO62", parity=1), now=0.0)
    s.heard([d("GM4BRI F4XYZ -10"), d("M0AAA DL1ABC -10"), d("CQ DL1ABC JO62")], now=15.0)
    assert s.step == 1 and s.contact.call == "DL1ABC"


def test_replying_mid_contact_continues_from_that_step():
    s = seq()
    assert s.reply(d("GM4BRI DL1ABC R-11", parity=0), now=0.0) == ""
    assert s.next_message(1) == "DL1ABC GM4BRI RR73"


def test_no_reply_gives_up_and_the_watchdog_stops_cq():
    s = seq()
    s.max_repeats = 3
    s.reply(d("CQ DL1ABC JO62", parity=0), now=0.0)
    sent = [s.next_message(i) for i in range(1, 12, 2)]
    assert sent[:3] == ["DL1ABC GM4BRI IO75"] * 3 and sent[3:] == [None] * 3
    assert s.stopped == "noreply"
    c = seq()
    c.watchdog_slots = 4
    c.call_cq(parity=0)
    assert [c.next_message(i) is not None for i in range(0, 12, 2)] == [True] * 4 + [False] * 2
    assert c.stopped == "watchdog"


def test_halt_stops_and_resume_carries_on_from_the_same_step():
    s = seq()
    s.call_cq(parity=0)
    s.halt()
    assert s.next_message(0) is None
    s.resume()
    assert s.next_message(2) == "CQ GM4BRI IO75"


def test_without_a_callsign_nothing_is_answered():
    s = Sequencer()
    assert s.reply(d("CQ DL1ABC JO62"), now=0.0) == "nocall"
    assert s.enabled is False
