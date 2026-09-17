"""
Command channel between the out-of-process control service and the add-in.

Why a file and not a socket
---------------------------
The add-in cannot host a listener. Everything it does must happen on Fusion's
UI thread or on the monitor's background timer thread, and during a long
multi-component order the UI thread is blocked for minutes at a time -- a
socket accept loop would either sit on a thread Fusion owns or miss its
window entirely. The monitor's timer callback already does safe background
file I/O every ~3 s (see folder_monitor._fire_check_event), so a file that it
polls there is the one channel guaranteed to be serviced even mid-order.

It also degrades well: if Fusion is dead, the command file simply sits
unclaimed and the service can see that no ack arrived and escalate to a
process-level kill.

Split of responsibility
-----------------------
GRACEFUL commands go through this channel and are executed by the add-in:
    pause   -- stop taking NEW orders; the in-flight order runs to completion
    resume  -- start taking orders again
    stop    -- pause, then shut the monitor down cleanly

FORCEFUL commands never touch this channel; the service acts on the process
directly, because a wedged Fusion cannot execute its own kill:
    kill / restart / start

Files (all under OUTPUT_BASE/dashboard/):
    control_command.json -- written by the service, consumed by the add-in
    control_ack.json     -- written by the add-in after it acts
"""

import json
import os
import time
from pathlib import Path

# Commands the add-in executes itself.
#
# refresh_hub is here rather than in the service because enumerating the Fusion
# Hub needs the Fusion API (app.data), which exists only inside Fusion. The
# add-in writes hub_snapshot.json and the dashboard's model/drawing pickers
# read that -- so the pickers keep working, from any browser, even while Fusion
# is busy or shut down.
#
# reload_config re-runs setup_cloud_models against drawing_config.json as it
# stands on disk. The config was previously read once, at monitor start, so an
# edit did nothing until the next Fusion launch -- and nothing said so. This
# makes a saved change applicable in place, while the monitor is idle.
GRACEFUL_COMMANDS = ('pause', 'resume', 'stop', 'refresh_hub', 'reload_config')

# Commands the control service executes against the process.
FORCEFUL_COMMANDS = ('kill', 'restart', 'start')


def _dir(output_base=None) -> Path:
    """Dashboard dir. output_base is passed explicitly by the service, which
    runs outside Fusion and so cannot import the add-in's config module."""
    if output_base is not None:
        return Path(output_base) / 'dashboard'
    from config import OUTPUT_BASE
    return Path(OUTPUT_BASE) / 'dashboard'


def command_path(output_base=None) -> Path:
    return _dir(output_base) / 'control_command.json'


def ack_path(output_base=None) -> Path:
    return _dir(output_base) / 'control_ack.json'


def _write_atomic(path: Path, payload: dict):
    """Temp file + os.replace so a reader never sees a half-written command."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(payload, indent=2), encoding='utf-8')
    os.replace(str(tmp), str(path))


# ---------------------------------------------------------------- service side

def send(command: str, output_base, issued_by: str = 'dashboard',
         args: dict = None) -> dict:
    """Queue a graceful command for the add-in. Returns the written payload.

    The id is what the dashboard polls the ack for -- without it, a stale ack
    from the previous command would read as success for this one.

    args carries command parameters (currently only refresh_hub's optional
    'project', so the dashboard can scan a project other than the configured
    one without editing the config first).
    """
    if command not in GRACEFUL_COMMANDS:
        raise ValueError('%r is not a graceful command (expected one of %s)'
                         % (command, ', '.join(GRACEFUL_COMMANDS)))
    payload = {
        'id': '%s-%d' % (command, int(time.time() * 1000)),
        'command': command,
        'issued_by': issued_by,
        'requested_at': time.time(),
        'args': args or {},
    }
    _write_atomic(command_path(output_base), payload)
    return payload


def read_ack(output_base=None) -> dict:
    """Most recent ack written by the add-in, or {} if none."""
    try:
        p = ack_path(output_base)
        if not p.exists():
            return {}
        return json.loads(p.read_text(encoding='utf-8'))
    except Exception:
        return {}


# ----------------------------------------------------------------- add-in side

def claim(last_seen_id: str = None, output_base=None) -> dict:
    """Return a pending command if one is waiting and unseen, else {}.

    Called from the monitor's timer thread. Deliberately does NOT delete the
    command file: the dashboard shows the last command issued, and deleting it
    would make the UI forget. Replay is prevented by the caller tracking the
    id it last acted on, which also survives an add-in reload -- after a Fusion
    restart the add-in re-reads the file, sees the id it already acked in
    control_ack.json, and does not re-run a stale pause.
    """
    try:
        p = command_path(output_base)
        if not p.exists():
            return {}
        cmd = json.loads(p.read_text(encoding='utf-8'))
        if not cmd.get('command'):
            return {}
        if last_seen_id and cmd.get('id') == last_seen_id:
            return {}
        return cmd
    except Exception:
        return {}


def ack(command: dict, state: str, message: str = '', output_base=None):
    """Record that a command was carried out. Best-effort: a failed ack must
    never break order processing, so every error is swallowed."""
    try:
        _write_atomic(ack_path(output_base), {
            'id': command.get('id'),
            'command': command.get('command'),
            'state': state,
            'message': message,
            'acked_at': time.time(),
        })
    except Exception:
        pass


# ------------------------------------------------------------- persisted pause

def pause_state_path(output_base=None) -> Path:
    return _dir(output_base) / 'pause_state.json'


def read_pause_state(output_base=None) -> bool:
    """Whether order processing should be held on the next monitor start.

    Persisted because the add-in runs at Fusion startup and begins taking
    orders immediately. Without this there is no quiet window: you cannot open
    Fusion to scan the hub or change configuration without it also emptying the
    queue at you. It also means a human pause survives the automatic
    thread-pressure restart, which would otherwise silently resume production.
    """
    try:
        p = pause_state_path(output_base)
        if not p.exists():
            return False
        return bool(json.loads(p.read_text(encoding='utf-8')).get('paused'))
    except Exception:
        return False


def write_pause_state(paused: bool, output_base=None, reason: str = ''):
    """Record the pause intent so it survives a restart."""
    try:
        _write_atomic(pause_state_path(output_base), {
            'paused': bool(paused),
            'reason': reason,
            'updated_at': time.time(),
        })
    except Exception:
        pass
