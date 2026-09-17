"""
Series and scenarios: the two levels above an individual inspection.

    series       3X8X, 2X8X ...        a product family being tested
      scenario   "3X8X standard run"   one or more IBUS orders, set up by hand
        test     one inspection        a specific ORDER at a specific OUTPUT RUN

Why the middle level exists
---------------------------
An order is re-run whenever the pipeline reprocesses it, and each run writes a
fresh set of output. Inspecting "order IBUS467506" is therefore ambiguous --
there are five sets of output for it. A scenario groups the orders that make up
a test case, and each inspection underneath it names the exact run it was
carried out against, so results stay attributable after the next re-run
overwrites the live folder.

The layout drawing lives at SCENARIO level rather than per order: it shows the
components laid out together for the case as a whole, which is the same drawing
whichever order of the scenario you are looking at.

Storage is one JSON per record under the testing directory, matching the rest
of the pipeline: readable without tooling, atomic writes, and it rides the
existing network sync.
"""

import json
import os
import re
from datetime import datetime
from pathlib import Path

# Accepted layout formats, decided by magic bytes rather than by filename or
# the browser-supplied type -- both are trivially wrong, and a mislabelled file
# renders as an empty frame with no explanation.
LAYOUT_TYPES = (
    (bytes.fromhex('25504446'), 'pdf', 'application/pdf'),      # %PDF
    (bytes.fromhex('89504E470D0A1A0A'), 'png', 'image/png'),    # PNG signature
    (bytes.fromhex('FFD8FF'), 'jpg', 'image/jpeg'),             # JPEG SOI
)
LAYOUT_EXTENSIONS = tuple(t[1] for t in LAYOUT_TYPES)
MAX_LAYOUT_BYTES = 30 * 1024 * 1024

DEFAULT_SERIES = [
    {'id': '3X8X', 'name': '3X8X', 'description': 'The series currently in production.'},
]


def _root() -> Path:
    """Shared testing root, so scenarios and layouts set up once on the VM are
    visible to every tester copy."""
    import deployment
    d = deployment.testing_base()
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d


def scenarios_dir() -> Path:
    d = _root() / 'scenarios'
    d.mkdir(parents=True, exist_ok=True)
    return d


def layouts_dir() -> Path:
    d = _root() / 'layouts'
    d.mkdir(parents=True, exist_ok=True)
    return d


def _series_file() -> Path:
    return _root() / 'series.json'


def _slug(text: str) -> str:
    return re.sub(r'[^A-Za-z0-9_-]', '_', (text or '').strip())[:60]


def _write_atomic(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(payload, indent=2, default=str), encoding='utf-8')
    os.replace(str(tmp), str(path))


# ---------------------------------------------------------------------- series

def list_series() -> list:
    p = _series_file()
    if not p.exists():
        _write_atomic(p, [dict(s, created_at=_now()) for s in DEFAULT_SERIES])
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except Exception:
        return list(DEFAULT_SERIES)


def _now() -> str:
    return datetime.now().isoformat(sep=' ', timespec='seconds')


def add_series(series_id: str, name: str = '', description: str = '') -> dict:
    sid = _slug(series_id).upper()
    if not sid:
        raise ValueError('Series id is required')
    existing = list_series()
    if any(s['id'] == sid for s in existing):
        raise ValueError('Series %s already exists' % sid)
    entry = {'id': sid, 'name': (name or sid).strip(),
             'description': (description or '').strip(), 'created_at': _now()}
    existing.append(entry)
    _write_atomic(_series_file(), existing)
    return entry


def delete_series(series_id: str) -> bool:
    """Removing a series with scenarios under it would orphan them, so it is
    refused rather than silently cascading."""
    sid = series_id.upper()
    if [s for s in list_scenarios() if s.get('series_id') == sid]:
        raise ValueError('Series %s still has scenarios; delete those first' % sid)
    remaining = [s for s in list_series() if s['id'] != sid]
    _write_atomic(_series_file(), remaining)
    return True


# -------------------------------------------------------------------- scenarios

def _scenario_path(scenario_id: str) -> Path:
    return scenarios_dir() / ('%s.json' % _slug(scenario_id))


def list_scenarios(series_id: str = None, assigned_to: str = None) -> list:
    """Scenarios, optionally narrowed to one series and one tester.

    Filtering by tester keeps an unassigned scenario visible to everybody: work
    nobody has been given yet is not the same as work that is none of your
    business, and hiding it would let a scenario sit untested because each
    person assumed it belonged to someone else.
    """
    out = []
    for p in sorted(scenarios_dir().glob('*.json')):
        try:
            s = json.loads(p.read_text(encoding='utf-8'))
        except Exception:
            continue
        if series_id and s.get('series_id') != series_id.upper():
            continue
        s.setdefault('assigned_to', [])
        if assigned_to and s['assigned_to'] and assigned_to not in s['assigned_to']:
            continue
        s['layout'] = layout_info(s['id'])
        out.append(s)
    return out


