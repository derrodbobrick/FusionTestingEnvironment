"""
Configuration for Fusion 360 Manufacturing Pipeline
Centralized path management for the pipeline's folder structure
Supports LOCAL mode (dev machine) and VM mode (production)

Toggling modes
--------------
Mode is chosen by set_run_mode('LOCAL'|'VM'), which is driven by the
"run_mode" field in autostart_config.json (used by both the toolbar button
and the Fusion-startup auto-start). Nothing else needs editing to switch.

Where output goes
-----------------
In BOTH modes Fusion writes every artifact to local disk only. Nothing the
add-in produces is written straight to a network share, because an inline
copy over the wire stalls Fusion between components and can leave a
half-written file visible to a machine controller.

Getting output onto the network is the job of file_sync_service.py, a
separate process that runs alongside Fusion and mirrors the local folders to
the shares. Because it is out-of-process, Fusion moves straight on to the
next order while the previous order's files are still being copied.
get_sync_pairs() below is the local -> network mapping that service consumes.

The one network path the add-in still touches directly is the inbound order
dropbox in VM mode: orders are small JSON files, and they are read, not
written.

Base folders
------------
Each mode has its own local base, resolved in this order (first hit wins):

  LOCAL: env FUSION_PIPELINE_LOCAL_BASE > "local_base" in local_config.json
         > DEFAULT_LOCAL_BASE
  VM:    env FUSION_PIPELINE_VM_BASE    > "vm_base"    in local_config.json
         > DEFAULT_VM_BASE

LOCAL mode also points the sync targets at sandbox folders, so a local run
exercises the same code paths as production -- including the sync service --
without touching any share.
"""

import json
import os
from pathlib import Path


# --- Run mode ---------------------------------------------------------------
# Set by set_run_mode() at startup based on user selection.
RUN_MODE = 'LOCAL'  # 'LOCAL' or 'VM'

# --- Local bases (Fusion writes here in BOTH modes) -------------------------
DEFAULT_LOCAL_BASE = r'C:\Users\james.derrod\VM Fusion Extension\local_test_env'
DEFAULT_VM_BASE    = r'C:\FusionPipeline'

# --- Network locations (VM mode) --------------------------------------------
# Read directly by the add-in:
VM_DROPBOX = r'\\ddc-mefs\iBob-Export\S2S_Export'

# Written ONLY by file_sync_service.py, never by the add-in:
VM_NET_BASE     = r'\\ddc-mefs\Fusion\Fusion Folders'
VM_NET_DRILLING = r'\\ddc-mefs\Gannomat\F360_output'   # {order_id}.json flat
VM_NET_NC       = r'\\ddc-mefs\Anderson'               # *.nc flat
VM_NET_CUTLIST  = r'\\ddc-mefs\Voorwood'               # *.csv flat


def _read_local_config(key: str) -> str:
    """Read a string setting from local_config.json at the add-in root."""
    try:
        # This file lives at <addin_root>/src/config.py
        cfg_path = Path(__file__).resolve().parent.parent / 'local_config.json'
        # utf-8-sig so a BOM (Notepad on the VM, PowerShell's Set-Content)
        # can't make this fall through to the built-in default base path.
        with open(cfg_path, 'r', encoding='utf-8-sig') as f:
            return str(json.load(f).get(key, '')).strip()
    except Exception:
        return ''


def _resolve_local_base() -> str:
    """LOCAL sandbox root: env var > local_config.json > DEFAULT_LOCAL_BASE."""
    return (os.environ.get('FUSION_PIPELINE_LOCAL_BASE', '').strip()
            or _read_local_config('local_base')
            or DEFAULT_LOCAL_BASE)


def _resolve_vm_base() -> str:
    """VM local root: env var > local_config.json > DEFAULT_VM_BASE."""
    return (os.environ.get('FUSION_PIPELINE_VM_BASE', '').strip()
            or _read_local_config('vm_base')
            or DEFAULT_VM_BASE)


