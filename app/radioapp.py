"""The radio app this app works through: its receive audio, its lease, its
transmit audio.

Digital Modes owns no radio (PLATFORM-PLAN 1: "it works with ANY radio
app"). The app that holds the rig - Icom, Flex, QO-100 NB, Radio - serves
the family's audio contract at its own base (Icom's TV-API 7 and 11 are the
written form):

- `GET api/audio/rx` (websocket): a JSON header with `rate`, then s16le
  mono PCM. `?ack=1` and an `{"ack": bytes}` now and then keep it live
  through proxies (TV-API 7, "Acknowledge what you play").
- `POST api/ptt {"on": true, "remote": <id>}`, heartbeat
  `POST api/ptt/heartbeat` every 1 s, `POST api/ptt {"on": false}`: the
  transmit lease, field for field (CONTROLLER-CONTRACT 6.1). The radio app
  makes its own checks before it keys - its level, its transmit switch, the
  licence at *its* frequency, the broker - and this app never goes round
  them.
- `GET api/audio/tx` (websocket): the radio app's header names the rate it
  wants, and this app sends s16le mono at that rate, only while keyed.

**Refuse a radio app without the lease.** As every family client does: a
status without `tx.remote` is a server from before the lease, and keying it
would leave a transmission nobody's heartbeat holds (GUIDE 2).

Sign-in: a bearer token (`RADIO_TOKEN`: a device token from the
controller, or a Remote-style app token) or the radio app's own password
(`RADIO_PASSWORD`, its form login, as Icom standalone has). One HTTP
session per RadioApp, so the login's cookie is kept.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from urllib.parse import urljoin

import aiohttp

from .i18n import _

log = logging.getLogger("radioapp")

HEARTBEAT_S = 1.0


class RadioRefused(Exception):
    """The radio app said no; the text is its reason, or ours, as a sentence."""


class RadioApp:
    def __init__(self, base: str, token: str = "", password: str = "", client_id: str = "digimodes",
                 timeout: float = 5.0):
        self.base = base if base.endswith("/") else base + "/"
        self.token = token
        self.password = password
        self.client_id = client_id
        self.timeout = timeout
        self._session: aiohttp.ClientSession | None = None
        self._logged_in = False
        self._beat: asyncio.Task | None = None
        self.keyed = False
        self.last_error = ""
        self.rx_rate = 0

    def url(self, path: str) -> str:
        return urljoin(self.base, path)

    async def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
            self._session = aiohttp.ClientSession(headers=headers, cookie_jar=aiohttp.CookieJar(unsafe=True),
                                                  timeout=aiohttp.ClientTimeout(total=self.timeout))
            self._logged_in = False
        if self.password and not self._logged_in:
            with contextlib.suppress(aiohttp.ClientError, asyncio.TimeoutError):
                async with self._session.post(self.url("api/login"), data={"password": self.password},
                                              allow_redirects=False):
                    pass
            self._logged_in = True
        return self._session

    async def close(self) -> None:
        await self.unkey()
        if self._session is not None:
            await self._session.close()

    async def _json(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        s = await self.session()
        try:
            async with s.request(method, self.url(path), json=body) as r:
                try:
                    data = await r.json(content_type=None)
                except ValueError:
                    data = {}
                return r.status, data if isinstance(data, dict) else {}
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise RadioRefused(_("the radio app at {url} did not answer ({why})").format(
                url=self.base, why=str(e) or type(e).__name__)) from e

    # -- receive ---------------------------------------------------------------

    async def rx(self):
        """(rate, s16le bytes) for as long as the radio app sends them."""
        s = await self.session()
        received = 0
        async with s.ws_connect(self.url("api/audio/rx?ack=1&window_ms=1000"), heartbeat=20,
                                timeout=aiohttp.ClientWSTimeout(ws_close=5)) as ws:
            first = await ws.receive(timeout=self.timeout)
            if first.type != aiohttp.WSMsgType.TEXT:
                raise RadioRefused(_("the radio app sent no audio format"))
            head = json.loads(first.data)
            if "error" in head:
                raise RadioRefused(str(head["error"]))
            if head.get("encoding", "s16le") != "s16le" or int(head.get("channels", 1)) != 1:
                raise RadioRefused(_("the radio app's audio is not 16-bit mono PCM"))
            self.rx_rate = rate = int(head.get("rate") or 12000)
            last_ack = time.monotonic()
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.BINARY:
                    received += len(msg.data)
                    if time.monotonic() - last_ack > 0.1:
                        last_ack = time.monotonic()
                        await ws.send_str(json.dumps({"ack": received}))
                    yield rate, msg.data
                elif msg.type == aiohttp.WSMsgType.TEXT:
                    with contextlib.suppress(ValueError):
                        err = json.loads(msg.data).get("error")
                        if err:
                            raise RadioRefused(str(err))
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break

    # -- transmit --------------------------------------------------------------

    async def key(self) -> None:
        """Take the radio app's lease and key it, or raise RadioRefused."""
        status, state = await self._json("GET", "api/state")
        if status != 200:
            raise RadioRefused(str(state.get("error") or _("the radio app's status answered HTTP {code}").format(
                code=status)))
        tx = state.get("tx")
        if not isinstance(tx, dict) or "remote" not in tx:
            raise RadioRefused(_("the radio app has no transmit lease (tx.remote), so it is not keyed from here"))
        status, body = await self._json("POST", "api/ptt", {"on": True, "remote": self.client_id})
        if status != 200 or body.get("onAir") is not True:
            raise RadioRefused(str(body.get("txError") or body.get("error")
                                   or _("the radio app did not key (HTTP {code})").format(code=status)))
        self.keyed = True
        self._beat = asyncio.ensure_future(self._heartbeat())

    async def _heartbeat(self) -> None:
        """Every second while keyed. A 409 is the lease gone (someone
        unkeyed, the time-out, the broker): stop at once."""
        while self.keyed:
            await asyncio.sleep(HEARTBEAT_S)
            try:
                status, body = await self._json("POST", "api/ptt/heartbeat", {"remote": self.client_id})
            except RadioRefused as e:
                self.last_error = str(e)
                continue
            if status != 200:
                self.last_error = str(body.get("error") or _("the radio app ended the lease"))
                self.keyed = False

    async def play(self, pcm: bytes, rate_of=None, realtime: bool = True) -> None:
        """Send s16le audio to api/audio/tx while keyed.

        `pcm` is made at the rate the radio app's header names, by
        `rate_of(rate) -> bytes` when given (so the waveform is generated at
        the right rate rather than resampled); 20 ms per message, paced at
        real time so the radio app's queue never fills.
        """
        s = await self.session()
        async with s.ws_connect(self.url("api/audio/tx"), timeout=aiohttp.ClientWSTimeout(ws_close=5)) as ws:
            first = await ws.receive(timeout=self.timeout)
            if first.type != aiohttp.WSMsgType.TEXT:
                raise RadioRefused(_("the radio app sent no transmit audio format"))
            head = json.loads(first.data)
            if "error" in head:
                raise RadioRefused(str(head["error"]))
            rate = int(head.get("rate") or 12000)
            data = rate_of(rate) if rate_of is not None else pcm
            step = 2 * round(rate * 0.02)
            t0 = time.monotonic()
            for i in range(0, len(data), step):
                if not self.keyed:
                    raise RadioRefused(self.last_error or _("the radio app ended the lease"))
                await ws.send_bytes(data[i:i + step])
                if realtime:
                    ahead = t0 + (i + step) / (2 * rate) - time.monotonic()
                    if ahead > 0.06:
                        await asyncio.sleep(ahead - 0.04)
                else:
                    await asyncio.sleep(0)

    async def unkey(self) -> None:
        """Never refused, and safe to repeat (any unkey ends the lease)."""
        was = self.keyed
        self.keyed = False
        if self._beat is not None:
            self._beat.cancel()
            self._beat = None
        if was:
            try:
                await self._json("POST", "api/ptt", {"on": False, "remote": self.client_id})
            except RadioRefused as e:
                # The radio app's own lease unkeys it within 5 s of silence.
                log.warning("unkey did not reach the radio app: %s", e)
