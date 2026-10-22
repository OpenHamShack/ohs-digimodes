"""OpenHamShack Digital Modes as a desktop app: the same server, in its own window.

BETA-PLAN architecture change 3, as OpenHamShack Flex does it (its
app/desktop.py is this file's model): shack_desktop starts this program
again with `--server`, gives the child a loopback port and a per-launch
token, checks the port answers nowhere but 127.0.0.1, and shows the page in
the system's web engine (or the default browser).

    python3 -m app.desktop            the window (what the installers run)
    python3 -m app.desktop --server   the server the window starts

What the desktop form changes, and why:

- **The launch token guards everything** (shack_desktop's guard runs before
  the app's own middleware): nothing else on the machine can start a
  transmission through the window's server. This is the only server in the
  desktop app, and it listens on loopback for its own window (the "no
  servers inside desktop apps" rule, DESKTOP.md).
- **A sound card is a source** (`SHACK_FORM=desktop`), receive only, beside
  a radio app on this machine or the network.
- **The data lives in the user's folder** (`shack_desktop.data_dir`), and the
  station profile and logbook in shack_client's per-user shared store, the
  one every OpenHamShack app on this machine uses: pairing from any of them
  pairs this one, and a contact logged here syncs with the rest.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

KIND = "digimodes"
TITLE = "OpenHamShack Digital Modes"
ICON = "com.openhamshack.digimodes.png"


def _icon() -> str | None:
    for p in (Path(sys.prefix) / "share/icons/hicolor/256x256/apps" / ICON,
              Path("/usr/share/icons/hicolor/256x256/apps") / ICON):
        if p.is_file():
            return str(p)
    return None


def serve() -> int:
    import shack_desktop

    env = shack_desktop.server_env()
    shack_desktop.watch_parent()
    os.environ["SHACK_FORM"] = "desktop"
    os.environ.setdefault("DIGIMODES_DATA_DIR", str(shack_desktop.data_dir(KIND)))
    os.environ.pop("SHACK_PLATFORM", None)
    if not os.environ.get("SHACK_STATION_DB"):
        try:
            from shack_client.store import default_path
            os.environ["SHACK_STATION_DB"] = str(default_path())
        except Exception:               # noqa: BLE001 - the app's own folder still works
            pass

    import logging

    from aiohttp import web

    from . import main as digimodes
    from .settings import Installation

    inst = Installation()
    logging.basicConfig(level=getattr(logging, inst.log_level, logging.INFO),
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    web.run_app(digimodes.build_app(digimodes.App(inst), outer=[env.guard.aiohttp_middleware()]),
                host=env.host, port=env.port, access_log=None, print=None)
    return 0


def main() -> int:
    if "--server" in sys.argv:
        return serve()
    import shack_desktop

    return shack_desktop.run(command=shack_desktop.self_command("app.desktop", "--server"),
                             title=TITLE, kind=KIND, icon=_icon(), size=(1300, 860))


if __name__ == "__main__":
    sys.exit(main())
