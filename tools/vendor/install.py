#!/usr/bin/env python3
"""Install the pinned OpenHamShack libraries from this folder, and nothing else.

    python3 tools/vendor/install.py                 # into this interpreter's site-packages
    python3 tools/vendor/install.py --target DIR    # into DIR (a bundle, a scratch tree)
    python3 tools/vendor/install.py --check         # verify the hashes, install nothing

Why a script and not `pip install --require-hashes`: the image has no pip
(Debian's python3 without python3-pip, the way the module has always been
built - see the Containerfile), and a wheel is a zip with nothing to run at
install time, so unpacking it is the whole installation. What pip would add
is the hash check, and that is the part kept: every wheel is compared with
requirements.lock before a byte of it is unpacked, so a wheel swapped in this
folder fails the build instead of shipping.

Digital Modes pins one native wheel, ohs-ohsdigi (the FT8/FT4 decoder),
once per platform. `--also amd64` (the Windows wheel's tag) unpacks a second platform's copy
beside this machine's: the desktop stage is assembled here on Linux and
frozen on Windows, and the two extension modules (ohsdigi.abi3.so,
ohsdigi.pyd) sit side by side in one package, each interpreter importing
its own.
"""

from __future__ import annotations

import argparse
import hashlib
import platform
import re
import sys
import sysconfig
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOCK = HERE / "requirements.lock"
#: Not a runtime library: tools/i18n.py runs the string check from its wheel.
NOT_INSTALLED = {"shack-i18n"}
#: The desktop form's sound card (python-sounddevice, MIT, pure Python over
#: the system's PortAudio): unpacked only with --desktop, so the store image
#: carries no audio-device code it could never use.
DESKTOP_ONLY = {"sounddevice"}
ARCH = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}


def pins() -> list[tuple[str, str, str]]:
    """(distribution, version, sha256) for every line of the lock."""
    out = []
    for line in LOCK.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        m = re.fullmatch(r"([A-Za-z0-9_.-]+)==(\S+)\s+--hash=sha256:([0-9a-f]{64})", line)
        if not m:
            raise SystemExit(f"requirements.lock: cannot read {line!r}")
        out.append(m.groups())
    return out


def wheels_for_this_machine(arch: str | None = None) -> list[tuple[Path, str]]:
    """The wheel file and its pinned hash, one per distribution."""
    arch = arch or ARCH.get(platform.machine().lower(), platform.machine().lower())
    chosen: dict[str, tuple[Path, str]] = {}
    for dist, version, digest in pins():
        stem = dist.replace("-", "_")
        for path in sorted(HERE.glob(f"{stem}-{version}-*.whl")):
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                continue
            native = not path.name.endswith("-none-any.whl")
            if native and not path.name.endswith(f"_{arch}.whl"):
                continue
            chosen[dist] = (path, digest)
    return [chosen[d] for d in sorted(chosen)]


def check() -> list[str]:
    """Problems: a pinned wheel missing, or a wheel here that matches no pin."""
    problems = []
    pinned = {digest for _d, _v, digest in pins()}
    for path in sorted(HERE.glob("*.whl")):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest not in pinned and path.name.split("-")[0].replace("_", "-") not in NOT_INSTALLED:
            problems.append(f"{path.name} matches no hash in requirements.lock")
    found = {p.name.split("-")[0].replace("_", "-") for p, _ in wheels_for_this_machine()}
    for dist in sorted({d for d, _v, _h in pins()} - found):
        problems.append(f"no wheel for {dist} matches its pinned hash on {platform.machine()}")
    return problems


def install(target: Path, also: list[str] = (), desktop: bool = False) -> list[str]:
    target.mkdir(parents=True, exist_ok=True)
    done = []
    extra = [w for a in also for w in wheels_for_this_machine(a) if not w[0].name.endswith("-none-any.whl")]
    for path, _digest in wheels_for_this_machine() + extra:
        dist = path.name.split("-")[0].replace("_", "-")
        if dist in NOT_INSTALLED or (dist in DESKTOP_ONLY and not desktop):
            continue
        with zipfile.ZipFile(path) as z:
            z.extractall(target)
        done.append(path.name)
    return done


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--target", type=Path, default=None)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--desktop", action="store_true", help="also the desktop form's own libraries")
    ap.add_argument("--also", action="append", default=[], metavar="ARCH",
                    help="also unpack this platform's native wheels (amd64: the Windows wheel)")
    args = ap.parse_args()
    problems = check()
    for p in problems:
        print("vendor:", p, file=sys.stderr)
    if args.check:
        return 1 if problems else 0
    if problems:
        return 1
    target = args.target or Path(sysconfig.get_paths()["purelib"])
    for name in install(target, args.also, args.desktop):
        print("installed", name, "->", target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
