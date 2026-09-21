"""
Pipeline Control Service — the out-of-process control plane for the dashboard.

Why this exists
---------------
Everything that can control Fusion from inside Fusion is useless exactly when
you need it most: if the process is wedged, hung on a modal dialog, or past the
thread-leak cliff, the add-in cannot answer a request or kill itself. So the
control plane lives outside the process, like file_sync_service.py, and owns:

    * process lifecycle  — start / kill / restart Fusion360.exe directly
    * graceful commands  — pause / resume / stop, handed to the add-in through
                           control_channel (a file the monitor's timer polls)
    * configuration      — reads and writes drawing_config.json so the cloud
                           model/drawing names can be edited without opening
                           Fusion at all
    * observation        — serves pipeline_status.json, current_progress.json
                           and order_ledger.jsonl, which the add-in already
                           writes for exactly this purpose
    * the dashboard UI   — static HTML served from control_ui/

Fusion itself is NOT containerized and cannot be: it is a Windows GUI
application that needs a real display, a GPU driver, and an interactive
Autodesk sign-in, and it has no headless mode. This service is the piece that
is portable; it talks to Fusion as a local process.

Split of duties with the add-in
-------------------------------
Graceful things go through the add-in because only it knows where a safe
boundary is (between components, between orders). Forceful things are done
here because a wedged Fusion cannot do them. See src/control_channel.py.

Usage
-----
    python control_service.py                  # bind per control_config.json
    python control_service.py --host 0.0.0.0 --port 8765
    python control_service.py --print-token    # show the LAN access token

Standard library only — no pip install on the VM.
"""

import argparse
import html
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'src'))

import config as pipeline_config       # noqa: E402  (path set above)
import control_channel                 # noqa: E402
import inspection                      # noqa: E402
import scenarios                       # noqa: E402
import scenario_csv                    # noqa: E402
import users                           # noqa: E402
import gcode as gcode_parser           # noqa: E402

UI_DIR = ROOT / 'control_ui'
CONTROL_CONFIG = ROOT / 'control_config.json'
DRAWING_CONFIG = ROOT / 'drawing_config.json'
AUTOSTART_CONFIG = ROOT / 'autostart_config.json'

SERVICE_VERSION = '1.0.0'

# How long after issuing a graceful command we keep reporting it as pending
# before deciding the add-in is not answering. Generous: the monitor polls
# every ~9 s, and during a heavy component the timer thread can be starved.
ACK_TIMEOUT_SECONDS = 90


# --------------------------------------------------------------------- config

def load_control_config() -> dict:
    """Service settings, creating the file with a fresh token on first run.

    The token is generated rather than defaulted so an unconfigured service is
    never reachable with a guessable credential once it is bound to the LAN.
    """
    defaults = {
        '_comment': ('Control service settings. host 0.0.0.0 exposes the '
                     'dashboard to the LAN; requests from other machines must '
                     'present the token. Requests from the VM itself '
                     '(127.0.0.1) are trusted without it so the console always '
                     'works.'),
        'host': '0.0.0.0',
        'port': 8765,
        'token': '',
        'run_mode': 'VM',
    }
    cfg = dict(defaults)
    if CONTROL_CONFIG.exists():
        try:
            cfg.update(json.loads(CONTROL_CONFIG.read_text(encoding='utf-8-sig')))
        except Exception as e:
            print(f'WARNING: could not read {CONTROL_CONFIG.name}: {e}')

    if not cfg.get('token'):
        cfg['token'] = secrets.token_urlsafe(24)
        try:
            CONTROL_CONFIG.write_text(json.dumps(cfg, indent=2), encoding='utf-8')
            print(f'Generated a new access token in {CONTROL_CONFIG.name}')
        except Exception as e:
            print(f'WARNING: could not persist token: {e}')
    return cfg


def _read_json(path: Path, default=None):
    try:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding='utf-8-sig'))
    except Exception:
        return default


def _write_json_atomic(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(payload, indent=2), encoding='utf-8')
    os.replace(str(tmp), str(path))


# ------------------------------------------------------------ process control

_csv_cache = {}
_csv_lock = threading.Lock()

# ------------------------------------------------------------- admin sessions
#
# Unlocking admin grants a ticket held in this process's memory only. That is
# deliberate: each tester runs their own copy of the service, so unlocking on
# one machine cannot unlock anybody else's, and restarting the service locks it
# again. Tickets expire so an unattended browser does not leave setup open.
#
# This is a gate against wandering in by accident, not a security boundary --
# see the note at the top of src/users.py.

ADMIN_SESSION_SECONDS = 8 * 3600
ADMIN_LOCKOUT_AFTER = 8          # wrong PINs before a pause
ADMIN_LOCKOUT_SECONDS = 60

_admin_sessions = {}             # ticket -> {'expires': ts, 'who': name}
_admin_lock = threading.Lock()
_admin_failures = {'count': 0, 'until': 0.0}


def _admin_new_session(who: str) -> dict:
    ticket = secrets.token_urlsafe(18)
    expires = time.time() + ADMIN_SESSION_SECONDS
    with _admin_lock:
        for key, s in list(_admin_sessions.items()):
            if s['expires'] < time.time():
                _admin_sessions.pop(key, None)
        _admin_sessions[ticket] = {'expires': expires, 'who': who or ''}
    return {'ticket': ticket, 'expires_at': datetime.fromtimestamp(expires)
            .isoformat(sep=' ', timespec='seconds')}


def _admin_valid(ticket: str) -> bool:
    with _admin_lock:
        s = _admin_sessions.get((ticket or '').strip())
        if not s:
            return False
        if s['expires'] < time.time():
            _admin_sessions.pop(ticket, None)
            return False
    return True

_proc_cache = {'at': 0.0, 'value': []}
_proc_lock = threading.Lock()

# Each probe spawns a PowerShell process. The dashboard polls every 3 s per
# open tab, and this VM is already thread-constrained enough to need a restart
# cycle -- so the control plane must not become its own source of churn. One
# probe every 2.5 s regardless of how many tabs are watching.
PROC_CACHE_TTL = 2.5


def find_fusion_processes() -> list:
    """PIDs, thread counts and RAM for every running Fusion360.exe.

    Cached briefly (see PROC_CACHE_TTL) and shared across requests.
    """
    now = time.time()
    with _proc_lock:
        if now - _proc_cache['at'] < PROC_CACHE_TTL:
            return _proc_cache['value']

    value = _probe_fusion_processes()
    with _proc_lock:
        _proc_cache['at'] = time.time()
        _proc_cache['value'] = value
    return value


def invalidate_process_cache():
    """Drop the cached probe so the next status poll reflects an action we just
    took, instead of showing the pre-kill process for another couple of seconds."""
    with _proc_lock:
        _proc_cache['at'] = 0.0


