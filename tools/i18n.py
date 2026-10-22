#!/usr/bin/env python3
"""Translation tooling for OpenHamShack Digital Modes: the string check, its baseline,
and the server's gettext template (docs/I18N.md says when to run which).

    python3 tools/i18n.py check            the string check, as tools/selftest.py runs it
    python3 tools/i18n.py baseline         drop baseline entries that no longer match
    python3 tools/i18n.py extract [--check] rewrite (or verify) the server's .pot

The check and baseline half of this file is the same in every OpenHamShack
repo that is not PyQt6 (Icom, Flex, Digital Modes, Remote, the Icom TV kind); the PyQt6
repos carry the same two commands beside their Qt catalog tools. Change it
everywhere or nowhere.

Why each piece is the way it is:

- The check is ohs-libs' `shack-i18n-check` (openhamshack
  docs/contract/I18N.md section 3), run from a pinned wheel in tools/vendor
  rather than an installed package: the selftests run with whatever
  python3 the host or the image has, and a wheel is a zip that Python imports
  straight off sys.path, so neither needs a venv or a network. Update it by
  building a new wheel with ohs-libs' tools/build.sh and changing WHEEL.
- i18n-pending.txt is the baseline: every literal that was already in the
  code when the check arrived (M1), so the check fails on a *new* literal
  today while M3 converts the old ones. It uses the allow-list's line format
  so an entry that turns out not to be language can move to i18n-allow.txt
  by giving it a real reason. A baseline entry that matches nothing fails the
  check, exactly as a stale allow-list entry does, so the count only falls.
  `baseline` never adds an entry unless told to with --add-new, which is for
  the day the baseline is first written and not for silencing the check.
- The gettext template is written by a small extractor here, not by
  `pybabel extract`: Babel is on none of the build hosts and the server is
  standard library only. It knows the one pattern the server uses, a literal
  inside `_()` or `ngettext()`, which is also the only pattern the check
  accepts as "already in a catalog". No .mo is committed: the beta ships
  English, gettext falls back to the msgid, and a translation compiles its
  .mo at build when there is one.
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

WHEEL = ROOT / "tools" / "vendor" / "shack_i18n-0.1.0-py3-none-any.whl"
BASELINE = ROOT / "i18n-pending.txt"

# The server's gettext domain: its source and its template. Empty in a repo
# with no Python server (the Android and Apple clients).
GETTEXT = {"domain": "shack-digimodes", "sources": ["app"], "pot": "app/locale/shack-digimodes.pot"}


# --------------------------------------------------------------------------
# The check and its baseline

def _shack_i18n():
    if str(WHEEL) not in sys.path:
        sys.path.insert(0, str(WHEEL))
    from shack_i18n import allow, check
    return allow, check


def _findings():
    """(report, baseline entries, problems with the baseline file)."""
    allow, check = _shack_i18n()
    report = check.run(cwd=ROOT)
    entries, problems = allow.load(BASELINE, BASELINE.name)
    return report, entries, problems


def _split(report, entries):
    """Findings the baseline does not cover, and how many it covered.

    Matched on exact path and literal: a baseline entry stands for one
    literal in one file, wherever in that file it moves, so moving code
    around does not churn the file but a new sentence always shows up.
    """
    keyed = {(e.glob, e.literal): e for e in entries}
    left, covered = [], 0
    for f in report.findings:
        e = keyed.get((f.path, f.literal)) if f.literal is not None else None
        if e is None:
            left.append(f)
        else:
            e.used = True
            covered += 1
    return left, covered


def cmd_check(_args) -> int:
    try:
        report, entries, problems = _findings()
    except Exception as err:  # the vendored check's ConfigError, or no wheel
        print(f"i18n check could not run: {err}", file=sys.stderr)
        return 2
    left, covered = _split(report, entries)
    stale = [e for e in entries if not e.used]
    out = sorted(left + report.warnings, key=lambda f: (f.path, f.line, f.col))
    for f in out:
        print(f.render())
    for p in problems:
        print(p.render())
    for e in stale:
        print(f"{BASELINE.name}:{e.line}:1: the baseline entry for {e.literal!r} in {e.glob} "
              f"matched nothing; it has been converted or removed, so run tools/i18n.py baseline")
    failed = len(left) + len(problems) + len(stale)
    print(f"i18n: {failed} finding(s), {len(report.warnings)} warning(s); "
          f"baseline {len(entries)} literal(s) still to convert ({covered} occurrence(s)) in {BASELINE.name}")
    return 1 if failed else 0


HEADER = """\
# Baseline for shack-i18n-check (docs/I18N.md): user-visible literals that
# were in the code before the check arrived, one per file and literal. M3
# moves each into a catalog and deletes its line; the check fails on any
# literal not listed here or in i18n-allow.txt, and on a line here that no
# longer matches. Rewrite with tools/i18n.py baseline, never by hand-adding.
# path | "literal" | pending
"""


def cmd_baseline(args) -> int:
    report, entries, problems = _findings()
    if problems:
        for p in problems:
            print(p.render())
        return 1
    left, _ = _split(report, entries)
    keep = {(e.glob, e.literal) for e in entries if e.used}
    new = [f for f in left if f.literal is not None]
    if new and not args.add_new:
        for f in new:
            print(f.render())
        print(f"{len(new)} new literal(s) above are not in the baseline. Put them in the catalog "
              f"(or in i18n-allow.txt with a reason); --add-new is only for writing the first baseline.")
    if args.add_new:
        keep |= {(f.path, f.literal) for f in new}
    removed = len(entries) - len([e for e in entries if e.used])
    lines = [f"{path} | {json.dumps(lit, ensure_ascii=False)} | pending" for path, lit in sorted(keep)]
    BASELINE.write_text(HEADER + "".join(line + "\n" for line in lines), encoding="utf-8")
    print(f"{BASELINE.name}: {len(lines)} literal(s); removed {removed}")
    return 1 if new and not args.add_new else 0


# --------------------------------------------------------------------------
# The server's gettext template

def _po_quote(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


def gettext_entries() -> dict[tuple[str, str | None], list[str]]:
    """{(msgid, msgid_plural): [file, ...]} for every literal _() and
    ngettext() in the server's source."""
    found: dict[tuple[str, str | None], list[str]] = {}
    for src in GETTEXT.get("sources", []):
        for path in sorted((ROOT / src).rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                    continue
                args = node.args
                lit = [isinstance(a, ast.Constant) and isinstance(a.value, str) for a in args]
                if node.func.id == "_" and args and lit[0]:
                    key = (args[0].value, None)
                elif node.func.id == "ngettext" and len(args) >= 2 and lit[0] and lit[1]:
                    key = (args[0].value, args[1].value)
                else:
                    continue
                # The file, not the line: with line numbers every edit above a
                # sentence would make the committed template stale and fail
                # the selftests for a change that touched no words.
                where = path.relative_to(ROOT).as_posix()
                found.setdefault(key, []).append(where)
    return found


def render_pot() -> str:
    domain = GETTEXT["domain"]
    out = [f"# English template for the {domain} domain. Written by tools/i18n.py extract; do not edit.",
           'msgid ""', 'msgstr ""', '"Content-Type: text/plain; charset=UTF-8\\n"',
           '"Plural-Forms: nplurals=2; plural=(n != 1);\\n"', f'"X-Domain: {domain}\\n"', ""]
    for (msgid, plural), wheres in sorted(gettext_entries().items(), key=lambda kv: kv[0][0]):
        out += [f"#: {w}" for w in sorted(set(wheres))]
        out.append(f"msgid {_po_quote(msgid)}")
        if plural is None:
            out.append('msgstr ""')
        else:
            out += [f"msgid_plural {_po_quote(plural)}", 'msgstr[0] ""', 'msgstr[1] ""']
        out.append("")
    return "\n".join(out)


def cmd_extract(args) -> int:
    if not GETTEXT:
        print("this repo has no gettext domain")
        return 0
    pot = ROOT / GETTEXT["pot"]
    text = render_pot()
    if args.check:
        if not pot.is_file() or pot.read_text(encoding="utf-8") != text:
            print(f"{GETTEXT['pot']} is out of date; run python3 tools/i18n.py extract")
            return 1
        print(f"{GETTEXT['pot']} is up to date")
        return 0
    pot.parent.mkdir(parents=True, exist_ok=True)
    pot.write_text(text, encoding="utf-8")
    print(f"{GETTEXT['pot']}: {len(gettext_entries())} message(s)")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="the string check with the baseline (what the selftests run)")
    b = sub.add_parser("baseline", help="drop baseline entries that no longer match")
    b.add_argument("--add-new", action="store_true", help="also add every current finding (first baseline only)")
    e = sub.add_parser("extract", help="rewrite the server's gettext template")
    e.add_argument("--check", action="store_true", help="fail if the committed template is out of date")
    args = p.parse_args(argv)
    return {"check": cmd_check, "baseline": cmd_baseline, "extract": cmd_extract}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
