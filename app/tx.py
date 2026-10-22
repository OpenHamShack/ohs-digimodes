"""One transmission: every check, the lease, the audio, the unkey.

What is checked before anything is keyed, in this order, and each refusal
is a sentence the page shows (CONTROLLER-CONTRACT 6.2 for the family's
order; the level is the proxy's and app/platform.py's, before a request
that starts a sequence gets here):

1. this app's transmit switch (`ALLOW_TRANSMIT`, off by default);
2. the audio goes to a radio app - the only source with a lease. A sound
   card or a file is receive only (audio.py says why);
3. a callsign, and shack_bandplan's yes for the signal where it will really
   be: the dial plus the transmit audio offset, as wide as the mode;
4. the message is one ohsdigi will encode, and as written (it cuts free
   text to 13 characters, as WSJT-X does, rather than refusing it);
5. the radio app's own lease answers `onAir: true` - where its level, its
   own switch, its licence check at its real frequency and the controller's
   broker all get their say.

**The dial is this app's, and the radio app checks its own.** No family
contract yet lets this app read or set the radio's frequency, so step 3
judges the dial chosen here, and step 5 is where the radio app judges the
frequency it is really on (Icom and Flex both check the licence at key-up).
A dial chosen here that the radio is not on cannot get past the radio's own
check; it can only make this one stricter than it needs to be.

The transmission is the slot's message, started half a second into the
slot as WSJT-X does, and never longer than the slot. Whatever ends it - the
end of the audio, `halt`, an error, the lease lost - the unkey runs.
"""

from __future__ import annotations

import asyncio
import logging
import time

from . import engine
from .i18n import _
from .radioapp import RadioRefused

log = logging.getLogger("tx")

#: Seconds into the slot the audio starts (WSJT-X's TxDelay is 0.2 s of
#: keyed silence before the 0.5 s point; the same here).
START_S = 0.5
KEY_LEAD_S = 0.2


class Transmitter:
    def __init__(self, *, station, decoder, allow, clock=time.time, sleep=asyncio.sleep, gain: float = 0.5):
        self.station = station
        self.decoder = decoder
        self.allow = allow
        self.clock = clock
        self.sleep = sleep
        self.gain = gain
        self.sending: dict | None = None
        self.last_error = ""
        self._task: asyncio.Task | None = None
        self._radio = None

    def refusal(self, source, mode: str, dial_hz: int | None, audio_hz: float, message: str) -> str | None:
        if not (self.allow() if callable(self.allow) else self.allow):
            return _("transmitting is switched off in this app's settings (Allow transmitting)")
        radio = getattr(source, "radio", None)
        if source is None or radio is None:
            return _("only a radio app can transmit: a sound card or a file is receive only here")
        why = self.station.refusal(dial_hz, audio_hz, mode)
        if why:
            return why
        if self.decoder is None:
            return _("the FT8/FT4 encoder is not available on this machine")
        try:
            sent = self.decoder.sent(mode, message)
        except Exception as e:      # noqa: BLE001 - ohsdigi.BadMessage and friends
            return _("{message} cannot be sent: {why}").format(message=message, why=str(e))
        if " ".join(sent.upper().split()) != " ".join(message.upper().split()):
            # Sending something other than what the page shows is worse
            # than not sending: the operator would log a contact on words
            # that never went out.
            return _("{message} cannot be sent as written: it would go out as {sent}").format(
                message=message, sent=sent)
        return None

    async def send(self, source, mode: str, dial_hz: int | None, audio_hz: float, message: str,
                   slot_start: float, realtime: bool = True) -> str:
        """Transmit `message` in the slot starting at `slot_start`.
        '' when it went out, else why not."""
        why = self.refusal(source, mode, dial_hz, audio_hz, message)
        if why:
            self.last_error = why
            return why
        radio = source.radio
        self._radio = radio
        period = engine.PERIOD[mode]
        self.sending = {"message": message, "slot": slot_start, "mode": mode, "audioHz": audio_hz}
        try:
            wait = slot_start + START_S - KEY_LEAD_S - self.clock()
            if wait > 0:
                await self.sleep(wait)
            if self.clock() > slot_start + period - 1.0:
                return self._fail(_("too late in the slot to transmit"))
            await radio.key()
            wait = slot_start + START_S - self.clock()
            if wait > 0:
                await self.sleep(wait)
            await asyncio.wait_for(
                radio.play(b"", rate_of=lambda rate: engine.float_to_s16(
                    self.decoder.encode(mode, message, audio_hz, rate), self.gain), realtime=realtime),
                timeout=period)
            self.last_error = ""
            return ""
        except RadioRefused as e:
            return self._fail(str(e))
        except asyncio.TimeoutError:
            return self._fail(_("the transmission ran past the end of its slot and was stopped"))
        finally:
            await radio.unkey()
            self.sending = None
            self._radio = None

    def _fail(self, why: str) -> str:
        self.last_error = why
        log.warning("not transmitted: %s", why)
        return why

    def start(self, *args, **kw) -> asyncio.Task:
        self._task = asyncio.ensure_future(self.send(*args, **kw))
        return self._task

    async def halt(self) -> None:
        """Stop now. Never refused; the unkey runs in send()'s finally."""
        t = self._task
        if t is not None and not t.done():
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):     # noqa: BLE001 - stopping is all that matters
                pass
        if self._radio is not None:
            await self._radio.unkey()

    @property
    def busy(self) -> bool:
        return self._task is not None and not self._task.done()