def _build_paths(base: str, dropbox_override: str = None):
    """Build all path variables from a local base directory.

    Every path returned is local. The flat machine-drop folders are staging
    dirs that file_sync_service.py copies to the Gannomat / Anderson /
    Voorwood shares; the add-in only ever writes the local side.
    """
    b = Path(base)
    dropbox = Path(dropbox_override) if dropbox_override else b / 'order_dropbox'
    drops = b / 'machine_drops'
    return {
        'NETWORK_BASE':     base,
        'INPUT_BASE':       b / 'input',
        'INPUT_DOOR':       b / 'input' / 'door',
        'INPUT_PANEL':      b / 'input' / 'panel',
        'INPUT_STILE':      b / 'input' / 'stile',
        'ORDER_DROPBOX':    dropbox,
        'ORDER_PROCESSING': b / 'order_processing',
        'ORDER_COMPLETED':  b / 'order_completed',
        'ORDER_FAILED':     b / 'order_failed',
        'OUTPUT_BASE':      b / 'output',
        'OUTPUT_MODELS':    b / 'output' / 'models',
        'OUTPUT_GCODE':     b / 'output' / 'gcode',
        'OUTPUT_PARAMETERS':b / 'output' / 'parameters',
        'OUTPUT_LOGS':      b / 'output' / 'logs',
        'DROP_DRILLING':    drops / 'gannomat',
        'DROP_NC':          drops / 'anderson',
        'DROP_CUTLIST':     drops / 'voorwood',
    }


def _apply(p: dict):
    """Rebind every module-level path global from a built path dict."""
    global NETWORK_BASE
    global INPUT_BASE, INPUT_DOOR, INPUT_PANEL, INPUT_STILE
    global ORDER_DROPBOX, ORDER_PROCESSING, ORDER_COMPLETED, ORDER_FAILED
    global OUTPUT_BASE, OUTPUT_MODELS, OUTPUT_GCODE, OUTPUT_PARAMETERS, OUTPUT_LOGS
    global DROP_DRILLING, DROP_NC, DROP_CUTLIST

    NETWORK_BASE     = p['NETWORK_BASE']
    INPUT_BASE       = p['INPUT_BASE']
    INPUT_DOOR       = p['INPUT_DOOR']
    INPUT_PANEL      = p['INPUT_PANEL']
    INPUT_STILE      = p['INPUT_STILE']
    ORDER_DROPBOX    = p['ORDER_DROPBOX']
    ORDER_PROCESSING = p['ORDER_PROCESSING']
    ORDER_COMPLETED  = p['ORDER_COMPLETED']
    ORDER_FAILED     = p['ORDER_FAILED']
    OUTPUT_BASE      = p['OUTPUT_BASE']
    OUTPUT_MODELS    = p['OUTPUT_MODELS']
    OUTPUT_GCODE     = p['OUTPUT_GCODE']
    OUTPUT_PARAMETERS= p['OUTPUT_PARAMETERS']
    OUTPUT_LOGS      = p['OUTPUT_LOGS']
    DROP_DRILLING    = p['DROP_DRILLING']
    DROP_NC          = p['DROP_NC']
    DROP_CUTLIST     = p['DROP_CUTLIST']


# Initialise with LOCAL defaults
_apply(_build_paths(_resolve_local_base()))


def set_run_mode(mode: str):
    """
    Switch all paths between LOCAL and VM mode.
    Must be called before any folder creation or processing.
    """
    global RUN_MODE

    mode = mode.upper()
    if mode not in ('LOCAL', 'VM'):
        raise ValueError(f"Invalid run mode: {mode!r}. Must be 'LOCAL' or 'VM'.")

    RUN_MODE = mode

    if mode == 'VM':
        # Local base for all output; inbound dropbox stays on the network.
        p = _build_paths(_resolve_vm_base(), dropbox_override=VM_DROPBOX)
    else:
        p = _build_paths(_resolve_local_base())

    _apply(p)


