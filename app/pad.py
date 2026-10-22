"""The Ajazz AKP03E (and USB keypads) on Digital Modes: what its keys do.

As in OpenHamShack Flex: the pad is not this app's hardware. On a
controller its input host owns it and sends the app in front its actions as
`POST api/shack/pad/action` (CONTROLLER-PAD 6.1), held to that device's
level like any request; this file is the app's half - the declarations in
store/pad-actions.json, what each does, and what each key shows.

**Nothing on the pad starts a transmission.** shack_pad lets only `ptt`
and `transmit` key, and both mean "key the radio now"; a digital-modes
transmission is a slot's message chosen by the sequencer, which a key
press has no business starting blind. So the pad tunes, changes band and
mode, logs the finished contact, and halts - and halting is a Control
action, never refused. Calling CQ and replying stay on the page, where the
operator sees what will be sent. In the desktop form Digital Modes does not
yet join shack_pad's desktop hand-over (as Flex does not).
"""

from __future__ import annotations

import json
from pathlib import Path

from .i18n import _

HERE = Path(__file__).resolve().parent
ACTIONS_FILE = next((p for p in (HERE.parent / "store" / "pad-actions.json", HERE / "store" / "pad-actions.json")
                     if p.exists()), HERE.parent / "store" / "pad-actions.json")
TUNE_STEP_HZ = 10


def declared() -> dict:
    return json.loads(ACTIONS_FILE.read_text(encoding="utf-8"))


class Pad:
    def __init__(self, app) -> None:
        self.app = app
        self.actions = {a["id"]: a for a in declared()["actions"]}

    async def run(self, action: str, kind: str, step: int) -> tuple[int, dict]:
        base = action.split(":", 1)[0]
        derived = base in self.actions and action.endswith((":up", ":down"))
        if action not in self.actions and not derived:
            return 400, {"error": _("{action} is not one of this app's pad actions").format(action=action or "?"),
                         "code": "bad_request"}
        if derived:
            action, kind, step = base, "turn", (1 if action.endswith(":up") else -1)
        if kind != self.actions[action]["kind"]:
            return 400, {"error": _("{action} is a {kind}").format(action=action, kind=self.actions[action]["kind"]),
                         "code": "bad_request"}
        app = self.app
        op = app.ops.value
        if action == "tune":
            await app.configure({"rxHz": op.rx_hz + int(step) * TUNE_STEP_HZ})
        elif action == "tx-to-rx":
            await app.configure({"txHz": op.rx_hz})
        elif action == "mode:next":
            await app.configure({"mode": "ft4" if op.mode == "ft8" else "ft8"})
        elif action in ("band", "band:next"):
            dials = [r for r in app.station.frequencies() if r["mode"] == op.mode]
            if not dials:
                return 409, {"error": _("there is no frequency list for this mode"), "code": "bad_request"}
            here = next((i for i, r in enumerate(dials) if r["dialHz"] == op.dial_hz), -1)
            nxt = dials[(here + (1 if action == "band:next" else int(step))) % len(dials)]
            await app.configure({"dialHz": nxt["dialHz"]})
        elif action == "halt":
            await app.halt()
        elif action == "log":
            status, body = await app.log_pending({})
            if status != 200:
                return status, body
        return 200, {"ok": True}

    def faces(self) -> dict:
        app = self.app
        op = app.ops.value
        sending = app.tx.sending is not None
        band = next((r["band"] for r in app.station.frequencies() if r["dialHz"] == op.dial_hz), "")
        faces = {
            "band": {"l1": band or "--", "l2": _("BAND"), "tone": "normal"},
            "band:next": {"l1": band or "--", "l2": _("next"), "tone": "normal"},
            "mode:next": {"l1": op.mode.upper(), "l2": _("MODE"), "tone": "normal"},
            "tune": {"l1": str(op.rx_hz), "l2": _("RX Hz"), "tone": "normal"},
            "tx-to-rx": {"l1": str(op.tx_hz), "l2": _("TX Hz"), "tone": "normal"},
            "halt": ({"l1": _("HALT"), "l2": _("ON AIR"), "tone": "onair"} if sending else
                     {"l1": _("HALT"), "l2": "", "tone": "normal" if app.sequencer.enabled else "disabled"}),
            "log": {"l1": _("LOG"), "l2": app.pending.call if app.pending else "",
                    "tone": "normal" if app.pending else "disabled"},
        }
        return {"faces": faces, "onAir": sending}
