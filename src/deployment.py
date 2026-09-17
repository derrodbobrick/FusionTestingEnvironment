"""
Where this copy reads and writes, and what it is allowed to do.

The same code runs in two roles:

    operator   on the pipeline VM. Full control: starts and stops Fusion,
               edits the cloud model configuration, generates order inputs.
               Reads and writes local paths under C:\\FusionPipeline.

    tester     on someone else's PC. Inspection only. Reads the pipeline's
               output from the network share and writes inspection results
               back to a shared folder. There is no Fusion on that machine, so
               every control that acts on Fusion is hidden rather than left to
               fail confusingly.

Both roles point at the same shared testing folder, so scenarios, layouts and
results are common to everyone. Paths come from control_config.json, which is
the one file that differs between a tester bundle and the VM install.
"""

import json
from pathlib import Path

ROLE_OPERATOR = 'operator'
ROLE_TESTER = 'tester'

_CACHE = {'at': None, 'value': None}


def _config_path() -> Path:
    # control_config.json sits at the package root, one level above src/.
    return Path(__file__).resolve().parent.parent / 'control_config.json'


def settings() -> dict:
    """Deployment settings, re-read when the file changes."""
    p = _config_path()
    try:
        stamp = p.stat().st_mtime
    except Exception:
        stamp = None
    if _CACHE['value'] is not None and _CACHE['at'] == stamp:
        return _CACHE['value']
    data = {}
    try:
        data = json.loads(p.read_text(encoding='utf-8-sig'))
    except Exception:
        data = {}
    _CACHE['at'] = stamp
    _CACHE['value'] = data
    return data


def role() -> str:
    r = str(settings().get('role', ROLE_OPERATOR)).strip().lower()
    return ROLE_TESTER if r == ROLE_TESTER else ROLE_OPERATOR


def is_tester() -> bool:
    return role() == ROLE_TESTER


def artifacts_base() -> Path:
    """Root holding the pipeline's output: gcode/, parameters/, models/.

    On the VM this is the local output folder. In a tester bundle it points at
    the network share, which the sync service already keeps current -- so a
    tester inspects exactly what the machines were sent, with no extra copying.
    """
    override = (settings().get('artifacts_base') or '').strip()
    if override:
        return Path(override)
    from config import OUTPUT_BASE
    return Path(OUTPUT_BASE)


def testing_base() -> Path:
    """Root holding scenarios, layouts and inspection results.

    Shared by every copy so that setting a scenario up once makes it visible to
    all testers, and results pool into one place.
    """
    override = (settings().get('testing_base') or '').strip()
    if override:
        p = Path(override)
    else:
        from config import OUTPUT_BASE
        p = Path(OUTPUT_BASE).parent / 'testing'
    try:
        p.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return p


def describe() -> dict:
    """What the UI needs to shape itself to this deployment."""
    s = settings()
    return {
        'role': role(),
        'is_tester': is_tester(),
        'site_name': s.get('site_name') or ('Inspection & Testing' if is_tester()
                                            else 'Fusion Pipeline Control'),
        'artifacts_base': str(artifacts_base()),
        'testing_base': str(testing_base()),
    }
