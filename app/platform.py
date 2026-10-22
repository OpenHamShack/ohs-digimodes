"""Digital Modes as a store app on an OpenHamShack controller.

Adapted from OpenHamShack Flex's (and so Icom's) file of the same name, so
the three apps read identity and levels the same way; only the route rules
and the services differ, and there is no broker claim: this app holds no
hardware. The radio it transmits through is another app's, and that app
holds the broker's claim and makes the broker's checks when it keys
(CONTROLLER-CONTRACT 6.2).

**Standalone** (the desktop window, or a developer's run on 127.0.0.1)
nothing here applies: the desktop shell's launch token guards the server.
**On the platform** (`SHACK_PLATFORM=dock360`, which the store image sets)
the controller signs people in, in front of the module, and says who is
asking and at what level in headers its proxy vouches for with a secret
(CONTROLLER-CONTRACT 5.3). No secret is a locked app, never an open one: a
request the proxy did not vouch for is `401 proxy_required`.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import os
import secrets
from dataclasses import dataclass
from typing import Awaitable, Callable

from .i18n import _

log = logging.getLogger("platform")

PROXY_SECRET_ENV = "DOCK360_SHACK_PROXY_SECRET"
SHACK_URL_ENV = "DOCK360_SHACK_URL"
SHACK_TOKEN_ENV = "DOCK360_SHACK_TOKEN"
PLATFORM_ENV = "SHACK_PLATFORM"

LEVELS = ("none", "view", "control", "transmit")
SECRET_HEADER = "X-OpenHamShack-Proxy-Secret"


def platform_mode() -> bool:
    return os.environ.get(PLATFORM_ENV, "").strip().lower() == "dock360"


def level_at_least(level: str, needed: str) -> bool:
    """Whether `level` reaches `needed`. An unknown level reaches nothing."""
    if needed == "none":
        return True
    if level not in LEVELS or needed not in LEVELS or level == "none":
        return False
    return LEVELS.index(level) >= LEVELS.index(needed)


@dataclass(frozen=True)
class Identity:
    user: str
    level: str
    device: str = "-"
    auth: str = ""

    def public(self) -> dict:
        return {"user": self.user, "level": self.level, "device": self.device, "auth": self.auth}


def identity(headers, secret: str) -> Identity | None:
    """The person the controller's proxy vouches for, or None. The secret is
    compared first, in constant time, and nothing else is read until it
    matches: every other app on the controller can reach this port."""
    if not secret:
        return None
    given = headers.get(SECRET_HEADER, "") or ""
    if not secrets.compare_digest(given.encode(), secret.encode()):
        return None
    user = (headers.get("X-OpenHamShack-User") or "").strip()
    level = (headers.get("X-OpenHamShack-Level") or "none").strip().lower()
    if not user or level not in LEVELS:
        return None
    return Identity(user=user, level=level,
                    device=(headers.get("X-OpenHamShack-Device") or "-").strip() or "-",
                    auth=(headers.get("X-OpenHamShack-Auth") or "").strip().lower())


# -- what each request needs (CONTROLLER-CONTRACT 5.2) ----------------------
#
# The same rules as store/module.yaml's `levels`. The allow-list design Icom
# settled: reading is View, a short list is Control, and every other write
# needs Transmit, so a route added later is closed to a TV until somebody
# decides otherwise. What transmits here is starting a sequence (CQ, a
# reply, enabling TX); halting one is Control, because stopping a
# transmission is never refused to anyone who may touch the app at all.

CONTROL_POSTS = (
    "/api/config", "/api/halt", "/api/log", "/api/log/dismiss", "/api/shack/pad/action",
)
PLATFORM_ROUTES = {("POST", "/api/shack/release"), ("GET", "/api/shack/busy")}


def required_level(method: str, path: str) -> str:
    method = method.upper()
    if (method, path) in PLATFORM_ROUTES:
        return "none"
    if method in ("GET", "HEAD", "OPTIONS"):
        return "view"
    if any(fnmatch.fnmatchcase(path, glob) for glob in CONTROL_POSTS):
        return "control"
    return "transmit"


def refusal_body(level: str, needs: str) -> dict:
    names = {"view": _("View"), "control": _("Control"), "transmit": _("Transmit")}
    return {"error": _("This needs the {needs} level; your level for Digital Modes is {level}.").format(
                needs=names.get(needs, needs), level=names.get(level, level)),
            "code": "level_too_low", "level": level, "needs": needs}


# -- the controller's services, with this app's own token ---------------------

Request = Callable[[str, str, "dict | None"], Awaitable[tuple[int, dict]]]


def http_request(base: str, token: str, timeout: float = 3.0) -> Request:
    import aiohttp

    async def call(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        hdrs = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as s:
                async with s.request(method, base.rstrip("/") + path, json=body, headers=hdrs) as r:
                    raw, status = await r.read(), r.status
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise OSError(str(e) or type(e).__name__) from e
        try:
            parsed = json.loads(raw or b"{}")
        except ValueError:
            parsed = {}
        return status, parsed if isinstance(parsed, dict) else {}
    return call


class PlatformClient:
    """The controller, as this app reaches it: profile, logbook, spots
    (SERVICES 2, 3 and 5)."""

    def __init__(self, request: Request):
        self.request = request

    @classmethod
    def from_env(cls) -> "PlatformClient | None":
        base, token = os.environ.get(SHACK_URL_ENV, ""), os.environ.get(SHACK_TOKEN_ENV, "")
        if not base or not token:
            return None
        return cls(http_request(base, token))

    async def _call(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        try:
            return await self.request(method, path, body)
        except OSError as e:
            log.info("the controller did not answer %s %s: %s", method, path, e)
            return 0, {}

    async def profile(self) -> dict | None:
        status, body = await self._call("GET", "/profile")
        return body if status == 200 else None

    async def post_qso(self, record: dict) -> bool:
        """The contact into the one logbook. Idempotent by uuid (SERVICES
        3.2), so a retry after a lost answer is never a second contact."""
        status, _body = await self._call("POST", "/logbook/qsos", record)
        return status in (200, 201)

    async def post_spots(self, spots: list[dict]) -> bool:
        if not spots:
            return True
        status, _body = await self._call("POST", "/spots", {"spots": spots[:200]})
        return status in (200, 201, 202)
