"""What an FT8 or FT4 message says, and the six a station sends in a QSO.

ohsdigi decodes a slot into text ("CQ DL1ABC JO62", "GM4BRI DL1ABC -12");
the sequencer needs to know who it is to, who it is from, and which step of
a contact it is. This is that reading, for the standard messages of
WSJT-X's basic QSO (its User Guide, "Standard messages" and "Tx1-Tx6"),
which is all the beta sequences. Anything else - contest exchanges, free
text, telemetry - parses as `other` and is shown but never answered
automatically.

The six messages are WSJT-X's, in its numbering, so an operator who knows
WSJT-X reads the same Tx1-Tx6 here:

    Tx1  DX MY GRID      answering a CQ
    Tx2  DX MY -12       a report
    Tx3  DX MY R-12      a report, acknowledging theirs
    Tx4  DX MY RR73      acknowledging, and signing
    Tx5  DX MY 73        signing
    Tx6  CQ MY GRID      calling CQ

Only the first four characters of a locator are sent: a standard message
has room for four (77-bit type 1), and ohsdigi refuses six.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: RR73 has the shape of a locator (two letters A-R, two digits), so it is
#: tested for first, exactly as WSJT-X does.
_GRID = re.compile(r"^[A-R]{2}[0-9]{2}$")
_REPORT = re.compile(r"^(R)?([+-][0-9]{2})$")
#: CQ's directed forms: "CQ DX", "CQ NA", "CQ POTA", "CQ 290"...
_CQ_MODIFIER = re.compile(r"^([A-Z]{1,4}|[0-9]{3})$")


@dataclass(frozen=True)
class Parsed:
    kind: str             # cq, grid, report, rreport, rr73, 73, other
    to: str = ""          # "" for a CQ
    de: str = ""
    grid: str = ""
    report: str = ""      # "-12", "+05" (without the R)
    text: str = ""

    def public(self) -> dict:
        return {"kind": self.kind, "to": self.to, "de": self.de, "grid": self.grid,
                "report": self.report}


def _call(token: str) -> str:
    """A callsign as sent: hashed calls arrive in <angle brackets>."""
    return token.strip("<>").upper()


def parse(text: str) -> Parsed:
    words = str(text or "").upper().split()
    if not words:
        return Parsed("other", text=text)
    if words[0] == "CQ":
        rest = words[1:]
        if len(rest) >= 2 and _CQ_MODIFIER.match(rest[0]) and not _GRID.match(rest[1]):
            rest = rest[1:]
        if not rest:
            return Parsed("other", text=text)
        grid = rest[1] if len(rest) > 1 and _GRID.match(rest[1]) and rest[1] != "RR73" else ""
        return Parsed("cq", de=_call(rest[0]), grid=grid, text=text)
    if len(words) < 3:
        return Parsed("other", text=text)
    to, de, last = _call(words[0]), _call(words[1]), words[2]
    if len(words) > 3:
        return Parsed("other", to=to, de=de, text=text)
    if last in ("RR73", "RRR"):
        return Parsed("rr73", to=to, de=de, text=text)
    if last == "73":
        return Parsed("73", to=to, de=de, text=text)
    m = _REPORT.match(last)
    if m:
        return Parsed("rreport" if m.group(1) else "report", to=to, de=de, report=m.group(2), text=text)
    if _GRID.match(last):
        return Parsed("grid", to=to, de=de, grid=last, text=text)
    return Parsed("other", to=to, de=de, text=text)


def report(snr: int | float) -> str:
    """An SNR as a report: two digits and a sign, clamped to what fits."""
    n = max(-30, min(49, int(round(snr))))
    return f"{n:+03d}"


def tx_messages(mycall: str, mygrid: str, dxcall: str, sent: str) -> dict[int, str]:
    """WSJT-X's Tx1-Tx6 for this contact (see the module docstring)."""
    my, dx, grid = mycall.upper(), dxcall.upper(), (mygrid or "")[:4].upper()
    rep = sent or "-10"
    out = {6: f"CQ {my} {grid}".strip()}
    if dx:
        out.update({1: f"{dx} {my} {grid}".strip(), 2: f"{dx} {my} {rep}", 3: f"{dx} {my} R{rep}",
                    4: f"{dx} {my} RR73", 5: f"{dx} {my} 73"})
    return out
