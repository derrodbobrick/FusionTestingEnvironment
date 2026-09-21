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

import concurrent.futures
import json
import os
import re
import time
import uuid
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
    paths = sorted(scenarios_dir().glob('*.json'))

    def _read(p):
        try:
            return json.loads(p.read_text(encoding='utf-8'))
        except Exception:
            return None

    # series_id and assigned_to live inside each file, so every one still has
    # to be read even to filter it out -- but it is the network round trip per
    # file, not the JSON parse, that makes this slow as the scenario count
    # grows, and the reads are independent. Run them concurrently instead of
    # one at a time; ThreadPoolExecutor.map preserves the input order, so this
    # is a drop-in replacement for the old serial loop.
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, len(paths) or 1)) as ex:
        out = [s for s in ex.map(_read, paths) if s is not None]

    if series_id:
        out = [s for s in out if s.get('series_id') == series_id.upper()]
    for s in out:
        s.setdefault('assigned_to', [])
    if assigned_to:
        out = [s for s in out if not s['assigned_to'] or assigned_to in s['assigned_to']]

    # One directory read for every surviving scenario's layout, instead of up
    # to three existence checks each -- the same share-latency problem as the
    # reads above, and it compounds because MOST scenarios have no layout yet.
    lmap = _layout_map(_slug(s['id']) for s in out)
    for s in out:
        s['layout'] = layout_info(s['id'], lmap)
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
    for old in _layout_files(_slug(scenario_id)):
        try:
            old.unlink()
        except Exception:
            pass
    return True


# ------------------------------------------------------- scenario layout drawing

def _layout_files(stem: str) -> list:
    """Every layout file currently on disk for this scenario stem.

    Normally at most one, but save_layout() cannot always replace the
    previous file in place (see its own comment on the share's ACLs), so a
    scenario can end up with a plain "<stem>.<ext>" AND one or more
    "<stem>__<n>.<ext>" fallbacks left behind from an account that couldn't
    remove someone else's file.
    """
    out = []
    try:
        entries = layouts_dir().iterdir()
    except Exception:
        return out
    for entry in entries:
        name = entry.name
        if name.endswith('.tmp') or '.' not in name:
            continue
        base, ext = name.rsplit('.', 1)
        if ext.lower() not in LAYOUT_EXTENSIONS:
            continue
        if base == stem or base.startswith(stem + '__'):
            out.append(entry)
    return out


def _layout_map(stems) -> dict:
    """{scenario stem: newest layout Path}, from one directory read.

    Used by list_scenarios() so a growing scenario list costs one network
    directory listing total, not up to three existence checks per scenario.
    "Newest wins" is also what makes a same-account replace, a cross-account
    fallback (see save_layout()), and an old-format-superseded-by-new-format
    all resolve the same way, without needing to know which case happened.
    """
    stems = set(stems)
    if not stems:
        return {}
    best = {}
    try:
        entries = list(layouts_dir().iterdir())
    except Exception:
        return {}
    for entry in entries:
        name = entry.name
        if name.endswith('.tmp') or '.' not in name:
            continue
        base, ext = name.rsplit('.', 1)
        if ext.lower() not in LAYOUT_EXTENSIONS:
            continue
        stem = base if base in stems else next(
            (s for s in stems if base.startswith(s + '__')), None)
        if stem is None:
            continue
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        cur = best.get(stem)
        if cur is None or mtime > cur[1]:
            best[stem] = (entry, mtime)
    return {k: v[0] for k, v in best.items()}


def layout_path(scenario_id: str):
    stem = _slug(scenario_id)
    files = _layout_files(stem)
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)


def layout_info(scenario_id: str, _map=None) -> dict:
    stem = _slug(scenario_id)
    p = _map.get(stem) if _map is not None else layout_path(scenario_id)
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

    stem = _slug(scenario_id)
    d = layouts_dir()
    previous = _layout_files(stem)
    canonical = d / ('%s.%s' % (stem, ext))
    # Unique per attempt: two uploads landing around the same moment must not
    # collide on one temp name before either has committed.
    tmp = d / ('%s.%s.%s.tmp' % (stem, ext, uuid.uuid4().hex[:8]))
    tmp.write_bytes(data)

    try:
        os.replace(str(tmp), str(canonical))
        final = canonical
    except OSError:
        # The UAT share grants CREATOR OWNER full control of a file it made,
        # but everyone else only create/append -- if a different account
        # uploaded the layout that's there now, os.replace() cannot delete or
        # overwrite it, and previously this raised straight back to the
        # browser as a failed upload. Land under a name nobody owns yet
        # instead; layout_path()/_layout_map() always resolve to whichever
        # file is newest, so this still reads as "the layout was replaced"
        # from the tester's side even though the old file physically remains.
        final = d / ('%s__%d.%s' % (stem, int(time.time() * 1000), ext))
        os.replace(str(tmp), str(final))

    # Best-effort cleanup of whatever this just superseded. A file we don't
    # own raises on unlink() same as it would have on replace(); that's fine,
    # it just stays behind as a superseded copy instead of the current one.
    for old in previous:
        if old == final:
            continue
        try:
            old.unlink()
        except Exception:
            pass

    return layout_info(scenario_id)
