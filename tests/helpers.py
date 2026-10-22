"""Small helpers the tests share: a fake radio app on a real socket, a station."""

import contextlib
import sys
import tempfile
from pathlib import Path

from aiohttp.test_utils import TestServer

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import fakeradio  # noqa: E402

from app.station import Station  # noqa: E402


@contextlib.asynccontextmanager
async def fake_radio(**kw):
    fake = fakeradio.Fake(**kw)
    server = TestServer(fakeradio.make_app(fake))
    await server.start_server()
    try:
        yield fake, str(server.make_url("/"))
    finally:
        await server.close()


def station(profile: dict | None = None) -> Station:
    st = Station(Path(tempfile.mkdtemp(prefix="digimodes-st-")) / "station.db", today="2026-10-09")
    if profile:
        st.update(profile)
    return st


FULL_GB = {"callsign": "GM4BRI", "locator": "IO75RL", "country": "GB", "licence": {"class": "full"}}
