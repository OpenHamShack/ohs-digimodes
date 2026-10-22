# OpenHamShack Digital Modes

FT8 and FT4 for the OpenHamShack family: OpenHamShack's replacement for
WSJT-X (PLATFORM-PLAN §1), built on **ohsdigi**, the family's one decoder.
It owns no radio. It listens to the receive audio of whichever app holds
the rig (Icom, Flex, QO-100 NB, Radio) through that app's `api/audio/rx`,
and transmits through that app's transmit lease and `api/audio/tx`, so the
radio app's own checks and the controller's broker apply to every
transmission. In the desktop form a sound card is also a source (receive
only). Decodes go to the station's spots; finished contacts to the one
logbook.

GPL-3.0-or-later (ohsdigi is derived from WSJT-X: decision D13).

## What it does (beta)

- FT8 (15 s) and FT4 (7.5 s): slots from any PCM source placed by the wall
  clock, decoded by `ohsdigi.decode` 14 s (6.4 s) into the slot.
- WSJT-X's two lists: band activity, and the RX frequency (decodes near it,
  and to or from this station). Click a decode to reply.
- WSJT-X's basic QSO sequence, Tx1-Tx6: call CQ and work the strongest
  caller, reply to a CQ or continue from a message to us, RR73/73, give up
  after six unanswered sends, the 6-minute TX watchdog, Halt.
- Logging: the finished contact is offered for logging (editable), stored
  in shack_client's local store first and posted to the controller's
  logbook when there is one (idempotent by uuid; retried if it was away).
- Transmit checks in order: this app's switch, a radio app as the source,
  a callsign and shack_bandplan's verdict for the signal at dial + audio
  offset, the message going out as written, then the radio app's own lease
  answer. Nothing is keyed by this app directly.
- Frequencies: the dataset's `digimodes` presets when it has them; until
  then WSJT-X's default list (app/data/dial-frequencies.json), each entry
  judged by the dataset's licence check.
- The pad (AKP03E, USB keypads): band, mode, RX/TX audio frequency, halt,
  log. Nothing on the pad starts a transmission.

Not in the beta: JS8, a waterfall, Fox/Hound and contest exchanges, auto-CQ
after a contact, PSK Reporter upload from a standalone install, CAT control
(the dial chosen here is not sent to the radio), a sound-card transmit path,
and a macOS build.

## Layout

| Path | What |
|---|---|
| `app/engine.py` | slot clock, `SlotBuffer`, ohsdigi decode/encode |
| `app/audio.py` | sources: radio app, sound card, WAV; `StreamClock` |
| `app/radioapp.py` | the radio app client: rx audio, lease, tx audio |
| `app/sequencer.py`, `app/messages.py` | WSJT-X's QSO flow |
| `app/tx.py` | one transmission: checks, lease, audio, unkey |
| `app/station.py` | profile, licence verdicts, frequency list, logbook |
| `app/platform.py` | proxy identity, route levels, controller services |
| `app/main.py` | the server; `app/desktop.py` the desktop window |
| `app/static/` | the page (no external assets; words in `i18n/en.json`) |
| `store/` | manifest, Containerfile, pad declarations and layout |
| `desktop/shack-app.toml` | the desktop packaging descriptor |
| `tools/` | builds, `fakeradio.py`, `live-check.py`, `store-check.sh`, `i18n.py` |
| `tests/` | pytest; `tests/vectors/` the recorded FT8 slot |

## Running and testing

```sh
python3 -m venv ~/build/venvs/m3-digimodes
~/build/venvs/m3-digimodes/bin/pip install aiohttp==3.12.15 pytest
~/build/venvs/m3-digimodes/bin/python tools/vendor/install.py   # pinned shack-* and ohsdigi
~/build/venvs/m3-digimodes/bin/python -m pytest -q tests        # the suite
~/build/venvs/m3-digimodes/bin/python tools/live-check.py       # a minute, in real time, into the fake
```

A development server: `DIGIMODES_DATA_DIR=~/build/dm RADIO_URL=http://127.0.0.1:18871/
python3 -m app.main` (127.0.0.1:8080), with `tools/fakeradio.py --wav
tests/vectors/ft8-three-signals.wav` as the radio app. Transmit tests key
only `tools/fakeradio.py`.

## Builds

- Store: `tools/build-store.sh` -> `~/build/apps/shack-digimodes/shack-digimodes-<ver>.d360mod`
  (+ `.sha256`), signed with the OpenHamShack modules key; `tools/store-check.sh`
  checks the image read-only (401 without the proxy secret, JSON with it).
- Desktop: `tools/build-desktop.sh all` -> `.deb` and AppImage in `~/build/m3-digimodes/out`;
  Windows: `~/openhamshack/tools/beta-build/desktop-remote.sh windows digimodes`.
- Nightly: the `test-digimodes`, `store-digimodes` and `desktop-digimodes`
  steps of `~/openhamshack/tools/beta-build/beta-build.sh`.