def load_scenario(scenario_id: str) -> dict:
    p = _scenario_path(scenario_id)
    if not p.exists():
        raise FileNotFoundError('No scenario %r' % scenario_id)
    s = json.loads(p.read_text(encoding='utf-8'))
    s.setdefault('assigned_to', [])
    s['layout'] = layout_info(s['id'])
    return s


def _clean_orders(orders) -> list:
    seen, out = set(), []
    for o in (orders or []):
        o = str(o).strip().upper()
        if o and o not in seen:
            seen.add(o)
            out.append(o)
    return out


def _clean_assignees(assigned) -> list:
    """Roster ids this scenario is assigned to.

    Ids rather than names, so renaming someone on the roster does not silently
    unassign their work. An empty list means unassigned, which is shown to
    everyone -- work with no owner should be visible, not hidden.
    """
    seen, out = set(), []
    for a in (assigned or []):
        a = str(a).strip()
        if a and a not in seen:
            seen.add(a)
            out.append(a)
    return out


def create_scenario(series_id: str, name: str, orders, description: str = '',
                    assigned_to=None) -> dict:
    sid = (series_id or '').strip().upper()
    if not sid or not any(s['id'] == sid for s in list_series()):
        raise ValueError('Unknown series %r' % series_id)
    name = (name or '').strip()
    if not name:
        raise ValueError('Scenario name is required')
    orders = _clean_orders(orders)
    if not orders:
        raise ValueError('A scenario needs at least one order')

    base = '%s__%s' % (sid, _slug(name))
    scenario_id, n = base, 1
    while _scenario_path(scenario_id).exists():
        n += 1
        scenario_id = '%s_%d' % (base, n)

    entry = {'id': scenario_id, 'series_id': sid, 'name': name,
             'description': (description or '').strip(), 'orders': orders,
             'assigned_to': _clean_assignees(assigned_to), 'created_at': _now()}
    _write_atomic(_scenario_path(scenario_id), entry)
    entry['layout'] = layout_info(scenario_id)
    return entry


def update_scenario(scenario_id: str, name=None, description=None, orders=None,
                    assigned_to=None) -> dict:
    s = load_scenario(scenario_id)
    s.pop('layout', None)
    if name is not None and str(name).strip():
        s['name'] = str(name).strip()
    if description is not None:
        s['description'] = str(description).strip()
    if orders is not None:
        cleaned = _clean_orders(orders)
        if not cleaned:
            raise ValueError('A scenario needs at least one order')
        s['orders'] = cleaned
    if assigned_to is not None:
        s['assigned_to'] = _clean_assignees(assigned_to)
    s['updated_at'] = _now()
    _write_atomic(_scenario_path(scenario_id), s)
    s['layout'] = layout_info(scenario_id)
    return s


def delete_scenario(scenario_id: str) -> bool:
    p = _scenario_path(scenario_id)
    if p.exists():
        p.unlink()
    lay = layout_path(scenario_id)
    if lay is not None:
        try:
            lay.unlink()
        except Exception:
            pass
    return True


# ------------------------------------------------------- scenario layout drawing

def layout_path(scenario_id: str):
    stem = _slug(scenario_id)
    for ext in LAYOUT_EXTENSIONS:
        p = layouts_dir() / ('%s.%s' % (stem, ext))
        if p.is_file():
            return p
    return None


def layout_info(scenario_id: str) -> dict:
    p = layout_path(scenario_id)
    if p is None:
        return {'exists': False}
    st = p.stat()
    ext = p.suffix.lstrip('.').lower()
    ctype = next((c for _, e, c in LAYOUT_TYPES if e == ext), 'application/octet-stream')
    return {'exists': True, 'bytes': st.st_size, 'kind': ext, 'content_type': ctype,
            'is_image': ext in ('png', 'jpg'),
            'uploaded_at': datetime.fromtimestamp(st.st_mtime).isoformat(sep=' ', timespec='seconds'),
            'path': str(p)}


def save_layout(scenario_id: str, data: bytes) -> dict:
    if not data:
        raise ValueError('No file content received')
    if len(data) > MAX_LAYOUT_BYTES:
        raise ValueError('Layout is %.1f MB; the limit is %d MB'
                         % (len(data) / 1048576, MAX_LAYOUT_BYTES // 1048576))
    ext = next((e for magic, e, _ in LAYOUT_TYPES if data.startswith(magic)), None)
    if ext is None:
        raise ValueError('Unsupported file type. Upload a PDF, PNG or JPG.')

    # Replacing with a different format must not leave the old file behind, or
    # layout_path would keep finding the stale one.
    existing = layout_path(scenario_id)
    if existing is not None and existing.suffix.lstrip('.').lower() != ext:
        try:
            existing.unlink()
        except Exception:
            pass

    p = layouts_dir() / ('%s.%s' % (_slug(scenario_id), ext))
    tmp = p.with_suffix(p.suffix + '.tmp')
    tmp.write_bytes(data)
    os.replace(str(tmp), str(p))
    return layout_info(scenario_id)
