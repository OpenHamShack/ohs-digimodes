#!/bin/sh
# Build OpenHamShack Digital Modes' desktop installers with the shared kit.
#
#   tools/build-desktop.sh stage            assemble desktop/stage-src and the kit's stage
#   tools/build-desktop.sh deb|appimage     the Linux installers (the AppImage needs podman)
#   tools/build-desktop.sh all              stage, deb and appimage
#
# Windows is built on the Windows desktop from the kit's staged folder by
# ~/openhamshack/tools/beta-build/desktop-remote.sh windows digimodes.
# There is no macOS form for the beta.
#
# Environment (all optional), as Flex's script, whose shape this is:
#   SHACK_KIT            the packaging kit (default ~/ohs-libs/packaging)
#   SHACK_DESKTOP_OUT    where installers go (default ~/build/m3-digimodes/out; never the repo)
#   SHACK_DESKTOP_STAGE  the kit's staged folder (default ~/build/m3-digimodes/desktop-stage)
#   SHACK_PY             a Python 3.11+ with pip, for the .deb's vendored pywebview
#                        (default ~/build/venvs/m3-digimodes/bin/python)
#
# The stage is the committed tree (git archive), so an installer never
# carries an edit that is in no commit.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
KIT=${SHACK_KIT:-$HOME/ohs-libs/packaging}
OUT=${SHACK_DESKTOP_OUT:-$HOME/build/m3-digimodes/out}
STAGE=${SHACK_DESKTOP_STAGE:-$HOME/build/m3-digimodes/desktop-stage}
export SHACK_PY=${SHACK_PY:-$HOME/build/venvs/m3-digimodes/bin/python}
SRC="$ROOT/desktop/stage-src"
DESCRIPTOR="$ROOT/desktop/shack-app.toml"

assemble() {
    rm -rf "$SRC"
    mkdir -p "$SRC"
    (cd "$ROOT" && git archive HEAD -- app) | tar -xf - -C "$SRC"
    mkdir -p "$SRC/app/store"
    cp "$ROOT/store/pad-actions.json" "$ROOT/store/pad-layout.json" "$SRC/app/store/"
    sed -n 's/^version: *//p' "$ROOT/store/module.yaml" | head -1 > "$SRC/VERSION"
    # This machine's ohsdigi and the Windows one side by side; the sound card.
    python3 "$ROOT/tools/vendor/install.py" --target "$SRC" --also amd64 --desktop >/dev/null
    rm -rf "$SRC"/*.dist-info
    python3 "$KIT/tools/shackpkg.py" stage "$DESCRIPTOR" "$STAGE"
    echo "kit stage: $STAGE"
}

cmd=${1:-all}
case "$cmd" in
    stage) assemble ;;
    deb) [ -d "$STAGE" ] || assemble; "$KIT/deb/build-deb.sh" "$STAGE" --out "$OUT" ;;
    appimage) [ -d "$STAGE" ] || assemble; "$KIT/appimage/build-appimage.sh" "$STAGE" --out "$OUT" ;;
    all)
        assemble
        mkdir -p "$OUT"
        "$KIT/deb/build-deb.sh" "$STAGE" --out "$OUT"
        "$KIT/appimage/build-appimage.sh" "$STAGE" --out "$OUT"
        ;;
    *) echo "usage: $0 stage|deb|appimage|all" >&2; exit 2 ;;
esac
ls -l "$OUT" 2>/dev/null || true
