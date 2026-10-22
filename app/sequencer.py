"""The QSO sequencer: WSJT-X's basic flow, as a plain state machine.

No clock, no audio and no I/O in here, so every step of a contact is tested
by handing it decodes and asking what it would send (tests/test_sequencer.py).
The app feeds it each slot's decodes (`heard`) and asks, at each slot
boundary, what to transmit (`next_message`); the transmitter does the rest.

What it does, and what WSJT-X does that it does not:

- **Call CQ** (`call_cq`): send Tx6 every other slot until somebody answers;
  the first station heard calling us is worked (WSJT-X answers the first
  caller too unless the operator picks another, which `reply` lets them do).
- **Reply** (`reply`): pick a decode. A CQ is answered with Tx1; a message
  to us continues from whatever step it is (WSJT-X's double-click). We
  transmit in the other half of the minute from the station we answer.
- **Advance on what we hear**: grid -> report (Tx2), report -> R+report
  (Tx3), R+report -> RR73 (Tx4), RR73/RRR -> 73 (Tx5), 73 -> done. A
  message not from our partner, or not to us, changes nothing.
- **The contact is complete** when we send RR73 or 73, or hear RR73 or 73
  from the partner - WSJT-X's "prompt to log" moment. `complete` then holds
  the contact for logging, once.
- **Stopping**: a contact with no answer is abandoned after `max_repeats`
  sends of the same message, and every transmission stops after
  `watchdog_slots` slots with no operator action (WSJT-X's Tx watchdog,
  there in minutes). `halt` stops at once. Fox/Hound, contest exchanges,
  auto-CQ after a contact and the Tx5 "free text" are not here (beta scope).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import messages as M


@dataclass
class Contact:
    call: str
    grid: str = ""
    rst_sent: str = ""
    rst_rcvd: str = ""
    start: float = 0.0          # epoch seconds of the first message exchanged
    freq_audio: float = 0.0

    def public(self) -> dict:
        return {"call": self.call, "grid": self.grid, "rstSent": self.rst_sent,
                "rstRcvd": self.rst_rcvd, "start": self.start}


@dataclass
class Sequencer:
    mycall: str = ""
    mygrid: str = ""
    max_repeats: int = 6
    watchdog_slots: int = 12        # our transmit slots: 6 minutes of FT8, as WSJT-X
    #: What is being sent: 1-6 (WSJT-X's Tx numbers) or None.
    step: int | None = None
    #: Which half of the cycle we transmit in: slot index % 2.
    parity: int = 0
    enabled: bool = False
    contact: Contact | None = None
    complete: Contact | None = None
    repeats: int = 0
    idle_slots: int = 0
    last_sent: str = ""
    #: Why it last stopped by itself, for the page ("" while running).
    stopped: str = ""
    _logged: set = field(default_factory=set)

    # -- the operator --------------------------------------------------------

    def call_cq(self, parity: int) -> None:
        self.contact, self.step, self.parity = None, 6, parity % 2
        self._start()

    def reply(self, decode: dict, now: float) -> str:
        """Answer one decode. '' when a contact was started, else why not."""
        p = M.parse(decode.get("message", ""))
        my = self.mycall.upper()
        if not my:
            return "nocall"
        if p.kind == "cq" and p.de and p.de != my:
            self.contact = Contact(p.de, p.grid, M.report(decode.get("snr", -10)), "", now,
                                   float(decode.get("freq") or 0))
            self.step = 1
        elif p.to == my and p.de:
            self.contact = Contact(p.de, p.grid, M.report(decode.get("snr", -10)), p.report, now,
                                   float(decode.get("freq") or 0))
            self.step = self._answer(p)
            if self.step is None:
                return "finished"
        else:
            return "unanswerable"
        self.parity = (int(decode.get("parity", 0)) + 1) % 2
        self._start()
        return ""

    def halt(self) -> None:
        self.enabled = False

    def resume(self) -> None:
        """Enable TX again on the step it stopped at (WSJT-X's Enable Tx)."""
        if self.step is not None:
            self._start()

    def _start(self) -> None:
        self.enabled, self.repeats, self.idle_slots, self.stopped = True, 0, 0, ""

    # -- the radio -----------------------------------------------------------

    def _answer(self, p: M.Parsed) -> int | None:
        """The step that answers this message from our partner."""
        return {"grid": 2, "report": 3, "rreport": 4, "rr73": 5, "73": None}.get(p.kind, self.step)

    def heard(self, decodes: list[dict], now: float) -> None:
        """One slot's decodes. Moves the contact on if our partner spoke."""
        my = self.mycall.upper()
        if not my or not self.enabled:
            return
        ours = [d for d in decodes if M.parse(d.get("message", "")).to == my]
        if self.step == 6 and self.contact is None:
            # Calling CQ: take the strongest caller (WSJT-X takes the first
            # in its list, which is sorted by strength in effect).
            callers = sorted(ours, key=lambda d: -float(d.get("snr", -99)))
            for d in callers:
                p = M.parse(d["message"])
                if p.kind in ("grid", "report"):
                    self.contact = Contact(p.de, p.grid, M.report(d.get("snr", -10)), p.report, now,
                                           float(d.get("freq") or 0))
                    self.step, self.repeats = self._answer(p), 0
                    return
            return
        if self.contact is None:
            return
        for d in ours:
            p = M.parse(d["message"])
            if p.de != self.contact.call:
                continue
            if p.grid:
                self.contact.grid = p.grid
            if p.report:
                self.contact.rst_rcvd = p.report
            nxt = self._answer(p)
            if p.kind in ("rr73", "73"):
                self._finish()
            if nxt is None:
                self.enabled, self.step = False, None
            elif nxt != self.step:
                self.step, self.repeats = nxt, 0
            return

    def next_message(self, slot_index: int) -> str | None:
        """What to send in this slot, or None. Counts the send."""
        if not self.enabled or self.step is None or slot_index % 2 != self.parity:
            return None
        self.idle_slots += 1
        if self.idle_slots > self.watchdog_slots:
            self.enabled, self.stopped = False, "watchdog"
            return None
        if self.step != 6:
            self.repeats += 1
            if self.repeats > self.max_repeats:
                self.enabled, self.stopped = False, "noreply"
                return None
        c = self.contact
        text = M.tx_messages(self.mycall, self.mygrid, c.call if c else "", c.rst_sent if c else "")[self.step]
        self.last_sent = text
        if self.step in (4, 5):
            self._finish()
        if self.step == 5:
            # 73 is sent once (WSJT-X disables Tx after it).
            self.enabled = False
        return text

    def _finish(self) -> None:
        c = self.contact
        if c is None:
            return
        key = (c.call, c.start)
        if key not in self._logged:
            self._logged.add(key)
            self.complete = Contact(**vars(c))

    def take_complete(self) -> Contact | None:
        c, self.complete = self.complete, None
        return c

    def public(self) -> dict:
        c = self.contact
        return {"enabled": self.enabled, "step": self.step, "parity": self.parity,
                "contact": c.public() if c else None, "repeats": self.repeats,
                "lastSent": self.last_sent, "stopped": self.stopped,
                "messages": {str(k): v for k, v in M.tx_messages(
                    self.mycall, self.mygrid, c.call if c else "", c.rst_sent if c else "").items()}
                if self.mycall else {}}