def _probe_fusion_processes() -> list:
    """Uncached probe. Uses PowerShell rather than a Python process library so
    the service keeps its no-dependency promise."""
    ps = ("Get-Process Fusion360 -ErrorAction SilentlyContinue | "
          "Select-Object Id,@{n='Threads';e={$_.Threads.Count}},"
          "@{n='MemoryMB';e={[math]::Round($_.WorkingSet64/1MB)}},"
          "@{n='StartTime';e={$_.StartTime.ToString('o')}} | ConvertTo-Json -Compress")
    try:
        out = subprocess.run(
            ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', ps],
            capture_output=True, text=True, timeout=20,
            creationflags=0x08000000,  # CREATE_NO_WINDOW
        ).stdout.strip()
        if not out:
            return []
        data = json.loads(out)
        return data if isinstance(data, list) else [data]
    except Exception:
        return []


def find_fusion_exe() -> str:
    """Newest Fusion360.exe under the webdeploy tree.

    process_health.get_fusion_exe_path() cannot be reused here: it calls
    GetModuleFileNameW(None), which returns the CURRENT process — correct
    inside Fusion, but it would return python.exe here.
    """
    base = Path(os.environ.get('LOCALAPPDATA', '')) / 'Autodesk' / 'webdeploy' / 'production'
    if not base.is_dir():
        return ''
    candidates = list(base.glob('*/Fusion360.exe'))
    if not candidates:
        return ''
    return str(max(candidates, key=lambda p: p.stat().st_mtime))


def _run_detached_ps(script: str) -> bool:
    """Run a PowerShell script in the background so it outlives this request.

    CREATE_NO_WINDOW only -- do NOT add DETACHED_PROCESS. With DETACHED_PROCESS
    set, powershell.exe gets no console, exits 0 immediately, and NEVER RUNS THE
    SCRIPT, while Popen still reports success. Measured 2026-08-25:

        CREATE_NO_WINDOW|DETACHED  -> Popen OK, rc=0, script_ran=False
        CREATE_NO_WINDOW           -> Popen OK, rc=0, script_ran=True
        DETACHED                   -> Popen OK, rc=0, script_ran=False

    That silent no-op is what made the dashboard's Start button report
    "Launching Fusion360.exe" while nothing happened. CREATE_NO_WINDOW alone
    still keeps the child alive after this process exits -- Windows does not
    terminate children with their parent -- so nothing is lost by dropping it.
    """
    try:
        import tempfile
        path = Path(tempfile.gettempdir()) / f'pipeline_control_{int(time.time()*1000)}.ps1'
        path.write_text(script, encoding='utf-8-sig')
        subprocess.Popen(
            ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(path)],
            creationflags=0x08000000,  # CREATE_NO_WINDOW
            close_fds=True,
        )
        return True
    except Exception:
        return False


def start_fusion() -> tuple:
    if find_fusion_processes():
        return False, 'Fusion is already running'
    exe = find_fusion_exe()
    if not exe:
        return False, 'Could not locate Fusion360.exe under the webdeploy folder'
    # Clear crash-recovery residue first: that prompt is modal and appears
    # BEFORE add-ins load, so it would block autostart with nobody at the VM.
    script = f"""
$fusionData = Join-Path $env:LOCALAPPDATA 'Autodesk\\Autodesk Fusion 360'
Get-ChildItem -Path $fusionData -Filter CrashRecovery -Recurse -Directory -ErrorAction SilentlyContinue | ForEach-Object {{
    Get-ChildItem $_.FullName -Force -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
}}
Start-Process -FilePath '{exe}'
"""
    if not _run_detached_ps(script):
        return False, 'Failed to spawn the launcher'

    # Confirm the process actually appears rather than reporting success on the
    # strength of having spawned a helper. Reporting "Launching Fusion360.exe"
    # when nothing started is exactly how the DETACHED_PROCESS bug above stayed
    # invisible. Fusion's process shows up within a few seconds even though its
    # UI takes far longer, so a short wait is enough to tell the difference.
    deadline = time.time() + 25
    while time.time() < deadline:
        time.sleep(2)
        invalidate_process_cache()
        procs = find_fusion_processes()
        if procs:
            return True, (f'Fusion is starting (pid {procs[0].get("Id")}). '
                          f'The add-in loads a little after the window appears.')
    return False, ('Launcher ran but no Fusion process appeared within 25s. '
                   f'Check that {exe} runs when started manually.')


def kill_fusion(force: bool = True) -> tuple:
    procs = find_fusion_processes()
    if not procs:
        return False, 'Fusion is not running'
    pids = [p['Id'] for p in procs]
    # Graceful WM_CLOSE first, force only if it does not go. Matches the
    # sequence process_health.spawn_restart_helper() already uses.
    lines = []
    for pid in pids:
        lines.append(f"taskkill /PID {pid} | Out-Null")
    lines.append("Start-Sleep -Seconds 20")
    for pid in pids:
        lines.append(
            f"if (Get-Process -Id {pid} -ErrorAction SilentlyContinue) "
            f"{{ taskkill /F /PID {pid} | Out-Null }}")
    if _run_detached_ps('\n'.join(lines)):
        return True, f'Closing Fusion (pid {", ".join(str(p) for p in pids)})'
    return False, 'Failed to spawn the kill helper'


def restart_fusion() -> tuple:
    exe = find_fusion_exe()
    if not exe:
        return False, 'Could not locate Fusion360.exe'
    procs = find_fusion_processes()
    pids = [p['Id'] for p in procs]

    parts = []
    for pid in pids:
        parts.append(f"taskkill /PID {pid} | Out-Null")
    if pids:
        pid_list = ','.join(str(p) for p in pids)
        parts.append(f"""
$deadline = (Get-Date).AddSeconds(120)
while ((Get-Date) -lt $deadline -and (Get-Process -Id {pid_list} -ErrorAction SilentlyContinue)) {{
    Start-Sleep -Seconds 2
}}
Get-Process -Id {pid_list} -ErrorAction SilentlyContinue | ForEach-Object {{ taskkill /F /PID $_.Id | Out-Null }}
while (Get-Process -Id {pid_list} -ErrorAction SilentlyContinue) {{ Start-Sleep -Seconds 2 }}
Start-Sleep -Seconds 15
""")
    parts.append(f"""
$fusionData = Join-Path $env:LOCALAPPDATA 'Autodesk\\Autodesk Fusion 360'
Get-ChildItem -Path $fusionData -Filter CrashRecovery -Recurse -Directory -ErrorAction SilentlyContinue | ForEach-Object {{
    Get-ChildItem $_.FullName -Force -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
}}
Start-Process -FilePath '{exe}'
""")
    if _run_detached_ps('\n'.join(parts)):
        return True, 'Restarting Fusion'
    return False, 'Failed to spawn the restart helper'


# ----------------------------------------------------------------- data views

