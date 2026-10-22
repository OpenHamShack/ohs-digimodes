#!/bin/sh
# One process, one event loop; SIGTERM reaches Python directly, which halts
# any transmission (the radio app's unkey) before it exits. Anything passed
# on the command line runs instead, so a check can run inside the image
# that will serve.
set -e
cd /opt/digimodes
if [ "$#" -gt 0 ]; then
    exec "$@"
fi
exec python3 -m app.main
