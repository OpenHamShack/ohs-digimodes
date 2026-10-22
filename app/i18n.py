"""The server's own sentences, through stdlib gettext (openhamshack
docs/contract/I18N.md section 2: Python outside a Qt UI uses gettext, one
domain per package; this app's is `shack-digimodes`).

What goes through here is what a person reads that the server wrote: the
`error` sentence of a JSON answer, which the page shows as it came. Log lines
and exception text nobody is shown stay English and stay out (section 1,
rule 8).

The beta ships English only. With no catalog installed gettext hands back the
msgid, which is the English, so nothing is needed at runtime; a translation is
`locale/<lang>/LC_MESSAGES/shack-digimodes.mo` beside this file, made from the
template `locale/shack-digimodes.pot` that `python3 tools/i18n.py extract` writes
(and the selftests refuse when it is stale).

One language for the whole process, chosen by gettext from the locale
variables (LANGUAGE, LC_ALL, LC_MESSAGES, LANG). Not a module setting yet:
the selftests hold every variable the code reads to one the manifest offers,
and a language setting with only English to pick is a setting that does
nothing. Per-request choice from Accept-Language is the obvious next
step once a second language exists; until then it would be machinery with
nothing to choose between. Clients that want their own wording translate the
error by its HTTP status today and by a `code` once the controller contract's
codes reach this module (CONTROLLER-CONTRACT.md section 9).
"""

from __future__ import annotations

import gettext
from pathlib import Path

DOMAIN = "shack-digimodes"
LOCALE = Path(__file__).resolve().parent / "locale"


_t = gettext.translation(DOMAIN, LOCALE, fallback=True)


def use(languages: list[str] | None, localedir: Path | None = None) -> None:
    """Switch language (None: back to the system's). localedir is for tests
    that build a catalog of their own."""
    global _t
    _t = gettext.translation(DOMAIN, localedir or LOCALE, languages=languages, fallback=True)


def _(message: str) -> str:
    return _t.gettext(message)


def ngettext(singular: str, plural: str, n: int) -> str:
    return _t.ngettext(singular, plural, n)