def folder_targets() -> list:
    """Every folder worth a link on the dashboard, with live counts."""
    c = pipeline_config
    entries = [
        ('Order dropbox (watched)', c.ORDER_DROPBOX, '*.json'),
        ('Order processing', c.ORDER_PROCESSING, '*.json'),
        ('Order completed', c.ORDER_COMPLETED, '*.json'),
        ('Order failed', c.ORDER_FAILED, '*.json'),
        ('Output — G-code', c.OUTPUT_GCODE, '*'),
        ('Output — models', c.OUTPUT_MODELS, '*'),
        ('Output — parameters', c.OUTPUT_PARAMETERS, '*'),
        ('Output — logs', c.OUTPUT_LOGS, '*'),
        ('Dashboard data', Path(c.OUTPUT_BASE) / 'dashboard', '*'),
        ('Add-in folder', ROOT, '*'),
        ('Add-in logs', ROOT / 'logs', '*.log'),
    ]
    out = []
    for label, path, pattern in entries:
        p = Path(path)
        item = {'label': label, 'path': str(p), 'exists': False, 'count': None}
        try:
            # A network share can hang; keep the probe cheap and tolerant.
            item['exists'] = p.exists()
            if item['exists']:
                item['count'] = sum(1 for _ in p.glob(pattern))
        except Exception:
            pass
        out.append(item)
    return out


def read_ledger(limit: int = 40) -> list:
    path = Path(pipeline_config.OUTPUT_BASE) / 'dashboard' / 'order_ledger.jsonl'
    rows = []
    try:
        if path.exists():
            lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
            for line in lines[-limit:]:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
    except Exception:
        pass
    return list(reversed(rows))


def newest_log() -> Path:
    try:
        logs = sorted((ROOT / 'logs').glob('pipeline_*.log'),
                      key=lambda p: p.stat().st_mtime, reverse=True)
        return logs[0] if logs else None
    except Exception:
        return None


def read_log_tail(lines: int = 200) -> dict:
    path = newest_log()
    if not path:
        return {'file': None, 'lines': []}
    try:
        # Read only the tail: these logs reach multiple MB in a session.
        with open(path, 'rb') as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            block = min(size, max(4096, lines * 400))
            f.seek(size - block)
            text = f.read().decode('utf-8', errors='replace')
        return {'file': str(path), 'lines': text.splitlines()[-lines:]}
    except Exception as e:
        return {'file': str(path), 'lines': [f'<could not read log: {e}>']}


def build_status() -> dict:
    """Everything the dashboard polls, merged into one payload."""
    dash = Path(pipeline_config.OUTPUT_BASE) / 'dashboard'
    status = _read_json(dash / 'pipeline_status.json', {}) or {}
    progress = _read_json(dash / 'current_progress.json', {}) or {}
    procs = find_fusion_processes()
    ack = control_channel.read_ack(pipeline_config.OUTPUT_BASE)
    pending = _read_json(dash / 'control_command.json', {}) or {}

    # A heartbeat is only meaningful if it is recent — the add-in writes one
    # every ~15 s, so a minutes-old file means Fusion died without saying so
    # and the reported state ('monitoring') is a lie.
    age = None
    if status.get('timestamp'):
        age = time.time() - float(status['timestamp'])

    fusion_running = bool(procs)
    if not fusion_running:
        effective = 'not_running'
    elif age is not None and age > 120:
        effective = 'stale'
    else:
        effective = status.get('state', 'unknown')

    command_state = None
    if pending:
        acked = ack.get('id') == pending.get('id')
        if acked:
            command_state = {'state': 'acked', **ack}
        elif time.time() - pending.get('requested_at', 0) > ACK_TIMEOUT_SECONDS:
            command_state = {'state': 'no_response', **pending}
        else:
            command_state = {'state': 'pending', **pending}

    return {
        'service_version': SERVICE_VERSION,
        'server_time': datetime.now().isoformat(timespec='seconds'),
        'fusion_running': fusion_running,
        'processes': procs,
        'effective_state': effective,
        'heartbeat_age_seconds': round(age) if age is not None else None,
        'status': status,
        'progress': progress,
        'last_command': command_state,
        'run_mode': pipeline_config.RUN_MODE,
        'hold_on_start': control_channel.read_pause_state(pipeline_config.OUTPUT_BASE),
        'paths': {
            'output_base': str(pipeline_config.OUTPUT_BASE),
            'dropbox': str(pipeline_config.ORDER_DROPBOX),
        },
    }


# -------------------------------------------------------------- drawing config

# Guards the config editor. The add-in looks these documents up by exact name
# in the Fusion Hub, so a stray newline or tab silently breaks resolution and
# costs a whole run of drawings — exactly the failure this dashboard exists to
# make visible.
CONFIG_STRING_FIELDS = ('project_name', 'folder_name', 'door_model',
                        'door_drawing', 'panel_model', 'panel_drawing')
CONFIG_MAP_FIELDS = ('stile_models', 'stile_drawings')


def validate_drawing_config(payload: dict) -> list:
    errors = []
    if not isinstance(payload, dict):
        return ['Payload must be a JSON object']

    for field in ('project_name', 'folder_name', 'door_model', 'panel_model'):
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            errors.append(f'"{field}" is required')

    for field in CONFIG_STRING_FIELDS:
        value = payload.get(field)
        if value is None:
            continue
        if not isinstance(value, str):
            errors.append(f'"{field}" must be text')
        elif value != value.strip():
            errors.append(f'"{field}" has leading or trailing whitespace')
        elif '\n' in value or '\t' in value:
            errors.append(f'"{field}" contains a newline or tab')

    for field in CONFIG_MAP_FIELDS:
        mapping = payload.get(field)
        if mapping is None:
            continue
        if not isinstance(mapping, dict):
            errors.append(f'"{field}" must be an object of series code -> name')
            continue
        for key, value in mapping.items():
            if not re.fullmatch(r'\d{4}', str(key)):
                errors.append(f'"{field}" key "{key}" should be a 4-digit series code')
            if not isinstance(value, str) or not value.strip():
                errors.append(f'"{field}"["{key}"] must be a non-empty name')
            elif value != value.strip():
                errors.append(f'"{field}"["{key}"] has leading or trailing whitespace')
    return errors


def config_load_state() -> dict:
    """Whether the running session actually resolved against the config on disk.

    setup_cloud_models() runs once, at monitor start. A config saved afterwards
    changes nothing until the next start, and that gap is silent and expensive:
    a corrected config sat unused for three days in August 2026 while every
    order kept resolving against the names loaded days earlier -- doors and
    panels fell back to local models and only stile drawings came out.

    The add-in stamps config_loaded.json with the config mtime it read. If that
    differs from the file's mtime now, the edit is pending a restart.
    """
    stamp = _read_json(Path(pipeline_config.OUTPUT_BASE) / 'dashboard' / 'config_loaded.json')
    try:
        disk_mtime = DRAWING_CONFIG.stat().st_mtime if DRAWING_CONFIG.exists() else None
    except Exception:
        disk_mtime = None

    if not stamp:
        # No stamp: either the add-in predates this, or it has not started
        # since. Either way we cannot claim the config is live.
        return {'known': False, 'stale': None, 'disk_mtime': disk_mtime}

    loaded_mtime = stamp.get('config_mtime')
    stale = (disk_mtime is not None and loaded_mtime is not None
             and abs(disk_mtime - loaded_mtime) > 1)
    return {
        'known': True,
        'stale': stale,
        'loaded_at_human': stamp.get('loaded_at_human'),
        'resolved': stamp.get('resolved'),
        'disk_mtime': disk_mtime,
        'loaded_mtime': loaded_mtime,
    }


