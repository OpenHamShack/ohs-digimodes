"""The station: its profile, where it may transmit, its frequencies, its log.

All four are shared, never this app's own (GUIDE 1, "one of each"):

- **The profile** (callsign, locator, country, licence) is shack_client's
  one store per user per machine, synced to a controller when paired; on
  the platform the controller's copy is read and this app never edits it.
  Adapted from OpenHamShack Flex's stationprofile.py.
- **Whether a transmission is allowed** is shack_bandplan's verdict for that
  profile, at the frequency the signal will really occupy: the dial plus the
  audio offset, as wide as the mode is (engine.WIDTH_HZ). No band edge is
  written here.
- **The frequency list** comes from the dataset too: `presets(ctx,
  "digimodes")` when the dataset carries one. The pinned dataset (2026.10.2)
  does not yet, so until it does the list is WSJT-X's own default
  (app/data/dial-frequencies.json, data and not code: an operator's
  convention, not a licence fact), named by the dataset's bands, and every
  entry still judged by the dataset's licence check - a 60 m dial outside
  the UK's segments shows as not transmittable rather than vanishing.
- **The logbook** is shack_client's. A contact always goes into the local
  replica first (offline first: a controller that is away loses nothing);
  on the platform it is also posted to the controller's logbook, idempotent
  by uuid, and posted again later if that failed. Standalone and paired,
  shack_client's own sync carries it.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import date, datetime, timezone
from pathlib import Path

from . import engine
from .i18n import _

log = logging.getLogger("station")

HERE = Path(__file__).resolve().parent
DIALS_FILE = HERE / "data" / "dial-frequencies.json"
STATION_DB_ENV = "SHACK_STATION_DB"
APP_ID = "shack-digimodes"
#: ADIF 3.1.4's mode/submode pair (SERVICES 3.1): FT8 is a mode, FT4 is MFSK.
ADIF_MODE = {"ft8": ("FT8", None), "ft4": ("MFSK", "FT4")}


def store_path(data_dir: Path) -> Path:
    explicit = os.environ.get(STATION_DB_ENV, "").strip()
    return Path(explicit) if explicit else data_dir / "shack" / "station.db"


class Station:
    REFRESH_S = 60.0

    def __init__(self, path: Path | None, *, platform=None, app_version: str = "", today=None,
                 outbox: Path | None = None):
        self.path = path
        self.platform = platform
        self.app_version = app_version
        self._today = today
        self._store = None
        self._store_error = ""
        self._remote: dict | None = None
        self._remote_at = 0.0
        self._data = None
        self._data_error = ""
        #: uuids logged here but not yet accepted by the controller.
        self.outbox = outbox
        self._unsent: list[str] = self._read_outbox()

    # -- the store -----------------------------------------------------------

    @property
    def store(self):
        if self._store is None and self.path is not None and not self._store_error:
            try:
                from shack_client import Station as ShackStation
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._store = ShackStation.open(app=APP_ID, app_version=self.app_version, path=self.path,
                                                background=self.platform is None)
            except Exception as e:      # noqa: BLE001 - reported, never fatal
                self._store_error = str(e)
                log.error("the station store at %s could not be opened: %s", self.path, e)
        return self._store

    def close(self) -> None:
        if self._store is not None:
            self._store.close()

    def get(self) -> dict:
        if self.platform is not None and self._remote is not None:
            return self._remote
        st = self.store
        return st.profile.get() if st is not None else {"schema": 1}

    async def refresh(self, force: bool = False) -> None:
        if self.platform is None:
            return
        if force or self._remote is None or time.monotonic() - self._remote_at >= self.REFRESH_S:
            got = await self.platform.profile()
            if got is not None:
                self._remote, self._remote_at = got, time.monotonic()
        await self.flush()

    def update(self, partial: dict) -> dict:
        if self.platform is not None:
            raise PermissionError(_("the station profile belongs to the controller; change it there"))
        st = self.store
        if st is None:
            raise RuntimeError(_("the station store could not be opened: {why}").format(why=self._store_error))
        return st.profile.update(partial)

    def callsign(self) -> str:
        return str(self.get().get("callsign") or "").upper()

    def locator(self) -> str:
        return str(self.get().get("locator") or "").upper()

    # -- the dataset -----------------------------------------------------------

    @property
    def data(self):
        if self._data is None and not self._data_error:
            try:
                import shack_bandplan
                self._data = shack_bandplan.load()
            except Exception as e:      # noqa: BLE001 - reported; transmitting then refuses
                self._data_error = str(e)
                log.error("the band-plan dataset could not be loaded: %s", e)
        return self._data

    def context(self):
        import shack_bandplan
        today = self._today() if callable(self._today) else (self._today or date.today().isoformat())
        return shack_bandplan.context(self.get(), self.data, today=today)

    def verdict(self, dial_hz: int, audio_hz: float, mode: str) -> dict:
        """shack_bandplan's verdict on sending `mode` at dial + audio."""
        if self.data is None:
            return {"allowed": False, "refusal": {"code": "no_data", "text": _(
                "the band-plan dataset is missing, so nothing can be checked")}, "warnings": []}
        import shack_bandplan
        width = engine.WIDTH_HZ[mode]
        lo = dial_hz + audio_hz
        return shack_bandplan.check(self.context(), shack_bandplan.Signal.edges(lo, lo + width, mode=mode),
                                    plan_guard="warn")

    def refusal(self, dial_hz: int | None, audio_hz: float, mode: str) -> str | None:
        """Why this station may not send there, in a sentence, or None."""
        if not self.callsign():
            return _("no callsign is set in the station profile, and an unidentified "
                     "transmission is not something this station makes")
        if not dial_hz:
            return _("no dial frequency is chosen")
        v = self.verdict(dial_hz, audio_hz, mode)
        if v.get("allowed"):
            return None
        return str((v.get("refusal") or {}).get("text") or _("the station's licence does not allow this transmission"))

    def frequencies(self) -> list[dict]:
        """The dial list for both modes, each row judged (module docstring)."""
        rows: list[dict] = []
        data = self.data
        if data is None:
            return rows
        import shack_bandplan
        ctx = self.context()
        source = "dataset"
        for mode in engine.MODES:
            got = [p for p in shack_bandplan.presets(ctx, "digimodes") if p.get("mode") == mode]
            for p in got:
                rows.append({"mode": mode, "dialHz": int(p.get("txHz") or p.get("rxHz")), "label": p.get("label", "")})
        if not rows:
            source = "wsjtx-default"
            spec = json.loads(DIALS_FILE.read_text(encoding="utf-8"))
            rows = [{"mode": e["mode"], "dialHz": int(e["dialHz"]), "label": ""} for e in spec["entries"]]
        out = []
        for r in rows:
            band = shack_bandplan.band_of(data, r["dialHz"])
            if not band:
                continue
            why = self.refusal(r["dialHz"], 1500.0, r["mode"])
            out.append({**r, "band": band, "tx": why is None, "why": why or "", "source": source})
        return out

    def licence(self) -> dict:
        if self.data is None:
            return {"canTransmit": False, "why": _("the band-plan dataset is missing, so nothing can be checked")}
        import shack_bandplan
        return shack_bandplan.in_force(self.get(), self.data)

    def public(self) -> dict:
        p = self.get()
        return {"source": ("controller" if self._remote is not None else "replica") if self.platform else "local",
                "callsign": p.get("callsign") or "", "locator": p.get("locator") or "",
                "country": p.get("country") or "", "licence": p.get("licence") or {},
                "derived": self.licence(), "editable": self.platform is None,
                "error": self._store_error or self._data_error}

    # -- the logbook -------------------------------------------------------------

    def qso_fields(self, contact, mode: str, dial_hz: int, audio_hz: float, end: float) -> dict:
        """The contact as SERVICES 3.1 fields (lower-case ADIF names)."""
        adif_mode, submode = ADIF_MODE[mode]
        iso = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
        hz = int(dial_hz + audio_hz) if dial_hz else None
        f = {"call": contact.call, "start": iso(contact.start), "end": iso(end), "mode": adif_mode,
             "rst_sent": contact.rst_sent, "rst_rcvd": contact.rst_rcvd,
             "station_callsign": self.callsign(), "operator": self.callsign(),
             "my_gridsquare": self.locator()}
        if submode:
            f["submode"] = submode
        if hz:
            f["freq_hz"] = hz
            if self.data is not None:
                import shack_bandplan
                band = shack_bandplan.band_of(self.data, hz)
                if band:
                    f["band"] = band
        if contact.grid:
            f["gridsquare"] = contact.grid
            f["app_shack_grid_source"] = "decode"
        if contact.rst_sent:
            # Our report of them is the decoder's SNR: kept as a number too.
            f["app_shack_decode_snr"] = contact.rst_sent
        return {k: v for k, v in f.items() if v not in ("", None)}

    async def log(self, fields: dict) -> dict:
        """Store a contact: locally first, then the controller's logbook."""
        st = self.store
        if st is None:
            raise RuntimeError(_("the station store could not be opened: {why}").format(why=self._store_error))
        q = st.logbook.add(fields)
        if self.platform is not None:
            self._unsent.append(q.uuid)
            self._write_outbox()
            await self.flush()
        return {"uuid": q.uuid, "fields": q.fields, "duplicateOf": list(getattr(q, "duplicate_of", []) or []),
                "sent": self.platform is None or q.uuid not in self._unsent}

    async def flush(self) -> None:
        """Post contacts the controller has not accepted yet (platform only)."""
        if self.platform is None or not self._unsent or self.store is None:
            return
        for uid in list(self._unsent):
            q = self.store.logbook.get(uid)
            if q is None or await self.platform.post_qso({"uuid": uid, "fields": q.fields}):
                self._unsent.remove(uid)
            else:
                break
        self._write_outbox()

    def recent(self, limit: int = 50) -> list[dict]:
        st = self.store
        if st is None:
            return []
        try:
            rows = st.logbook.query(limit=limit)
        except TypeError:
            rows = st.logbook.query()[:limit]
        return [{"uuid": q.uuid, **{k: q.fields.get(k) for k in
                                    ("call", "start", "band", "mode", "submode", "rst_sent", "rst_rcvd",
                                     "gridsquare", "freq_hz")}} for q in rows]

    def _read_outbox(self) -> list[str]:
        if self.outbox is None:
            return []
        try:
            return list(json.loads(self.outbox.read_text()))
        except (FileNotFoundError, ValueError):
            return []

    def _write_outbox(self) -> None:
        if self.outbox is None:
            return
        tmp = self.outbox.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._unsent))
        tmp.replace(self.outbox)
