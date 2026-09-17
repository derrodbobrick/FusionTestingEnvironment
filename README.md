# Fusion Testing Environment

The out-of-process control plane for the Fusion manufacturing pipeline: a
standalone web server (`control_service.py`) plus its browser UI
(`control_ui/`), covering three views:

- **Pipeline** (`index.html`) — live status, order ledger, folder shortcuts,
  and process control (start/stop/restart Fusion, pause/resume monitoring).
- **Inspection** (`inspection.html`) — the CNC testing environment: review a
  processed order's output (parameters, layout, G-code cut lengths, exported
  drawings) against its scenario and record pass/fail per test.
- **Generate Scenarios** (`generate.html`) — upload a scenario-definition CSV,
  preview/plan the rows, and write out the test order JSONs.

It runs independently of Fusion itself (which cannot be scripted headlessly)
so it keeps working even if Fusion is hung, wedged on a dialog, or crashed —
see the module docstring in `control_service.py` for why this is a separate
process from the add-in.

## Contents

- **`control_service.py`** — the server. Stdlib-only `ThreadingHTTPServer`;
  no pip installs required.
- **`control_ui/`** — `index.html`, `inspection.html`, `generate.html`,
  `app.js`, `app.css`.
- **`src/`** — the modules the service imports:
  - `config.py` — pipeline paths/run-mode (LOCAL vs VM).
  - `control_channel.py` — the file-based channel used to hand graceful
    pause/resume/stop commands to the add-in.
  - `inspection.py` — test catalogue, per-order output lookup, progress.
  - `scenarios.py` — scenario/series definitions.
  - `scenario_csv.py` — scenario-generation CSV parsing.
  - `users.py` — name-based session selection and admin PIN (no passwords).
  - `gcode.py` — G-code parsing/cut-length analysis used by the Inspection
    tab's G-code view.
  - `deployment.py` — resolves `testing_base`/`artifacts_base` per the
    operator/tester role in `control_config.json`.
- **`control_config.example.json`** — example service config. Copy to
  `control_config.json` and fill in a real `token`, or just delete/omit the
  `token` value and let the service generate one on first run (it writes a
  fresh token back into `control_config.json` if one isn't set).

## Running it

```
python control_service.py                  # bind per control_config.json
python control_service.py --host 0.0.0.0 --port 8765
python control_service.py --print-token     # show the LAN access token
```

Then open `http://localhost:8765` (requests from the machine itself need no
token; anything off-box must append `?token=...` or send an
`Authorization: Bearer` header).

## Security note

`control_config.json` (generated on first run, not checked in) holds the LAN
access token — treat it like a credential. `control_config.example.json` in
this repo has the token blanked out. The service binds to `0.0.0.0` by
default (LAN-exposed) over plain HTTP with no TLS, trusts any request from
`127.0.0.1` with no token, and accepts the token as a URL query parameter as
well as a Bearer header — keep that in mind before exposing it beyond the VM.