def hub_snapshot() -> dict:
    """The hub listing the add-in captured, for the model/drawing pickers.

    The service cannot query the hub itself — app.data is a Fusion API call
    that exists only inside Fusion. So the add-in enumerates it on request
    (control_channel 'refresh_hub') and writes hub_snapshot.json here. Reading
    a file means the pickers still work with Fusion busy or shut down; they
    just show whatever the last scan saw, with its age on display so a stale
    list is never mistaken for a live one.
    """
    path = Path(pipeline_config.OUTPUT_BASE) / 'dashboard' / 'hub_snapshot.json'
    data = _read_json(path)
    if not data:
        return {'available': False}

    files = data.get('files', [])
    age = None
    if data.get('captured_at'):
        age = round(time.time() - float(data['captured_at']))

    return {
        'available': True,
        'captured_at_human': data.get('captured_at_human'),
        'age_seconds': age,
        'project_name': data.get('project_name'),
        'projects': data.get('projects', []),
        'folders': data.get('folders', []),
        'models': sorted({f['name'] for f in files if f.get('type') == 'model'}),
        'drawings': sorted({f['name'] for f in files if f.get('type') == 'drawing'}),
        'files': files,
        'error': data.get('error'),
    }


# ----------------------------------------------------------------- HTTP server

class Handler(BaseHTTPRequestHandler):
    server_version = f'PipelineControl/{SERVICE_VERSION}'
    control_config = {}

    def log_message(self, fmt, *args):
        # Default logging writes a line per poll; the dashboard polls every
        # few seconds, so this would bury anything useful.
        pass

    # ---- auth

    def _client_is_local(self) -> bool:
        return self.client_address[0] in ('127.0.0.1', '::1', 'localhost')

    def _authorized(self) -> bool:
        """Local requests are trusted; anything off-box must present the token.

        The VM console needs to work without someone typing a token into a
        kiosk browser, while a LAN-bound port stays closed to everyone else.
        """
        if self._client_is_local():
            return True
        token = self.control_config.get('token', '')
        if not token:
            return False

        auth = self.headers.get('Authorization', '')
        if auth.startswith('Bearer ') and secrets.compare_digest(auth[7:], token):
            return True
        cookie = self.headers.get('Cookie', '')
        for part in cookie.split(';'):
            name, _, value = part.strip().partition('=')
            if name == 'pipeline_token' and secrets.compare_digest(value, token):
                return True
        query = urllib.parse.urlparse(self.path).query
        supplied = urllib.parse.parse_qs(query).get('token', [''])[0]
        return bool(supplied) and secrets.compare_digest(supplied, token)

    # ---- responses

    def _send(self, code, body: bytes, content_type='application/json',
              extra_headers=None):
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # dashboard tab closed mid-poll

    def _json(self, payload, code=200):
        self._send(code, json.dumps(payload, default=str).encode('utf-8'))

    def _read_body(self) -> dict:
        try:
            length = int(self.headers.get('Content-Length', 0))
            if not length:
                return {}
            return json.loads(self.rfile.read(length).decode('utf-8'))
        except Exception:
            return {}

    def _read_raw_body(self) -> bytes:
        """Drain exactly Content-Length bytes from the socket.

        Must happen before ANY response is written on a raw-body route (file
        uploads use this instead of _read_body(), which assumes JSON). An
        admin gate or a size limit that responds first and never reads this
        closes the connection while the browser may still be mid-upload of a
        multi-MB file; the client sees that as a bare "Failed to fetch" with
        no message, not the real error.
        """
        try:
            length = int(self.headers.get('Content-Length', 0))
        except ValueError:
            length = 0
        if length <= 0:
            return b''
        try:
            return self.rfile.read(length)
        except Exception:
            return b''

    # ---- routing

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if not self._authorized():
            self._json({'error': 'unauthorized',
                        'hint': 'append ?token=... or send an Authorization: Bearer header'},
                       401)
            return

        if path in ('/', '/index.html'):
            self._serve_page(parsed, 'index.html')
        elif path in ('/inspection', '/inspection.html'):
            self._serve_page(parsed, 'inspection.html')
        elif path in ('/generate', '/generate.html'):
            self._serve_page(parsed, 'generate.html')
        elif path in ('/results', '/results.html'):
            self._serve_page(parsed, 'results.html')
        elif path in ('/app.css', '/app.js'):
            self._serve_asset(path.lstrip('/'))
        elif path == '/api/status':
            self._json(build_status())
        elif path == '/api/folders':
            self._json({'folders': folder_targets(),
                        'can_open_locally': self._client_is_local()})
        elif path == '/api/orders':
            self._json({'orders': read_ledger()})
        elif path == '/api/logs':
            n = int(urllib.parse.parse_qs(parsed.query).get('lines', ['200'])[0])
            self._json(read_log_tail(min(n, 2000)))
        elif path == '/api/tests':
            self._json({'tests': inspection.list_tests(),
                        'catalogue': inspection.TESTS})
        elif path == '/api/deployment':
            import deployment
            self._json(deployment.describe())
        elif path == '/api/users':
            self._json({'users': users.list_users(
                include_inactive=self._is_admin()),
                'admin_configured': users.admin_configured(),
                'is_admin': self._is_admin()})
        elif path == '/api/progress':
            self._json({'people': inspection.progress_by_user()})
        elif path == '/api/series':
            self._json({'series': scenarios.list_series()})
        elif path == '/api/scenarios':
            q = urllib.parse.parse_qs(parsed.query)
            # An admin is setting the work up and needs to see all of it; a
            # tester is doing the work and sees what is theirs, unless they ask
            # for everything.
            mine = (q.get('assigned_to', [''])[0] or '').strip()
            if q.get('all', [''])[0] in ('1', 'true'):
                mine = ''
            self._json({'scenarios': scenarios.list_scenarios(
                q.get('series', [None])[0], assigned_to=mine or None)})
        elif path == '/api/scenario':
            q = urllib.parse.parse_qs(parsed.query)
            try:
                sc = scenarios.load_scenario(q.get('id', [''])[0])
            except Exception as e:
                self._json({'error': str(e)}, 404)
                return
            # Everything the scenario view needs: its orders, the runs
            # available for each, and the inspections already recorded.
            sc['order_runs'] = {o: inspection.list_output_runs(o) for o in sc['orders']}
            sc['tests'] = [t for t in inspection.list_tests()
                           if t.get('scenario_id') == sc['id']]
            self._json(sc)
        elif path == '/api/tests/runs':
            q = urllib.parse.parse_qs(parsed.query)
            self._json({'runs': inspection.list_output_runs(
                (q.get('order', [''])[0] or '').strip().upper())})
        elif path == '/api/generate/rows':
            q = urllib.parse.parse_qs(parsed.query)
            key = q.get('key', [''])[0]
            with _csv_lock:
                parsedcsv = _csv_cache.get(key)
            if not parsedcsv:
                self._json({'error': 'upload expired; drop the CSV again'}, 404)
                return
            try:
                offset = int(q.get('offset', ['0'])[0])
                limit = min(int(q.get('limit', ['200'])[0]), 1000)
            except ValueError:
                offset, limit = 0, 200
            self._json({'rows': parsedcsv['rows'][offset:offset + limit],
                        'offset': offset, 'total': parsedcsv['row_count']})
        elif path == '/api/tests/orders':
            self._json({'orders': inspection.known_orders()})
        elif path == '/api/tests/detail':
            q = urllib.parse.parse_qs(parsed.query)
            try:
                self._json(inspection.test_detail(q.get('id', [''])[0]))
            except FileNotFoundError as e:
                self._json({'error': str(e)}, 404)
        elif path == '/api/tests/config':
            self._serve_component_config(urllib.parse.parse_qs(parsed.query))
        elif path == '/api/tests/layout':
            self._serve_layout(urllib.parse.parse_qs(parsed.query))
        elif path == '/api/tests/gcode':
            self._serve_parsed_gcode(urllib.parse.parse_qs(parsed.query))
        elif path == '/api/tests/doc':
            self._serve_test_document(urllib.parse.parse_qs(parsed.query))
        elif path == '/api/tests/docinfo':
            self._serve_document_geometry(urllib.parse.parse_qs(parsed.query))
        elif path == '/api/tests/markup':
            self._serve_markup(urllib.parse.parse_qs(parsed.query))
        elif path == '/api/config':
            self._json({'config': _read_json(DRAWING_CONFIG, {}),
                        'autostart': _read_json(AUTOSTART_CONFIG, {}),
                        'hub': hub_snapshot(),
                        'load_state': config_load_state()})
        else:
            self._json({'error': 'not found'}, 404)

    def do_POST(self):
        if not self._authorized():
            self._json({'error': 'unauthorized'}, 401)
            return

        path = urllib.parse.urlparse(self.path).path
        # These carry a file body, not JSON. Read it here, unconditionally,
        # before anything downstream (an admin gate, a size limit) can reject
        # the request without draining the socket -- see _read_raw_body().
        raw_body_paths = ('/api/tests/layout', '/api/generate/parse')
        raw_body = self._read_raw_body() if path in raw_body_paths else b''
        body = {} if path in raw_body_paths else self._read_body()

        if path == '/api/control':
            self._handle_control(body)
        elif path == '/api/hub/refresh':
            self._handle_hub_refresh(body)
        elif path == '/api/pause-state':
            self._handle_pause_state(body)
        elif path == '/api/generate/parse':
            self._handle_csv_parse(urllib.parse.parse_qs(
                urllib.parse.urlparse(self.path).query), raw_body)
        elif path == '/api/generate/plan':
            self._handle_csv_plan(body)
        elif path == '/api/generate/write':
            self._handle_csv_write(body)
        elif path == '/api/tests/layout':
            # The layout is scenario setup, not inspection, so it sits behind
            # the same gate as everything else that shapes the work.
            if not self._require_admin():
                return
            self._handle_layout_upload(urllib.parse.parse_qs(
                urllib.parse.urlparse(self.path).query), raw_body)
        elif path == '/api/admin/unlock':
            self._handle_admin_unlock(body)
        elif path == '/api/admin/lock':
            with _admin_lock:
                _admin_sessions.pop((body.get('ticket') or '').strip(), None)
            self._json({'ok': True})
        elif path == '/api/admin/pin':
            self._handle_admin_pin(body)
        elif path == '/api/users/create':
            if not self._require_admin():
                return
            self._simple(lambda: users.add_user(
                body.get('name', ''), body.get('note', '')))
        elif path == '/api/users/update':
            if not self._require_admin():
                return
            self._simple(lambda: users.update_user(
                body.get('id', ''), body.get('name'), body.get('note'),
                body.get('active')))
        elif path == '/api/users/retire':
            if not self._require_admin():
                return
            self._simple(lambda: users.remove_user(body.get('id', '')))
        elif path == '/api/series/create':
            if not self._require_admin():
                return
            self._simple(lambda: scenarios.add_series(
                body.get('id', ''), body.get('name', ''), body.get('description', '')))
        elif path == '/api/series/delete':
            if not self._require_admin():
                return
            self._simple(lambda: scenarios.delete_series(body.get('id', '')))
        elif path == '/api/scenarios/create':
            if not self._require_admin():
                return
            self._simple(lambda: scenarios.create_scenario(
                body.get('series_id', ''), body.get('name', ''),
                body.get('orders', []), body.get('description', ''),
                assigned_to=body.get('assigned_to')))
        elif path == '/api/scenarios/update':
            if not self._require_admin():
                return
            self._simple(lambda: scenarios.update_scenario(
                body.get('id', ''), body.get('name'), body.get('description'),
                body.get('orders'), assigned_to=body.get('assigned_to')))
        elif path == '/api/scenarios/assign':
            if not self._require_admin():
                return
            self._simple(lambda: scenarios.update_scenario(
                body.get('id', ''), assigned_to=body.get('assigned_to') or []))
        elif path == '/api/scenarios/delete':
            if not self._require_admin():
                return
            self._simple(lambda: scenarios.delete_scenario(body.get('id', '')))
        elif path == '/api/tests/create':
            self._handle_test_create(body)
        elif path == '/api/tests/result':
            self._handle_test_result(body)
        elif path == '/api/tests/markup':
            self._handle_markup_save(body)
        elif path == '/api/config':
            self._handle_config_save(body)
        elif path == '/api/open-folder':
            self._handle_open_folder(body)
        else:
            self._json({'error': 'not found'}, 404)

    # ---- handlers

    def _handle_control(self, body):
        action = str(body.get('action', '')).lower()

        if action in control_channel.GRACEFUL_COMMANDS:
            if not find_fusion_processes():
                self._json({'ok': False,
                            'message': 'Fusion is not running — nothing to '
                                       f'{action}. Use Start.'}, 409)
                return
            try:
                cmd = control_channel.send(action, pipeline_config.OUTPUT_BASE)
            except Exception as e:
                self._json({'ok': False, 'message': str(e)}, 400)
                return
            self._json({'ok': True, 'queued': cmd,
                        'message': f'"{action}" sent to the add-in; it is applied '
                                   f'at the next safe boundary'})
            return

        if action == 'start':
            ok, msg = start_fusion()
        elif action == 'kill':
            ok, msg = kill_fusion()
        elif action == 'restart':
            ok, msg = restart_fusion()
        else:
            self._json({'ok': False, 'message': f'Unknown action: {action}'}, 400)
            return

        # The helper runs detached, so the process list changes moments from
        # now; drop the cache so the dashboard's next poll shows the truth.
        invalidate_process_cache()
        self._json({'ok': ok, 'message': msg}, 200 if ok else 409)

    def _handle_pause_state(self, body):
        """Set the persisted pause flag directly, without needing the add-in.

        This is the piece that makes a quiet start possible: the add-in runs at
        Fusion startup and begins taking orders immediately, so if the flag
        could only be set through the running add-in there would be no way to
        launch Fusion for configuration work without it also draining the
        queue. Setting it here, with Fusion closed, means the next launch comes
        up held.
        """
        paused = bool(body.get('paused'))
        control_channel.write_pause_state(
            paused, pipeline_config.OUTPUT_BASE,
            reason='set from dashboard while Fusion was not running'
            if not find_fusion_processes() else 'set from dashboard')
        self._json({'ok': True, 'paused': paused,
                    'message': ('Next Fusion start will be held — no orders '
                                'will be taken until you press Resume'
                                if paused else
                                'Next Fusion start will process orders normally')})

    def _handle_hub_refresh(self, body):
        """Ask the add-in to re-enumerate the hub.

        Requires a running Fusion — this is the one dashboard action that
        genuinely cannot be served without it, since only the add-in can reach
        app.data. Says so plainly rather than appearing to work and leaving a
        stale list on screen.
        """
        if not find_fusion_processes():
            self._json({'ok': False,
                        'message': 'Fusion must be running to scan the hub — '
                                   'only the add-in can reach the Fusion API. '
                                   'Start Fusion, then scan again.'}, 409)
            return
        project = body.get('project') or None
        try:
            cmd = control_channel.send('refresh_hub', pipeline_config.OUTPUT_BASE,
                                       args={'project': project} if project else None)
        except Exception as e:
            self._json({'ok': False, 'message': str(e)}, 400)
            return
        self._json({'ok': True, 'queued': cmd,
                    'message': 'Hub scan requested. It runs at the next idle '
                               'moment — if an order is in flight, after it '
                               'finishes.'})

    def _simple(self, fn):
        """Run a store operation and report it uniformly.

        ValueError carries the user-facing reasons (duplicate series, scenario
        with no orders, deleting a series that still has scenarios), so it maps
        to 400 rather than a 500 that hides the explanation.
        """
        try:
            return self._json({'ok': True, 'result': fn()})
        except users.ReadOnlyShare as e:
            # Not a bug and not the caller's mistake: this account may read the
            # share but not change it. Say which, so nobody hunts a fault.
            return self._json({'ok': False, 'read_only': True, 'message': str(e)}, 403)
        except ValueError as e:
            return self._json({'ok': False, 'message': str(e)}, 400)
        except FileNotFoundError as e:
            return self._json({'ok': False, 'message': str(e)}, 404)
        except Exception as e:
            return self._json({'ok': False, 'message': str(e)}, 500)

    def _handle_csv_parse(self, query, data: bytes):
        """Read an uploaded scenario CSV and return a preview.

        The parsed sheet is cached server-side under a key: these files hold
        up to 9,700 rows, and re-uploading megabytes just to write the JSONs
        would be wasteful.
        """
        if not data:
            self._json({'ok': False, 'message': 'empty upload'}, 400)
            return
        if len(data) > 64 * 1024 * 1024:
            self._json({'ok': False, 'message': 'CSV exceeds the 64 MB limit'}, 413)
            return
        try:
            parsed = scenario_csv.parse_csv(data)
        except ValueError as e:
            self._json({'ok': False, 'message': str(e)}, 400)
            return
        except Exception as e:
            self._json({'ok': False, 'message': str(e)}, 500)
            return

        key = secrets.token_urlsafe(9)
        with _csv_lock:
            # Keep only the few most recent uploads; these are large.
            for old in list(_csv_cache)[:-4]:
                _csv_cache.pop(old, None)
            _csv_cache[key] = parsed

        preview = dict(parsed)
        preview['rows'] = parsed['rows'][:200]
        preview['key'] = key
        preview['ok'] = True
        preview['filename'] = (query.get('name', [''])[0] or '')
        self._json(preview)

    def _handle_csv_plan(self, body):
        with _csv_lock:
            parsed = _csv_cache.get(body.get('key', ''))
        if not parsed:
            self._json({'ok': False, 'message': 'upload expired; drop the CSV again'}, 404)
            return
        self._json({'ok': True, 'plan': scenario_csv.plan(
            parsed, body.get('scenarios'), body.get('prefix', ''),
            body.get('destination', scenario_csv.DEST_DROPBOX))})

    def _handle_csv_write(self, body):
        with _csv_lock:
            parsed = _csv_cache.get(body.get('key', ''))
        if not parsed:
            self._json({'ok': False, 'message': 'upload expired; drop the CSV again'}, 404)
            return
        series_id = (body.get('series_id') or '').strip()
        if not series_id:
            self._json({'ok': False, 'message': 'series_id is required'}, 400)
            return
        try:
            result = scenario_csv.generate(
                parsed, body.get('scenarios'), series_id,
                kind=body.get('kind') or None,
                destination=body.get('destination', scenario_csv.DEST_DROPBOX),
                prefix=body.get('prefix', ''),
                overwrite=bool(body.get('overwrite')))
        except ValueError as e:
            self._json({'ok': False, 'message': str(e)}, 400)
            return
        except Exception as e:
            self._json({'ok': False, 'message': str(e)}, 500)
            return
        self._json(result)

    # ---- admin gate

    def _ticket(self) -> str:
        """The admin ticket, from a header or the query string."""
        t = self.headers.get('X-Admin-Ticket', '')
        if t:
            return t.strip()
        query = urllib.parse.urlparse(self.path).query
        return urllib.parse.parse_qs(query).get('ticket', [''])[0].strip()

    def _is_admin(self) -> bool:
        return _admin_valid(self._ticket())

    def _require_admin(self) -> bool:
        """Guard a setup route. Answers the request itself when refused."""
        if self._is_admin():
            return True
        if not users.admin_configured():
            self._json({'ok': False, 'needs_admin': True, 'unconfigured': True,
                        'message': 'No admin PIN has been set yet. Set one from '
                                   'the pipeline VM before configuring scenarios.'},
                       403)
        else:
            self._json({'ok': False, 'needs_admin': True,
                        'message': 'Unlock admin to change this.'}, 403)
        return False

    def _handle_admin_unlock(self, body):
        now = time.time()
        with _admin_lock:
            locked_until = _admin_failures['until']
        if now < locked_until:
            self._json({'ok': False,
                        'message': 'Too many wrong PINs. Try again in %d seconds.'
                                   % int(locked_until - now)}, 429)
            return
        if not users.admin_configured():
            self._json({'ok': False, 'unconfigured': True,
                        'message': 'No admin PIN has been set yet.'}, 400)
            return
        if not users.check_admin_pin(str(body.get('pin', '') or '')):
            # Slow repeated guessing down. The PIN is short by design, so
            # without this the gate would be a formality.
            with _admin_lock:
                _admin_failures['count'] += 1
                if _admin_failures['count'] >= ADMIN_LOCKOUT_AFTER:
                    _admin_failures['count'] = 0
                    _admin_failures['until'] = now + ADMIN_LOCKOUT_SECONDS
            self._json({'ok': False, 'message': 'That PIN is not right.'}, 401)
            return
        with _admin_lock:
            _admin_failures['count'] = 0
            _admin_failures['until'] = 0.0
        session = _admin_new_session(str(body.get('who', '') or ''))
        self._json({'ok': True, **session})

    def _handle_admin_pin(self, body):
        """Set the PIN for the first time, or change a known one.

        Setting the first PIN is allowed from a local request only: on the VM
        that is the operator at the console, and on a tester's machine the
        share is read-only so the write fails with a clear message anyway.
        """
        first_time = not users.admin_configured()
        if first_time and not self._client_is_local():
            self._json({'ok': False,
                        'message': 'Set the first admin PIN from the pipeline VM.'},
                       403)
            return
        if not first_time and not self._is_admin():
            self._json({'ok': False, 'needs_admin': True,
                        'message': 'Unlock admin before changing the PIN.'}, 403)
            return
        try:
            users.set_admin_pin(str(body.get('pin', '') or ''),
                                str(body.get('current', '') or ''))
        except users.ReadOnlyShare as e:
            self._json({'ok': False, 'message': str(e)}, 403)
            return
        except ValueError as e:
            self._json({'ok': False, 'message': str(e)}, 400)
            return
        self._json({'ok': True, **_admin_new_session(str(body.get('who', '') or ''))})

    def _handle_test_create(self, body):
        try:
            record = inspection.create_test(
                str(body.get('order_number', '')),
                scenario_id=body.get('scenario_id') or None,
                run_id=str(body.get('run_id') or inspection.LIVE_RUN),
                # Who started it, not which IP did: the address said nothing
                # useful once several people were working from their own PCs.
                created_by=(str(body.get('tester', '') or '').strip()
                            or self.client_address[0]))
        except ValueError as e:
            self._json({'ok': False, 'message': str(e)}, 400)
            return
        except Exception as e:
            self._json({'ok': False, 'message': str(e)}, 500)
            return
        self._json({'ok': True, 'test_id': record['test_id'],
                    'message': 'Inspection started for %s at %s (%d components)'
                               % (record['order_number'], record['run_label'],
                                  len(record['components']))})

    def _handle_test_result(self, body):
        try:
            record = inspection.record_result(
                str(body.get('test_id', '')), str(body.get('step', '')),
                str(body.get('component', '')), str(body.get('status', '')),
                str(body.get('comment', '') or ''),
                tester=str(body.get('tester', '') or ''),
                user_id=str(body.get('user_id', '') or ''))
        except (ValueError, FileNotFoundError) as e:
            self._json({'ok': False, 'message': str(e)}, 400)
            return
        self._json({'ok': True, 'summary': inspection.summarise(record)})

    def _serve_markup(self, query):
        """Saved drawing markup for one (test, step, component).

        Vector strokes in normalised 0..1 coordinates -- see
        inspection.save_markup for why they live in the test record. Returned
        separately from /api/tests/detail so opening a test does not carry
        every component's markup with it.
        """
        test_id = query.get('id', [''])[0]
        step = query.get('step', [''])[0]
        component = query.get('component', [''])[0].upper()
        if not test_id or not step or not component:
            self._json({'ok': False,
                        'message': 'id, step and component are all required'}, 400)
            return
        try:
            self._json(dict(inspection.load_markup(test_id, step, component),
                            ok=True))
        except FileNotFoundError as e:
            self._json({'ok': False, 'message': str(e)}, 404)
        except ValueError as e:
            self._json({'ok': False, 'message': str(e)}, 400)

    def _handle_markup_save(self, body):
        """Replace the markup for one (test, step, component)."""
        test_id = str(body.get('test_id', ''))
        step = str(body.get('step', ''))
        component = str(body.get('component', '')).upper()
        if not test_id or not step or not component:
            self._json({'ok': False,
                        'message': 'test_id, step and component are all required'}, 400)
            return
        try:
            result = inspection.save_markup(test_id, step, component,
                                            body.get('strokes'))
        except FileNotFoundError as e:
            self._json({'ok': False, 'message': str(e)}, 404)
            return
        except ValueError as e:
            # Bad geometry from a client is the client's fault, not a crash.
            self._json({'ok': False, 'message': str(e)}, 400)
            return
        self._json(dict(result, ok=True))

    def _serve_component_config(self, query):
        """What Fusion determined a component to be, from its parameter output."""
        try:
            record = inspection.load_test(query.get('id', [''])[0])
        except Exception as e:
            self._json({'available': False, 'error': str(e)}, 404)
            return
        component = query.get('component', [''])[0].upper()
        self._json(inspection.describe_configuration(record, component))

    def _serve_layout(self, query):
        """Serve a scenario's uploaded layout drawing."""
        scenario = (query.get('scenario', [''])[0] or '').strip()
        info = scenarios.layout_info(scenario) if scenario else {'exists': False}
        if not info.get('exists'):
            self._send(404, b'no layout uploaded for this scenario', 'text/plain')
            return
        # Content type follows what was actually stored -- serving a PNG as
        # application/pdf gives the viewer a blank frame.
        self._send(200, Path(info['path']).read_bytes(),
                   info.get('content_type', 'application/octet-stream'),
                   {'Content-Disposition': 'inline; filename="%s-layout.%s"'
                                           % (scenario, info.get('kind', 'bin'))})

    def _handle_layout_upload(self, query, data: bytes):
        """Store a scenario layout sent as the raw request body."""
        scenario = (query.get('scenario', [''])[0] or '').strip()
        if not scenario:
            self._json({'ok': False, 'message': 'scenario id missing'}, 400)
            return
        if not data:
            self._json({'ok': False, 'message': 'empty upload'}, 400)
            return
        if len(data) > scenarios.MAX_LAYOUT_BYTES:
            self._json({'ok': False, 'message': 'file exceeds the %d MB limit'
                        % (scenarios.MAX_LAYOUT_BYTES // 1048576)}, 413)
            return
        try:
            info = scenarios.save_layout(scenario, data)
        except ValueError as e:
            self._json({'ok': False, 'message': str(e)}, 400)
            return
        except Exception as e:
            self._json({'ok': False, 'message': str(e)}, 500)
            return
        self._json({'ok': True, 'layout': info,
                    'message': 'Layout stored for %s (%.0f KB)'
                               % (scenario, info['bytes'] / 1024)})

    def _serve_parsed_gcode(self, query):
        """Parsed toolpath for one component.

        Served on demand rather than bundled into the test detail: an order can
        hold 90 programs and parsing them all up front would make opening any
        inspection slow for data the user may never look at. The file is
        resolved THROUGH the test record, never from the query string.
        """
        try:
            record = inspection.load_test(query.get('id', [''])[0])
        except Exception as e:
            self._json({'error': str(e)}, 404)
            return
        component = query.get('component', [''])[0].upper()
        entry = next((c for c in record.get('components', [])
                      if c.get('component_id') == component), None)
        target = ((entry or {}).get('documents', {}).get('gcode') or {}).get('path')
        if not target or not Path(target).is_file():
            self._json({'error': 'no G-code for %s' % component}, 404)
            return
        try:
            self._json(gcode_parser.parse(target))
        except Exception as e:
            self._json({'error': 'could not parse: %s' % e}, 500)

    def _serve_document_geometry(self, query):
        """Page proportions of a component's drawing.

        The markup canvas is laid out from this so it covers the page exactly.
        Without it the canvas would have to guess an aspect ratio, and every
        stile -- which comes out portrait where doors are landscape -- would
        have its marks in the wrong place.
        """
        try:
            record = inspection.load_test(query.get('id', [''])[0])
        except Exception as e:
            self._json({'ok': False, 'message': str(e)}, 404)
            return
        self._json(inspection.document_geometry(
            record, query.get('component', [''])[0].upper(),
            query.get('kind', ['drawing'])[0]))

    def _serve_test_document(self, query):
        """Stream a component document (currently the drawing PDF).

        The file is looked up THROUGH the test record rather than taken from
        the query string, so this endpoint cannot be talked into serving an
        arbitrary path.
        """
        test_id = query.get('id', [''])[0]
        component = query.get('component', [''])[0].upper()
        kind = query.get('kind', ['drawing'])[0]
        try:
            record = inspection.load_test(test_id)
        except Exception as e:
            self._send(404, str(e).encode(), 'text/plain')
            return

        entry = next((c for c in record.get('components', [])
                      if c.get('component_id') == component), None)
        doc = (entry or {}).get('documents', {}).get(kind) or {}
        target = doc.get('path')
        if not target or not Path(target).is_file():
            self._send(404, b'document not available', 'text/plain')
            return

        types = {'.pdf': 'application/pdf', '.txt': 'text/plain; charset=utf-8',
                 '.csv': 'text/csv; charset=utf-8', '.json': 'application/json'}
        ctype = types.get(Path(target).suffix.lower(), 'application/octet-stream')
        try:
            data = Path(target).read_bytes()
        except Exception as e:
            self._send(500, str(e).encode(), 'text/plain')
            return
        # inline so the browser renders it in the panel instead of downloading
        self._send(200, data, ctype,
                   {'Content-Disposition': 'inline; filename="%s"' % Path(target).name})

    def _handle_config_save(self, body):
        payload = body.get('config')
        errors = validate_drawing_config(payload)
        if errors:
            self._json({'ok': False, 'errors': errors}, 400)
            return
        try:
            # Keep a timestamped copy: a bad name here costs a whole run of
            # drawings, and rolling back should not need this dashboard.
            if DRAWING_CONFIG.exists():
                stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                backup = ROOT / 'config_backups' / f'drawing_config_{stamp}.json'
                backup.parent.mkdir(parents=True, exist_ok=True)
                backup.write_text(DRAWING_CONFIG.read_text(encoding='utf-8-sig'),
                                  encoding='utf-8')
            _write_json_atomic(DRAWING_CONFIG, payload)
        except Exception as e:
            self._json({'ok': False, 'errors': [str(e)]}, 500)
            return

        self._json({
            'ok': True,
            'message': ('Saved. The add-in reads this at monitor start, so '
                        'restart Fusion (or Stop then Start the monitor) for it '
                        'to take effect.'),
        })

    def _handle_open_folder(self, body):
        """Open Explorer on the VM. Only for requests from the VM itself —
        a browser on another machine cannot be given a local Explorer window,
        so the UI shows a copyable path there instead."""
        if not self._client_is_local():
            self._json({'ok': False,
                        'message': 'Folders can only be opened on the VM itself; '
                                   'copy the path instead'}, 403)
            return
        target = str(body.get('path', ''))
        allowed = {f['path'] for f in folder_targets()}
        if target not in allowed:
            # Never hand an arbitrary path to Explorer from an HTTP request.
            self._json({'ok': False, 'message': 'Path is not one of the known '
                                                'pipeline folders'}, 400)
            return
        try:
            subprocess.Popen(['explorer.exe', target], close_fds=True)
            self._json({'ok': True, 'message': f'Opened {target}'})
        except Exception as e:
            self._json({'ok': False, 'message': str(e)}, 500)

    def _serve_asset(self, name):
        """Serve a shared CSS/JS file from control_ui.

        The name comes from a fixed route list, never from user input, so
        there is no path to traverse out of the UI directory.
        """
        target = UI_DIR / name
        if not target.is_file():
            self._send(404, b'not found', 'text/plain')
            return
        ctype = 'text/css; charset=utf-8' if name.endswith('.css') else 'application/javascript; charset=utf-8'
        self._send(200, target.read_bytes(), ctype)

    def _serve_page(self, parsed, filename):
        """Serve one of the control-plane pages."""
        page = UI_DIR / filename
        if not page.exists():
            self._send(500, ('control_ui/%s is missing' % filename).encode(), 'text/plain')
            return

        # A ?token= visit hands back a cookie so the page's own fetch() calls
        # are authorised without rewriting every URL.
        extra = {}
        supplied = urllib.parse.parse_qs(parsed.query).get('token', [''])[0]
        if supplied and not self._client_is_local():
            extra['Set-Cookie'] = ('pipeline_token=%s; Path=/; SameSite=Strict; '
                                   'Max-Age=31536000' % supplied)
        self._send(200, page.read_bytes(), 'text/html; charset=utf-8', extra)


def main():
    parser = argparse.ArgumentParser(description='Fusion pipeline control service')
    parser.add_argument('--host', default=None)
    parser.add_argument('--port', type=int, default=None)
    parser.add_argument('--run-mode', default=None, choices=['LOCAL', 'VM'])
    parser.add_argument('--print-token', action='store_true')
    args = parser.parse_args()

    cfg = load_control_config()
    if args.print_token:
        print(cfg['token'])
        return 0

    pipeline_config.set_run_mode(args.run_mode or cfg.get('run_mode', 'VM'))

    host = args.host or cfg.get('host', '0.0.0.0')
    port = args.port or int(cfg.get('port', 8765))

    Handler.control_config = cfg
    server = ThreadingHTTPServer((host, port), Handler)

    print(f'Pipeline control service {SERVICE_VERSION}')
    print(f'  run mode   : {pipeline_config.RUN_MODE}')
    print(f'  output base: {pipeline_config.OUTPUT_BASE}')
    print(f'  listening  : http://{host}:{port}/')
    print(f'  LAN access : http://<vm-address>:{port}/?token={cfg["token"]}')
    print('  (requests from the VM itself need no token)')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nShutting down.')
        server.shutdown()
    return 0


if __name__ == '__main__':
    sys.exit(main())
