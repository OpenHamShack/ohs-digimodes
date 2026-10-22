"""Two kinds of setting, kept apart on purpose.

**Installation** settings come from the environment: the store manifest's
`env` (store/module.yaml) on a controller, the desktop shell in the desktop
form. They are what an administrator decides once - above all whether this
app may transmit at all - and the page can show them but never change them.

**Operating** settings are what an operator changes while working: mode,
dial, the audio offsets, which source to listen to. They are saved in the
data folder (written by rename, so a crash leaves the old file or the new,
never half), and a value the page saved beats the environment's seed, as in
Flex: a changed default never silently overrides what somebody chose.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from . import engine

DATA_ENV = "DIGIMODES_DATA_DIR"
PORT_ENV = "DIGIMODES_HTTP_PORT"


def _bool(v: str | None) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


class Installation:
    def __init__(self, env=None):
        env = os.environ if env is None else env
        self.data_dir = Path(env.get(DATA_ENV) or "/data")
        self.port = int(env.get(PORT_ENV) or 8080)
        self.allow_transmit = _bool(env.get("ALLOW_TRANSMIT"))
        self.radio_url = (env.get("RADIO_URL") or "").strip()
        self.radio_token = (env.get("RADIO_TOKEN") or "").strip()
        self.radio_password = env.get("RADIO_PASSWORD") or ""
        self.depth = max(1, min(3, int(env.get("DECODE_DEPTH") or 2)))
        self.log_level = (env.get("LOG_LEVEL") or "INFO").upper()
        #: "desktop" in the desktop window (app/desktop.py), else "server".
        self.form = (env.get("SHACK_FORM") or "server").strip()

    def public(self) -> dict:
        return {"allowTransmit": self.allow_transmit, "form": self.form, "depth": self.depth,
                "radioTokenSet": bool(self.radio_token), "radioPasswordSet": bool(self.radio_password)}


SOURCES = ("radio", "soundcard", "wav", "none")


@dataclass
class Operating:
    mode: str = "ft8"
    dial_hz: int = 14074000
    rx_hz: int = 1500
    tx_hz: int = 1500
    #: Where receive audio comes from: one of SOURCES.
    source: str = "radio"
    radio_url: str = ""
    sound_device: str = ""
    wav_path: str = ""
    #: The desktop form's transmit switch. A desktop has no administrator's
    #: settings page, so the person at it sets it here; on a controller it
    #: is ignored and ALLOW_TRANSMIT (the manifest's setting) decides.
    allow_transmit: bool = False

    def clean(self) -> "Operating":
        if self.mode not in engine.MODES:
            self.mode = "ft8"
        self.rx_hz = max(200, min(3000, int(self.rx_hz)))
        self.tx_hz = max(200, min(3000 - engine.WIDTH_HZ[self.mode], int(self.tx_hz)))
        self.dial_hz = max(0, int(self.dial_hz))
        if self.source not in SOURCES:
            self.source = "none"
        return self

    def public(self) -> dict:
        return {"mode": self.mode, "dialHz": self.dial_hz, "rxHz": self.rx_hz, "txHz": self.tx_hz,
                "source": self.source, "radioUrl": self.radio_url, "soundDevice": self.sound_device,
                "wavPath": self.wav_path, "allowTransmit": self.allow_transmit}

    #: The page's names for the fields.
    NAMES = {"mode": "mode", "dialHz": "dial_hz", "rxHz": "rx_hz", "txHz": "tx_hz", "source": "source",
             "radioUrl": "radio_url", "soundDevice": "sound_device", "wavPath": "wav_path",
             "allowTransmit": "allow_transmit"}


class Store:
    def __init__(self, path: Path, seed: Operating | None = None):
        self.path = path
        self.value = seed or Operating()
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            known = {f.name for f in fields(Operating)}
            for k, v in saved.items():
                if k in known:
                    setattr(self.value, k, v)
        except (FileNotFoundError, ValueError):
            pass
        self.value.clean()

    def update(self, changes: dict) -> Operating:
        for k, v in changes.items():
            name = Operating.NAMES.get(k)
            if name is not None:
                setattr(self.value, name, type(getattr(self.value, name))(v))
        self.value.clean()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self.value), indent=1), encoding="utf-8")
        tmp.replace(self.path)
        return self.value