def get_sync_pairs():
    """
    The local -> remote mapping consumed by file_sync_service.py.

    Returns a list of dicts:
        name       short label used in logs and the sync state file
        source     local folder the add-in writes
        dest       folder the service copies into
        recursive  True to mirror the whole subtree, False for a flat folder

    In VM mode `dest` is the real share. In LOCAL mode it is a folder under
    <local_base>/network_mirror, so the service can be exercised end to end
    on a dev machine without touching the network.

    Deliberately excluded: order_processing (transient, files are mid-flight)
    and the input/ folders (add-in reads them, never writes them).
    """
    if RUN_MODE == 'VM':
        net = Path(VM_NET_BASE)
        drilling, nc, cutlist = VM_NET_DRILLING, VM_NET_NC, VM_NET_CUTLIST
    else:
        net = Path(_resolve_local_base()) / 'network_mirror'
        drilling = net / 'gannomat'
        nc       = net / 'anderson'
        cutlist  = net / 'voorwood'

    return [
        # Flat machine drops -- the time-critical ones the machines read.
        {'name': 'gannomat', 'source': DROP_DRILLING, 'dest': Path(drilling), 'recursive': False},
        {'name': 'anderson', 'source': DROP_NC,       'dest': Path(nc),       'recursive': False},
        {'name': 'voorwood', 'source': DROP_CUTLIST,  'dest': Path(cutlist),  'recursive': False},
        # Full output tree, preserving the layout the shares had previously.
        {'name': 'output',          'source': OUTPUT_BASE,     'dest': net / 'output',          'recursive': True},
        {'name': 'order_completed', 'source': ORDER_COMPLETED, 'dest': net / 'order_completed', 'recursive': True},
        {'name': 'order_failed',    'source': ORDER_FAILED,    'dest': net / 'order_failed',    'recursive': True},
    ]


def get_sync_state_file() -> Path:
    """Where file_sync_service.py records what it has already copied."""
    base = _resolve_vm_base() if RUN_MODE == 'VM' else _resolve_local_base()
    return Path(base) / 'sync_state.json'


def ensure_folder_structure():
    """
    Create all required folders if they don't exist.
    Call this on startup to initialize the folder structure.

    Only local folders are created here. The sync service creates its own
    destination folders, so a share being offline never blocks Fusion startup.
    """
    folders = [
        # Input folders
        INPUT_DOOR,
        INPUT_PANEL,
        INPUT_STILE,
        # Order processing folders
        ORDER_DROPBOX,
        ORDER_PROCESSING,
        ORDER_COMPLETED,
        ORDER_FAILED,
        # Output folders
        OUTPUT_MODELS,
        OUTPUT_GCODE,
        OUTPUT_PARAMETERS,
        OUTPUT_LOGS,
        # Flat machine-drop staging folders
        DROP_DRILLING,
        DROP_NC,
        DROP_CUTLIST,
    ]

    for folder in folders:
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            # ORDER_DROPBOX is a network share in VM mode. If it is
            # unreachable at startup, surface it as a dropbox problem rather
            # than failing the whole folder-structure step.
            if folder == ORDER_DROPBOX:
                continue
            raise

    return True


def get_paths_info() -> str:
    """
    Get formatted information about all configured paths.

    Returns:
        Formatted string with path information
    """
    info = "=== Fusion 360 Manufacturing Pipeline - Folder Configuration ===\n\n"
    info += f"RUN MODE: {RUN_MODE}\n\n"

    info += "INPUT FOLDERS:\n"
    info += f"  Base:  {INPUT_BASE}\n"
    info += f"  Door:  {INPUT_DOOR}\n"
    info += f"  Panel: {INPUT_PANEL}\n"
    info += f"  Stile: {INPUT_STILE}\n\n"

    info += "ORDER PROCESSING FOLDERS:\n"
    info += f"  Dropbox:    {ORDER_DROPBOX}\n"
    info += f"  Processing: {ORDER_PROCESSING}\n"
    info += f"  Completed:  {ORDER_COMPLETED}\n"
    info += f"  Failed:     {ORDER_FAILED}\n\n"

    info += "OUTPUT FOLDERS (local):\n"
    info += f"  Base:       {OUTPUT_BASE}\n"
    info += f"  Models:     {OUTPUT_MODELS}\n"
    info += f"  G-Code:     {OUTPUT_GCODE}\n"
    info += f"  Parameters: {OUTPUT_PARAMETERS}\n"
    info += f"  Logs:       {OUTPUT_LOGS}\n"
    info += f"  Dashboard:  {Path(OUTPUT_BASE) / 'dashboard'}\n\n"

    info += "MACHINE DROP STAGING (local):\n"
    info += f"  Drilling:   {DROP_DRILLING}\n"
    info += f"  NC files:   {DROP_NC}\n"
    info += f"  Cutlist:    {DROP_CUTLIST}\n\n"

    info += "SYNC TARGETS (copied by file_sync_service.py, not by Fusion):\n"
    for pair in get_sync_pairs():
        info += f"  {pair['name']:<16} -> {pair['dest']}\n"

    return info
