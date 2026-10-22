#!/bin/sh
# Run the store image the way the controller runs it - read-only root, every
# capability dropped, /tmp a tmpfs, /data the only volume - and check its
# front door: nothing without the controller's proxy secret, JSON with it,
# the page under the front door's prefix, and View refused a transmit route.
#
#   tools/store-check.sh [image]   (default localhost/dock360-shack-digimodes:<store version>)
#
# Port 18861 must be free. The container is stopped by the name this script
# gave it; nothing is stopped by pattern. No radio app is configured, so
# nothing can be keyed.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
VERSION=$(sed -n 's/^version: *//p' "$ROOT/store/module.yaml" | head -1)
IMAGE=${1:-localhost/dock360-shack-digimodes:$VERSION}
RUN=${SHACK_HARNESS_DIR:-$HOME/build/m3-digimodes/store-check}
PORT=18861
SECRET=store-check-secret
NAME=shack-digimodes-check-$$

# Rootless: container root is this user, so what it wrote here is ours to remove.
rm -rf "$RUN"
mkdir -p "$RUN/data"
podman run -d --name "$NAME" --read-only --cap-drop ALL --security-opt no-new-privileges \
    --tmpfs /tmp -v "$RUN/data:/data:Z" -p 127.0.0.1:$PORT:8080 \
    -e DOCK360_SHACK_PROXY_SECRET="$SECRET" "$IMAGE" > "$RUN/container.id"
trap 'podman logs "$NAME" > "$RUN/container.log" 2>&1 || true; podman rm -f "$NAME" >/dev/null 2>&1 || true' EXIT

for _ in $(seq 1 40); do
    curl -fs "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 && break
    sleep 0.5
done

fails=0
check() {  # name, expected status, expected content type prefix, curl args...
    name=$1; want=$2; ctype=$3; shift 3
    got=$(curl -s -o "$RUN/body" -w '%{http_code} %{content_type}' "$@")
    case "$got" in
        "$want $ctype"*) echo "  ok    $name ($got)" ;;
        *) echo "  FAIL  $name: wanted $want $ctype, got $got: $(head -c 200 "$RUN/body")"; fails=$((fails + 1)) ;;
    esac
}
H="-H X-OpenHamShack-Proxy-Secret:$SECRET -H X-OpenHamShack-User:check -H X-OpenHamShack-Device:dev_CHECK"
base="http://127.0.0.1:$PORT"
check "healthz answers without the secret" 200 application/json "$base/healthz"
check "state is refused without the secret" 401 application/json "$base/api/state"
check "the page is refused without the secret" 401 application/json "$base/"
check "a wrong secret is refused" 401 application/json -H "X-OpenHamShack-Proxy-Secret: wrong" \
    -H "X-OpenHamShack-User: check" -H "X-OpenHamShack-Level: transmit" "$base/api/state"
# shellcheck disable=SC2086
check "state is JSON with the secret" 200 application/json $H -H "X-OpenHamShack-Level: view" "$base/api/state"
grep -q '"platform": *true' "$RUN/body" || { echo "  FAIL  state does not say platform mode"; fails=$((fails + 1)); }
grep -q '"ok": *true' "$RUN/body" || { echo "  FAIL  the decoder did not load in the image"; fails=$((fails + 1)); }
# shellcheck disable=SC2086
check "the page under a prefix" 200 text/html $H -H "X-OpenHamShack-Level: view" \
    -H "X-Forwarded-Prefix: /shack-digimodes" "$base/"
grep -q '<base href="/shack-digimodes/">' "$RUN/body" || { echo "  FAIL  base href"; fails=$((fails + 1)); }
# shellcheck disable=SC2086
check "View may not call CQ" 403 application/json $H -H "X-OpenHamShack-Level: view" -X POST \
    -H "Content-Type: application/json" -d '{}' "$base/api/cq"
# shellcheck disable=SC2086
check "Transmit is refused by the switch" 409 application/json $H -H "X-OpenHamShack-Level: transmit" -X POST \
    -H "Content-Type: application/json" -d '{}' "$base/api/cq"
# The root really is read-only.
if podman exec "$NAME" sh -c 'touch /opt/digimodes/x' 2>/dev/null; then
    echo "  FAIL  the image root is writable"; fails=$((fails + 1))
else
    echo "  ok    the image root is read-only"
fi
[ "$fails" = 0 ] && echo "store-check: PASS" || echo "store-check: FAIL ($fails)"
exit "$fails"
