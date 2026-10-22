#!/bin/sh
# Build OpenHamShack Digital Modes' store package: a signed .d360mod carrying
# its container image, so a controller with no route out can install it (D12).
#
#   tools/build-store.sh [--allow-dirty]
#
# Output: ~/build/apps/shack-digimodes/shack-digimodes-<version>.d360mod and
# its .sha256 (the version is store/module.yaml's, e.g. 0.1.0-beta.1).
#
# Environment (all optional), as OpenHamShack Flex's script, whose shape this is:
#   SHACK_STORE_OUT    where the package goes (default ~/build/apps/shack-digimodes;
#                      never /tmp, a small shared tmpfs)
#   SHACK_STAGE        scratch for the staged module (default ~/build/m3-digimodes/stage)
#   DOCK360_SRC        a tree of Dock360's feature/ohs-edition (default
#                      ~/build/m3-digimodes/dock360, made with git archive if missing)
#   DOCK360_PY         a Python with Dock360's requirements (default ~/build/venvs/dock360/bin/python)
#   SHACK_MODULES_KEY  the OpenHamShack modules signing key, read in place and never copied
#                      (default ~/.config/openhamshack/keys/ohs-modules.key)
#   PACKAGE_APP        ~/openhamshack-os/tools/package-app.sh
#
# Why a stage: the manifest's build context must be inside the package, and
# the app's code lives beside store/, not in it. So one commit of this
# repository is assembled into a folder - store/'s files at its top, the app
# beside them, minus the desktop packaging and the tests - committed into a
# throwaway repository, and handed to the platform's package-app.sh, which
# clones it, builds the image with podman and signs the package. The stage
# commit and COMMIT inside the package name the source commit.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
OUT=${SHACK_STORE_OUT:-$HOME/build/apps/shack-digimodes}
STAGE=${SHACK_STAGE:-$HOME/build/m3-digimodes/stage}
DOCK360_SRC=${DOCK360_SRC:-$HOME/build/m3-digimodes/dock360}
export DOCK360_PY=${DOCK360_PY:-$HOME/build/venvs/dock360/bin/python}
KEY=${SHACK_MODULES_KEY:-$HOME/.config/openhamshack/keys/ohs-modules.key}
PACKAGE_APP=${PACKAGE_APP:-$HOME/openhamshack-os/tools/package-app.sh}

ALLOW_DIRTY=0
for arg in "$@"; do
    case "$arg" in
        --allow-dirty) ALLOW_DIRTY=1 ;;
        *) echo "usage: $0 [--allow-dirty]" >&2; exit 2 ;;
    esac
done

cd "$ROOT"
if [ "$ALLOW_DIRTY" = 0 ] && [ -n "$(git status --porcelain)" ]; then
    echo "The repository has uncommitted changes; commit them (or --allow-dirty to package HEAD anyway)." >&2
    exit 1
fi
python3 tools/vendor/install.py --check
[ -r "$KEY" ] || { echo "No signing key at $KEY" >&2; exit 1; }
[ -x "$PACKAGE_APP" ] || { echo "No package-app.sh at $PACKAGE_APP" >&2; exit 1; }
if [ ! -d "$DOCK360_SRC/src/dock360" ]; then
    mkdir -p "$DOCK360_SRC"
    git -C "$HOME/dock360" archive feature/ohs-edition | tar -x -C "$DOCK360_SRC"
fi

COMMIT=$(git rev-parse --short=12 HEAD)
rm -rf "$STAGE"
mkdir -p "$STAGE"
git archive HEAD -- . ":(exclude)desktop" ":(exclude)tests" | tar -x -C "$STAGE"
for f in module.yaml Containerfile pad-actions.json pad-layout.json; do
    cp "$STAGE/store/$f" "$STAGE/$f"
done
echo "$COMMIT" > "$STAGE/COMMIT"

git -C "$STAGE" init -q -b main
git -C "$STAGE" -c user.name="OpenHamShack build" -c user.email="build@openhamshack.invalid" add -A
git -C "$STAGE" -c user.name="OpenHamShack build" -c user.email="build@openhamshack.invalid" \
    commit -q -m "shack-digimodes store package from openhamshack-digimodes $COMMIT"

mkdir -p "$OUT"
BUILD_DIR="$(dirname "$STAGE")" "$PACKAGE_APP" "$STAGE" . "$OUT" --key "$KEY" --dock360 "$DOCK360_SRC"
ls -l "$OUT"
