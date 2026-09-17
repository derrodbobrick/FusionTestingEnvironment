"""
Inspection test suite: per-order, per-component sign-off against pipeline output.

Modelled on the Receiving-Inspection pilot app: you enter an identifier, the
app resolves every reference document for it, and you record a decision. Here
the identifier is an IBUS order number, the documents are the drawing and
drilling output the pipeline already produces, and the decision is per
component rather than per lot.

Shape
-----
    scenario (one per order under test)
      └── test          e.g. "Check Drill Positions for Accuracy"
            └── component   D1, D2, S1 ...  pass | fail | na | pending
                  └── optional comment

A test passes only when every applicable component passes. Components with no
data for that test are marked 'na' and excluded from the requirement rather
than silently dropped -- a panel with no drill data still appears in the list,
so a missing artifact is visible instead of invisible.

Storage is one JSON file per test under TESTS_DIR. That matches how the rest
of the pipeline keeps state (atomic temp-file + replace), stays readable
without tooling, and rides the existing network sync for free.

Snapshot policy: a test resolves against the CURRENT output folder for the
order, and records exactly which files and sizes it saw. Orders get re-run and
overwrite that folder, so without the record you could not tell afterwards
which run was inspected.
"""

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

COMPONENT_TYPES = {'D': 'door', 'P': 'panel', 'S': 'stile'}

STATUS_PASS = 'pass'
STATUS_FAIL = 'fail'
STATUS_NA = 'na'
STATUS_PENDING = 'pending'
VALID_STATUSES = (STATUS_PASS, STATUS_FAIL, STATUS_NA, STATUS_PENDING)

# Test catalogue. Every scenario gets the same tests; more will be added.
# 'requires' names the artifact a component must have for the test to apply to
# it -- components lacking it become 'na' instead of blocking the roll-up.
TESTS = [
    {
        'id': 'component_config',
        'name': 'Check Component Configuration',
        'description': ('Compare the scenario layout drawing against this '
                        'component, confirming it is the right part in the right '
                        'position. The layout is uploaded once per scenario and '
                        'shared by every order and component under it.'),
        'documents': ['layout', 'drawing'],
        # No 'requires': the layout covers the whole order, so this check
        # applies to every component. A missing layout blocks the step rather
        # than excusing components from it -- marking them N/A would let an
        # order pass without the configuration ever being looked at.
        'requires': None,
        'needs_layout': True,
    },
    {
        'id': 'drill_positions',
        'name': 'Check Drill Positions for Accuracy',
        'description': ('Step through each component comparing its drawing '
                        'against the drilling output sent to the Gannomat.'),
        'documents': ['drawing', 'drilling'],
        'requires': 'drilling',
    },
    {
        'id': 'cnc_routing',
        'name': 'Check CNC Routing',
        'description': ('Step through each component comparing its drawing against '
                        'the routed toolpath. Doors carry two setups with the part '
                        'flipped between them, shown separately.'),
        'documents': ['drawing', 'gcode'],
        'requires': 'gcode',
    },
]


def tests_dir() -> Path:
    import deployment
    d = deployment.testing_base()
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d


def results_dir() -> Path:
    d = tests_dir() / 'results'
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d


def _write_atomic(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(payload, indent=2, default=str), encoding='utf-8')
    os.replace(str(tmp), str(path))


# ------------------------------------------------------------------ discovery

def _paths():
    import deployment
    base = deployment.artifacts_base()
    return base / 'gcode', base / 'parameters' 


# Each completed run is archived beside the live folder as
# "IBUS467506 Aug 26 2026 - 1019". Every output type is snapshotted the same
# way -- g-code, drawings, parameters, models, and the order's drilling JSON --
# so a run is fully reconstructable after later runs overwrite the live folder.
_STAMPED = re.compile(r'^(?P<order>.+) (?P<label>[A-Z][a-z]{2} \d{1,2} \d{4} - \d{4})$')

LIVE_RUN = 'live'


def _run_dirs(order_number: str, run_id: str):
    """Directories holding one run's output, for each output type."""
    gcode, params = _paths()
    suffix = '' if run_id in (None, '', LIVE_RUN) else ' ' + run_id
    return {
        'gcode': gcode / (order_number + suffix),
        'drawings': gcode / 'drawings' / (order_number + suffix),
        'parameters': params / (order_number + suffix),
    }


def list_output_runs(order_number: str) -> list:
    """Every output run available for an order, newest first.

    The live folder is listed separately from the archived snapshots: it is
    what the pipeline is writing now and is overwritten by the next run, so an
    inspection pinned to it is not reproducible in the way one pinned to a
    snapshot is.
    """
    gcode, _ = _paths()
    runs = []
    try:
        for d in gcode.iterdir():
            if not d.is_dir() or d.name == 'drawings':
                continue
            m = _STAMPED.match(d.name)
            if m and m.group('order') == order_number:
                runs.append({'run_id': m.group('label'), 'label': m.group('label'),
                             'is_live': False, 'dir': str(d),
                             'modified': datetime.fromtimestamp(d.stat().st_mtime)
                                                 .isoformat(sep=' ', timespec='seconds')})
            elif d.name == order_number:
                runs.append({'run_id': LIVE_RUN, 'label': 'Current output (live)',
                             'is_live': True, 'dir': str(d),
                             'modified': datetime.fromtimestamp(d.stat().st_mtime)
                                                 .isoformat(sep=' ', timespec='seconds')})
    except Exception:
        pass
    runs.sort(key=lambda r: (not r['is_live'], r['modified']), reverse=True)
    return runs


def known_orders() -> list:
    """Order numbers that have pipeline output available to inspect."""
    gcode, _ = _paths()
    found = set()
    try:
        for d in gcode.iterdir():
            if not d.is_dir() or d.name == 'drawings':
                continue
            m = _STAMPED.match(d.name)
            found.add(m.group('order') if m else d.name)
    except Exception:
        pass
    return sorted(found)


def _component_type(comp_id: str) -> str:
    return COMPONENT_TYPES.get(comp_id[:1].upper(), 'unknown')


def _sort_key(comp_id: str):
    """D1, D2, D10 -- order by letter then number, not lexicographically."""
    m = re.match(r'^([A-Za-z]+)(\d+)$', comp_id)
    if m:
        return (m.group(1).upper(), int(m.group(2)))
    return (comp_id.upper(), 0)


def _file_stat(p: Path) -> dict:
    try:
        st = p.stat()
        return {'path': str(p), 'exists': True, 'bytes': st.st_size,
                'modified': datetime.fromtimestamp(st.st_mtime).isoformat(sep=' ', timespec='seconds')}
    except Exception:
        return {'path': str(p), 'exists': False}


def resolve_order(order_number: str, run_id: str = LIVE_RUN) -> dict:
    """Components and documents for one order AT ONE OUTPUT RUN."""
    dirs = _run_dirs(order_number, run_id)
    order_dir = dirs['gcode']
    drawings_dir = dirs['drawings']
    params_dir = dirs['parameters']
    # The drilling payload for a run sits inside that run's g-code folder, so
    # it snapshots with everything else. The flat machine_drops copy is the
    # machine's feed and only ever holds the newest run.
    drilling_file = order_dir / ('%s.json' % order_number)

    comps = {}

    # Drawings define the component set a person can actually look at.
    try:
        for pdf in drawings_dir.glob('*-drawing.pdf'):
            m = re.match(r'^\d+-([A-Za-z]+\d+)-', pdf.name)
            if m:
                comps.setdefault(m.group(1).upper(), {})['drawing'] = _file_stat(pdf)
    except Exception:
        pass

    drill_payload = {}
    if drilling_file.exists():
        try:
            raw = json.loads(drilling_file.read_text(encoding='utf-8'))
            body = raw.get(order_number) or (list(raw.values())[0] if raw else {})
            for group in ('doors', 'panels', 'stiles'):
                for entry in (body.get(group) or []):
                    cid = str(entry.get('component_id', '')).upper()
                    if not cid:
                        continue
                    coords = entry.get('coordinates') or []
                    drill_payload[cid] = coords
                    comps.setdefault(cid, {})['drilling'] = {
                        'exists': True, 'position_count': len(coords),
                        'path': str(drilling_file),
                    }
        except Exception as e:
            drill_payload = {}
            comps.setdefault('_error', {})['drilling'] = str(e)

    for cid in list(comps.keys()):
        if cid.startswith('_'):
            continue
        for kind, suffix in (('gcode', '.txt'), ('cutlist', '.csv')):
            p = order_dir / ('1-%s-%s%s' % (cid, order_number, suffix))
            if p.exists():
                comps[cid][kind] = _file_stat(p)
        params = params_dir / ('%s_all_parameters.json' % cid)
        if params.exists():
            comps[cid]['parameters'] = _file_stat(params)

    components = []
    for cid in sorted([c for c in comps if not c.startswith('_')], key=_sort_key):
        d = comps[cid]
        components.append({
            'component_id': cid,
            'type': _component_type(cid),
            'documents': d,
            'drill_position_count': (d.get('drilling') or {}).get('position_count', 0),
        })

    return {
        'order_number': order_number,
        'run_id': run_id or LIVE_RUN,
        'resolved_at': datetime.now().isoformat(sep=' ', timespec='seconds'),
        'sources': {
            'order_dir': _file_stat(order_dir),
            'drawings_dir': _file_stat(drawings_dir),
            'parameters_dir': _file_stat(params_dir),
            'drilling_file': _file_stat(drilling_file),
        },
        'components': components,
        'drill_data': drill_payload,
    }


# --------------------------------------------------------------- test records

def _test_path(test_id: str) -> Path:
    safe = re.sub(r'[^A-Za-z0-9_.-]', '_', test_id)
    return tests_dir() / ('%s.json' % safe)


def create_test(order_number: str, scenario_id: str = None,
                run_id: str = LIVE_RUN, created_by: str = '') -> dict:
    """Start an inspection of one ORDER at one OUTPUT RUN.

    The run is part of the record's identity, not incidental metadata: the
    pipeline reprocesses orders and each run writes a fresh set of output, so
    "we inspected IBUS467506" means nothing without saying which run.
    """
    order_number = order_number.strip().upper()
    run_id = (run_id or LIVE_RUN).strip() or LIVE_RUN
    resolved = resolve_order(order_number, run_id)
    if not resolved['components']:
        raise ValueError('No components found for %s at run %r. Check the order '
                         'number, or that the pipeline produced output for it.'
                         % (order_number, run_id))

    runs = {r['run_id']: r for r in list_output_runs(order_number)}
    run = runs.get(run_id, {'run_id': run_id, 'label': run_id, 'is_live': run_id == LIVE_RUN})

    # The id carries the run so a record is self-describing on disk, and is
    # made unique explicitly: a plain second-resolution timestamp collides when
    # two inspections are started in the same second, and the second one
    # silently overwrote the first.
    run_slug = re.sub(r'[^A-Za-z0-9]+', '-', run_id).strip('-') or 'live'
    base = '%s__%s__%s' % (order_number, run_slug, datetime.now().strftime('%Y%m%d_%H%M%S'))
    test_id, n = base, 1
    while _test_path(test_id).exists():
        n += 1
        test_id = '%s_%d' % (base, n)
    steps = {}
    for spec in TESTS:
        need = spec.get('requires')
        comps = {}
        for c in resolved['components']:
            has = bool(c['documents'].get(need)) if need else True
            comps[c['component_id']] = {
                'status': STATUS_PENDING if has else STATUS_NA,
                'comment': '',
                'updated_at': None,
                'na_reason': '' if has else ('no %s output for this component' % need),
            }
        steps[spec['id']] = {'components': comps}

    record = {
        'test_id': test_id,
        'scenario_id': scenario_id or None,
        'order_number': order_number,
        'run_id': run['run_id'],
        'run_label': run.get('label', run_id),
        'run_is_live': bool(run.get('is_live')),
        'created_at': datetime.now().isoformat(sep=' ', timespec='seconds'),
        'created_by': created_by,
        # What was actually on disk for that run, so the record stands alone
        # once the live folder moves on.
        'snapshot': resolved['sources'],
        'components': resolved['components'],
        'steps': steps,
    }
    _write_atomic(_test_path(test_id), record)
    invalidate_list_cache()
    return record


def _backfill_steps(record: dict) -> bool:
    """Seed any test added to the catalogue since this record was created.

    Tests get added over time, and an inspection started last week should pick
    up a new test rather than silently omitting it. Seeding uses the documents
    already pinned on the record, so a backfilled step judges the same snapshot
    the rest of the inspection does. Returns True if anything changed.
    """
    changed = False
    for spec in TESTS:
        if spec['id'] in record.get('steps', {}):
            continue
        need = spec.get('requires')
        comps = {}
        for c in record.get('components', []):
            has = bool((c.get('documents') or {}).get(need)) if need else True
            comps[c['component_id']] = {
                'status': STATUS_PENDING if has else STATUS_NA,
                'comment': '',
                'updated_at': None,
                'na_reason': '' if has else ('no %s output for this component' % need),
            }
        record.setdefault('steps', {})[spec['id']] = {'components': comps}
        changed = True
    return changed


def _backfill_documents(record: dict) -> bool:
    """Attach document kinds this record predates.

    The snapshot deliberately pins the files an inspection was started against,
    so existing entries are never touched. But a document TYPE added later --
    parameter output, say -- is absent from older records entirely, and without
    this those inspections could never show it. Only missing kinds are filled
    in; anything already pinned stays exactly as recorded.
    """
    try:
        resolved = {c['component_id']: c.get('documents') or {}
                    for c in resolve_order(record['order_number'],
                                           record.get('run_id', LIVE_RUN))['components']}
    except Exception:
        return False

    changed = False
    for c in record.get('components', []):
        current = c.setdefault('documents', {})
        for kind, doc in (resolved.get(c['component_id']) or {}).items():
            if kind not in current:
                current[kind] = doc
                changed = True
    return changed


def load_test(test_id: str, light: bool = False, results=None) -> dict:
    """Load a record. light=True skips document backfill.

    Backfill re-resolves every component's files, which on a tester machine
    means a burst of SMB round trips per record. Listing a dozen tests that way
    takes long enough to time the page out, and a listing only needs the
    summary -- so it asks for the light load and the detail view does the full
    resolution.
    """
    p = _test_path(test_id)
    if not p.exists():
        raise FileNotFoundError('No test %r' % test_id)
    record = json.loads(p.read_text(encoding='utf-8'))
    changed = _backfill_steps(record)
    if not light:
        changed = _backfill_documents(record) or changed
    if changed:
        try:
            _write_atomic(p, record)
        except Exception:
            # A tester may only be able to create files, not rewrite them.
            # Backfill is a convenience; losing it must not block inspection.
            pass
    if not light and migrate_inline_results(record):
        record['_results_migrated'] = True
        try:
            _write_atomic(p, record)
        except Exception:
            pass
    return _apply_results(record, prefetched=results)


_LIST_CACHE = {'at': 0.0, 'value': None}
LIST_CACHE_TTL = 20.0     # seconds


def invalidate_list_cache():
    _LIST_CACHE['value'] = None


def list_tests(force: bool = False) -> list:
    """Summary of every inspection.

    Cached briefly: on a tester machine each record is read across SMB, and the
    scenario page asks for this on every open. The cache is dropped whenever a
    test is created or a result recorded, so a tester's own action shows
    immediately; another tester's appears within the TTL.
    """
    if not force and _LIST_CACHE['value'] is not None and             (time.time() - _LIST_CACHE['at']) < LIST_CACHE_TTL:
        return _LIST_CACHE['value']

    out = []
    all_results = read_all_results()
    for p in sorted(tests_dir().glob('*.json'), reverse=True):
        try:
            r = load_test(p.stem, light=True, results=all_results.get(p.stem))
        except Exception:
            continue
        out.append({
            'test_id': r.get('test_id'),
            'scenario_id': r.get('scenario_id'),
            'order_number': r.get('order_number'),
            'run_id': r.get('run_id', LIVE_RUN),
            'run_label': r.get('run_label', 'Current output (live)'),
            'run_is_live': r.get('run_is_live', True),
            'created_at': r.get('created_at'),
            'created_by': r.get('created_by', ''),
            'summary': summarise(r),
        })
    _LIST_CACHE['at'] = time.time()
    _LIST_CACHE['value'] = out
    return out


def record_result(test_id: str, step_id: str, component_id: str,
                  status: str, comment: str = '', tester: str = '',
                  user_id: str = '') -> dict:
    if status not in VALID_STATUSES:
        raise ValueError('status must be one of %s' % ', '.join(VALID_STATUSES))
    # A failure without a reason is not a usable record: whoever reads it later
    # cannot tell what was wrong. Enforced here rather than only in the UI so
    # the rule holds for any client.
    if status == STATUS_FAIL and not (comment or '').strip():
        raise ValueError('A comment is required when failing a step - say what '
                         'was wrong.')
    if not (tester or '').strip():
        raise ValueError('Enter your name before recording a result, so the '
                         'decision can be attributed.')

    record = load_test(test_id)
    step = record['steps'].get(step_id)
    if step is None:
        raise ValueError('Unknown test step %r' % step_id)
    comp = step['components'].get(component_id)
    if comp is None:
        raise ValueError('Component %r is not part of this test' % component_id)
    if comp.get('status') == STATUS_NA:
        raise ValueError('%s is not applicable for this step (%s)'
                         % (component_id, comp.get('na_reason') or 'no data'))

    write_result_file(test_id, step_id, component_id, status, comment, tester,
                      user_id=user_id)
    invalidate_list_cache()
    return _apply_results(load_test(test_id))


# ------------------------------------------------------------ page geometry

# The markup canvas has to sit exactly over the drawing, which means knowing
# the page's proportions before laying it out. Drawings are not all the same
# shape -- doors and panels come out landscape at 1584x1224, stiles portrait at
# 1224x1584 -- so assuming one aspect ratio would put every stile's marks in
# the wrong place.
#
# There is no PDF library on this box and no internet to fetch one, so the page
# box is read straight out of the file. /MediaBox is written as plain text in
# the page dictionary even when the page CONTENT is compressed, which is what
# makes this possible without a parser.

_MEDIABOX = re.compile(
    rb'/MediaBox\s*\[\s*(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s*\]')
_ROTATE = re.compile(rb'/Rotate\s+(-?\d+)')

DEFAULT_PAGE = {'width': 1584.0, 'height': 1224.0}   # the common drawing size


def pdf_page_size(path) -> dict:
    """Width and height of a PDF's first page, in points.

    Returns the fallback size rather than raising if the file cannot be read:
    a drawing that displays at a slightly wrong aspect is recoverable, a step
    that will not open at all is not.
    """
    out = dict(DEFAULT_PAGE, source='default', pages=1, rotate=0)
    try:
        data = Path(path).read_bytes()
    except Exception:
        return out

    box = _MEDIABOX.search(data)
    if not box:
        return out
    x0, y0, x1, y1 = (float(v) for v in box.groups())
    w, h = abs(x1 - x0), abs(y1 - y0)
    if w <= 0 or h <= 0:
        return out

    rot = 0
    m = _ROTATE.search(data)
    if m:
        try:
            rot = int(m.group(1)) % 360
        except Exception:
            rot = 0
    # A quarter-turn page is displayed with its sides swapped, and the canvas
    # must match what is displayed, not what is stored.
    if rot in (90, 270):
        w, h = h, w

    out.update(width=round(w, 2), height=round(h, 2), rotate=rot,
               source='mediabox',
               pages=len(re.findall(rb'/Type\s*/Page[^s]', data)) or 1)
    return out


def document_geometry(record: dict, component_id: str, kind: str = 'drawing') -> dict:
    """Page geometry for one component's document, for laying out the canvas."""
    entry = next((c for c in record.get('components', [])
                  if c.get('component_id') == component_id), None)
    doc = (entry or {}).get('documents', {}).get(kind) or {}
    path = doc.get('path')
    if not path or not Path(path).is_file():
        return {'ok': False, 'message': 'no %s for %s' % (kind, component_id)}
    if Path(path).suffix.lower() != '.pdf':
        return {'ok': False, 'message': 'not a PDF'}
    geom = pdf_page_size(path)
    geom['ok'] = True
    geom['aspect'] = round(geom['width'] / geom['height'], 6) if geom['height'] else 1.0
    return geom


MARKUP_COLOR = '#ff2d2d'          # the red the UI marks up in
MARKUP_TOOLS = ('pen', 'ellipse')
MARKUP_MAX_STROKES = 400          # a step's markup, not a drawing program
MARKUP_MAX_POINTS = 4000          # one very long freehand stroke


def _markup_key(step_id: str, component_id: str) -> str:
    return '%s|%s' % (step_id, component_id)


def _clamp01(v) -> float:
    f = float(v)
    if f != f or f in (float('inf'), float('-inf')):   # NaN / infinity
        raise ValueError('coordinates must be finite numbers')
    return round(min(1.0, max(0.0, f)), 4)


def _clean_strokes(strokes) -> list:
    """Validate and normalise strokes coming from a client.

    Rejected rather than repaired where the shape is wrong, so a broken client
    is visible immediately instead of writing silently useless markup.
    """
    if strokes is None:
        return []
    if not isinstance(strokes, list):
        raise ValueError('strokes must be a list')
    if len(strokes) > MARKUP_MAX_STROKES:
        raise ValueError('too many strokes (limit %d)' % MARKUP_MAX_STROKES)

    out = []
    for i, s in enumerate(strokes):
        if not isinstance(s, dict):
            raise ValueError('stroke %d is not an object' % i)
        tool = str(s.get('tool', 'pen'))
        if tool not in MARKUP_TOOLS:
            raise ValueError('stroke %d: unknown tool %r (expected %s)'
                             % (i, tool, ' or '.join(MARKUP_TOOLS)))
        pts = s.get('points')
        if not isinstance(pts, list) or not pts:
            raise ValueError('stroke %d has no points' % i)
        if len(pts) > MARKUP_MAX_POINTS:
            raise ValueError('stroke %d has too many points (limit %d)'
                             % (i, MARKUP_MAX_POINTS))
        if tool == 'ellipse' and len(pts) != 2:
            raise ValueError('stroke %d: an ellipse needs exactly two corner '
                             'points' % i)
        clean_pts = []
        for p in pts:
            if not isinstance(p, (list, tuple)) or len(p) != 2:
                raise ValueError('stroke %d: each point must be [x, y]' % i)
            try:
                clean_pts.append([_clamp01(p[0]), _clamp01(p[1])])
            except (TypeError, ValueError):
                raise ValueError('stroke %d: point coordinates must be numbers'
                                 % i)
        try:
            width = round(min(0.05, max(0.0005, float(s.get('width', 0.003)))), 5)
        except (TypeError, ValueError):
            raise ValueError('stroke %d: width must be a number' % i)
        colour = str(s.get('color', MARKUP_COLOR))
        if not re.match(r'^#[0-9A-Fa-f]{6}$', colour):
            colour = MARKUP_COLOR
        out.append({'tool': tool, 'color': colour, 'width': width,
                    'points': clean_pts})
    return out


def _markup_target(record: dict, step_id: str, component_id: str):
    """Fail loudly if the step/component pair is not part of this test."""
    step = (record.get('steps') or {}).get(step_id)
    if step is None:
        raise ValueError('Unknown test step %r' % step_id)
    if component_id not in (step.get('components') or {}):
        raise ValueError('Component %r is not part of this test' % component_id)
    return step


def load_markup(test_id: str, step_id: str, component_id: str) -> dict:
    record = load_test(test_id)
    _markup_target(record, step_id, component_id)
    entry = (record.get('markups') or {}).get(_markup_key(step_id, component_id))
    return {
        'test_id': record['test_id'], 'step': step_id, 'component': component_id,
        'strokes': (entry or {}).get('strokes', []),
        'updated_at': (entry or {}).get('updated_at'),
        'color': MARKUP_COLOR,
    }


def save_markup(test_id: str, step_id: str, component_id: str, strokes) -> dict:
    """Replace the markup for one (test, step, component).

    Whole-list replacement rather than append: the client owns the drawing
    surface and undo/clear are part of it, so anything else would need a
    delete protocol to express "that stroke is gone".
    """
    clean = _clean_strokes(strokes)
    record = load_test(test_id)
    _markup_target(record, step_id, component_id)
    markups = record.setdefault('markups', {})
    key = _markup_key(step_id, component_id)
    if clean:
        markups[key] = {
            'strokes': clean,
            'updated_at': datetime.now().isoformat(sep=' ', timespec='seconds'),
        }
    else:
        markups.pop(key, None)     # an emptied markup is an absent one
    _write_atomic(_test_path(test_id), record)
    return {
        'test_id': record['test_id'], 'step': step_id, 'component': component_id,
        'strokes': clean, 'count': len(clean),
        'updated_at': (markups.get(key) or {}).get('updated_at'),
    }


def markup_counts(record: dict) -> dict:
    """How many strokes each step|component carries, for badging the UI."""
    return {k: len((v or {}).get('strokes') or [])
            for k, v in (record.get('markups') or {}).items()
            if (v or {}).get('strokes')}


def step_status(step: dict) -> dict:
    """Roll a step's components up. Pass requires EVERY applicable component
    to pass; 'na' components do not count either way."""
    comps = step.get('components', {})
    counts = {s: 0 for s in VALID_STATUSES}
    for c in comps.values():
        counts[c.get('status', STATUS_PENDING)] = counts.get(c.get('status', STATUS_PENDING), 0) + 1
    applicable = len(comps) - counts[STATUS_NA]
    if counts[STATUS_FAIL]:
        overall = STATUS_FAIL
    elif applicable and counts[STATUS_PASS] == applicable:
        overall = STATUS_PASS
    elif applicable == 0:
        overall = STATUS_NA
    else:
        overall = STATUS_PENDING
    return {'status': overall, 'counts': counts, 'applicable': applicable,
            'total': len(comps)}


def summarise(record: dict) -> dict:
    per_step, statuses = {}, []
    for spec in TESTS:
        step = record.get('steps', {}).get(spec['id'])
        if not step:
            continue
        s = step_status(step)
        per_step[spec['id']] = s
        statuses.append(s['status'])
    if STATUS_FAIL in statuses:
        overall = STATUS_FAIL
    elif statuses and all(s in (STATUS_PASS, STATUS_NA) for s in statuses):
        overall = STATUS_PASS
    else:
        overall = STATUS_PENDING
    return {'overall': overall, 'steps': per_step}


def component_summary(record: dict) -> dict:
    """Per component, its status in each test and overall.

    The stored shape is test -> components, which suits the roll-up but not the
    way inspection is actually done: one part is carried through every check
    before the next part is touched. This pivots it so the UI can present a
    component at a time.
    """
    out = {}
    for c in record.get('components', []):
        cid = c['component_id']
        steps, statuses = {}, []
        for spec in TESTS:
            step = record.get('steps', {}).get(spec['id'], {})
            entry = (step.get('components') or {}).get(cid)
            if entry is None:
                continue
            steps[spec['id']] = entry
            statuses.append(entry.get('status', STATUS_PENDING))

        applicable = [s for s in statuses if s != STATUS_NA]
        if STATUS_FAIL in statuses:
            overall = STATUS_FAIL
        elif not applicable:
            overall = STATUS_NA
        elif all(s == STATUS_PASS for s in applicable):
            overall = STATUS_PASS
        else:
            overall = STATUS_PENDING

        out[cid] = {
            'overall': overall,
            'steps': steps,
            'done': sum(1 for s in applicable if s in (STATUS_PASS, STATUS_FAIL)),
            'applicable': len(applicable),
        }
    return out


def test_detail(test_id: str) -> dict:
    """A test plus live drill data, for rendering the step UI."""
    record = load_test(test_id)
    resolved = resolve_order(record['order_number'], record.get('run_id', LIVE_RUN))
    record['summary'] = summarise(record)
    record['by_component'] = component_summary(record)
    # The layout belongs to the scenario, so every order under it shares one.
    try:
        import scenarios
        record['layout'] = (scenarios.layout_info(record['scenario_id'])
                            if record.get('scenario_id') else {'exists': False})
        record['scenario'] = (scenarios.load_scenario(record['scenario_id'])
                              if record.get('scenario_id') else None)
    except Exception:
        record['layout'] = {'exists': False}
        record['scenario'] = None
    record['catalogue'] = TESTS
    record['drill_data'] = resolved['drill_data']
    # Stroke counts only -- the strokes themselves are fetched per step,
    # so opening a test does not drag every markup along with it.
    record['markup_counts'] = markup_counts(record)
    # Flag drift: the output may have been regenerated since the test started.
    now_dir = resolved['sources'].get('order_dir', {})
    was_dir = (record.get('snapshot') or {}).get('order_dir', {})
    record['snapshot_changed'] = bool(
        record.get('run_id', LIVE_RUN) == LIVE_RUN
        and was_dir.get('modified') and now_dir.get('modified')
        and was_dir['modified'] != now_dir['modified'])
    return record


# ------------------------------------------------- configuration read-out

def _pval(params: dict, name: str):
    """Value of a user parameter, or None."""
    entry = (params or {}).get(name)
    if not isinstance(entry, dict):
        return None
    return entry.get('value')


def _flag(params: dict, name: str):
    """A 0/1 model flag as a bool, or None when the parameter is absent."""
    v = _pval(params, name)
    if v is None:
        return None
    try:
        return bool(float(v))
    except (TypeError, ValueError):
        return None


def _text(params: dict, name: str) -> str:
    """Text parameters arrive wrapped in the quotes Fusion stores them with."""
    v = _pval(params, name)
    if v is None:
        return ''
    return str(v).strip().strip("'").strip('"').strip()


def _door_config(params: dict) -> list:
    """Hinging side and swing direction, as Fusion resolved them."""
    items = []
    hinge = _flag(params, 'door_hinging_right')
    items.append({
        'label': 'Hinging',
        'value': 'Unknown' if hinge is None else
                 ('Right-hand hinging' if hinge else 'Left-hand hinging'),
        'param': 'door_hinging_right',
    })
    swing = _flag(params, 'door_swinging_out')
    items.append({
        'label': 'Swing',
        'value': 'Unknown' if swing is None else ('Outswing' if swing else 'Inswing'),
        'param': 'door_swinging_out',
    })
    # Secondary conditions, shown only when set: they change how the door is
    # hung and are easy to miss on a drawing.
    if _flag(params, 'door_wall_post_hinging'):
        items.append({'label': 'Hinging', 'value': 'Hinges on a wall post',
                      'param': 'door_wall_post_hinging'})
    if _flag(params, 'door_wall_keep_latching'):
        items.append({'label': 'Latching', 'value': 'Latches on a wall keep',
                      'param': 'door_wall_keep_latching'})
    return items


def _stile_side(params: dict, prefix: str, side: str) -> dict:
    """Describe the door on one side of a stile.

    A stile can have a door on one side only, so 'no door' is a real answer
    and is reported rather than shown as a blank.
    """
    present = _flag(params, '%s_side_door' % side.lower())
    if present is False:
        return {'label': 'Door to the %s' % side.upper(), 'value': 'No door on this side',
                'param': '%s_side_door' % side.lower()}
    hinge = _flag(params, '%s_hinging_right' % prefix)
    swing = _flag(params, '%s_swinging_out' % prefix)
    if hinge is None and swing is None:
        return {'label': 'Door to the %s' % side.upper(), 'value': 'Unknown',
                'param': '%s_hinging_right' % prefix}
    bits = []
    if hinge is not None:
        bits.append('Right-hand hinging' if hinge else 'Left-hand hinging')
    if swing is not None:
        bits.append('Outswing' if swing else 'Inswing')
    return {'label': 'Door to the %s' % side.upper(), 'value': ', '.join(bits),
            'param': '%s_hinging_right / %s_swinging_out' % (prefix, prefix)}


def _rabbet_tag(params: dict, side: str) -> str:
    """Reproduces the model's own left_rabbet_tag / right_rabbet_tag logic.

    Those parameters are text expressions, so the exported value is the
    formula rather than its result; the condition is re-evaluated here from
    the numeric flags it depends on.
    """
    interior = _flag(params, '%s_interior_rabbeting' % side)
    exterior = _flag(params, '%s_exterior_rabbeting' % side)
    if interior:
        return 'Interior rabbet (%s)' % ('G59' if side == 'left' else 'G57')
    if exterior:
        return 'Exterior rabbet (%s)' % ('G57' if side == 'left' else 'G59')
    return 'No rabbet'


def _drilling_tag(params: dict, side: str):
    """Reproduces LD_drilling_tag / RD_drilling_tag, including its invalid case.

    A stile is only drilled on the side whose door hinges toward it: the LEFT
    door must be right-hand hinging, the RIGHT door left-hand hinging. If that
    door is taller than the stile once floor clearance is added, the model
    flags the input as invalid rather than producing holes -- which is worth
    seeing during inspection, not just in the model.
    """
    prefix = 'LD' if side == 'left' else 'RD'
    has_door = _flag(params, '%s_side_door' % side)
    hinging_right = _flag(params, '%s_hinging_right' % prefix)
    toward_stile = hinging_right if side == 'left' else (
        None if hinging_right is None else (not hinging_right))
    if not has_door or not toward_stile:
        return 'No drilling', False

    height = _pval(params, '%s_height' % prefix)
    clearance = _pval(params, '%s_floor_clearance' % prefix)
    stile_height = _pval(params, 'component_height')
    try:
        if (float(height) + float(clearance)) > float(stile_height):
            return 'INVALID INPUT - door is taller than the stile', True
    except (TypeError, ValueError):
        pass
    interior = _flag(params, '%s_interior_drilling' % side)
    return ('Interior drilling' if interior else 'Exterior drilling'), False


def _stile_config(params: dict) -> list:
    items = [_stile_side(params, 'LD', 'left'), _stile_side(params, 'RD', 'right')]
    alerts = False
    for side in ('left', 'right'):
        tag, bad = _drilling_tag(params, side)
        alerts = alerts or bad
        items.append({'label': '%s side drilling' % side.title(), 'value': tag,
                      'param': '%s_drilling_tag' % ('LD' if side == 'left' else 'RD'),
                      'alert': bad})
        items.append({'label': '%s side rabbeting' % side.title(),
                      'value': _rabbet_tag(params, side),
                      'param': '%s_rabbet_tag' % side})
    if alerts:
        items.insert(0, {'label': 'Alert', 'value':
                         'Fusion flagged INVALID INPUT for this stile\'s drilling',
                         'param': 'drilling_alert', 'alert': True})
    return items


def _panel_config(params: dict) -> list:
    """Whether the panel has a cutout, and which one.

    cutout_A / cutout_B carry the cutout TYPE as text ('B-354', 'B-4354', ...)
    or an empty string for none, so an empty value is 'no cutout' rather than
    missing data.
    """
    items = []
    found = False
    for slot in ('A', 'B'):
        name = _text(params, 'cutout_%s' % slot)
        if not name:
            continue
        found = True
        w = _pval(params, 'cutout_%s_width' % slot)
        h = _pval(params, 'cutout_%s_height' % slot)
        size = (' \u2014 %s" x %s"' % (w, h)) if w and h else ''
        items.append({'label': 'Cutout %s' % slot, 'value': '%s%s' % (name, size),
                      'param': 'cutout_%s' % slot})
    if not found:
        items.append({'label': 'Cutout', 'value': 'No cutout',
                      'param': 'cutout_A / cutout_B'})
    return items


def describe_configuration(record: dict, component_id: str) -> dict:
    """What Fusion determined this component to be, from its parameter output.

    Read on demand rather than bundled into the test detail: an order can hold
    90 components and each parameter file is sizeable, so loading them all up
    front would slow every inspection for data most of it never shows.
    """
    entry = next((c for c in record.get('components', [])
                  if c.get('component_id') == component_id), None)
    if entry is None:
        return {'available': False, 'error': 'unknown component'}

    doc = (entry.get('documents') or {}).get('parameters') or {}
    path = doc.get('path')
    if not path or not Path(path).is_file():
        return {'available': False, 'component_id': component_id,
                'type': entry.get('type'),
                'error': 'No parameter output was produced for this component.'}

    try:
        payload = json.loads(Path(path).read_text(encoding='utf-8'))
    except Exception as e:
        return {'available': False, 'component_id': component_id,
                'error': 'Could not read parameters: %s' % e}

    params = payload.get('user_parameters') or {}
    ctype = entry.get('type')
    if ctype == 'door':
        items = _door_config(params)
    elif ctype == 'stile':
        items = _stile_config(params)
    elif ctype == 'panel':
        items = _panel_config(params)
    else:
        items = []

    return {
        'available': True,
        'component_id': component_id,
        'type': ctype,
        'series': (payload.get('metadata') or {}).get('series_id', ''),
        'width': _pval(params, 'component_width'),
        'height': _pval(params, 'component_height'),
        'items': items,
        'source': path,
    }


# ------------------------------------------------------- shared result storage

# A decision is stored as its OWN small file, named for the step, the component
# and the tester:
#
#     results/<test_id>/<step>__<component>__<tester>.json
#
# Two reasons, both forced by how this is deployed.
#
# Concurrency: several testers work one order at the same time. The record used
# to be a single JSON rewritten in full on every decision, so the last save
# silently discarded everyone else's work -- 183 decisions for a 61-component
# order all sharing one file. Per-decision files cannot collide.
#
# Permissions: on the UAT share BUILTIN\Users has ReadAndExecute, CreateFiles
# and AppendData but NOT Modify or Delete, while CREATOR OWNER gets full
# control of whatever it creates. So a tester can always write and re-write
# their OWN files and can never write anyone else's. Putting the tester in the
# filename keeps every write inside that permission, whichever account is used.
#
# Reading merges every file for a test; where two testers judged the same
# component the most recent wins, and the others remain on disk as history.

_SAFE = re.compile(r'[^A-Za-z0-9_.-]')


def _safe(text: str) -> str:
    return _SAFE.sub('_', (text or '').strip())[:60] or 'unknown'


def _result_dir(test_id: str) -> Path:
    d = results_dir() / _safe(test_id)
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d


def _result_path(test_id: str, step_id: str, component_id: str, tester: str) -> Path:
    return _result_dir(test_id) / ('%s__%s__%s.json'
                                  % (_safe(step_id), _safe(component_id), _safe(tester)))


def write_result_file(test_id: str, step_id: str, component_id: str, status: str,
                      comment: str, tester: str, user_id: str = '') -> dict:
    """Persist one decision as its own file.

    The file is named for the tester's display name, which is what it has
    always been; the roster id is carried inside the payload as the stable
    identity. Naming files by id instead would have been tidier but would leave
    every decision recorded before the roster existed looking like a different
    person.
    """
    now = datetime.now()
    payload = {
        'test_id': test_id, 'step': step_id, 'component': component_id,
        'status': status, 'comment': comment or '', 'tester': tester or 'unknown',
        'user_id': user_id or '',
        'updated_at': now.isoformat(sep=' ', timespec='seconds'),
        # Whole seconds are what people read, but two testers can judge the
        # same component inside one second, and then "whose decision stands"
        # would come down to the order the files happened to be listed in.
        # This is the tiebreak; nothing displays it.
        'recorded_at': now.isoformat(sep=' ', timespec='microseconds'),
    }
    path = _result_path(test_id, step_id, component_id, tester)
    try:
        # Temp-then-replace where permitted; a plain write is the fallback for
        # accounts that may create but not delete.
        tmp = path.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(payload, indent=2), encoding='utf-8')
        os.replace(str(tmp), str(path))
    except Exception:
        path.write_text(json.dumps(payload, indent=2), encoding='utf-8')
    return payload


def _recency(entry) -> str:
    """Sort key for "whose decision stands".

    Prefers the microsecond stamp and falls back to the displayed one, so
    entries written before that field existed still order correctly against
    newer ones rather than sinking to the bottom.
    """
    return entry.get('recorded_at') or entry.get('updated_at') or ''


def read_results(test_id: str) -> dict:
    """Latest decision per (step, component), plus everything seen.

    Returns {(step, component): entry} keyed by tuple, with 'history' listing
    every tester's entry for that cell newest first.
    """
    latest, history = {}, {}
    d = _result_dir(test_id)
    try:
        files = sorted(d.glob('*.json'))
    except Exception:
        files = []
    for f in files:
        try:
            e = json.loads(f.read_text(encoding='utf-8'))
        except Exception:
            continue
        key = (e.get('step'), e.get('component'))
        history.setdefault(key, []).append(e)
    for key, entries in history.items():
        entries.sort(key=_recency, reverse=True)
        latest[key] = entries[0]
    return {'latest': latest, 'history': history}


def read_all_results() -> dict:
    """Every result file in one directory walk, grouped by test id.

    Listing calls used to glob the results folder once per record. Over SMB
    that turned opening the app into an 18-second wait; one walk makes it a
    single round trip regardless of how many tests exist.
    """
    out = {}
    root = results_dir()
    try:
        for f in root.rglob('*.json'):
            try:
                e = json.loads(f.read_text(encoding='utf-8'))
            except Exception:
                continue
            out.setdefault(e.get('test_id') or f.parent.name, []).append(e)
    except Exception:
        pass
    return out


def progress_by_user() -> list:
    """What each tester has decided, across every inspection.

    Counted from the result files themselves rather than from the test records,
    so it stays right even where a record could not be rewritten -- which is the
    normal case for a tester account on the share.

    Where the same tester judged one cell more than once only their latest
    decision counts, otherwise changing your mind would inflate your own tally.
    """
    latest = {}
    for test_id, entries in read_all_results().items():
        for e in entries:
            who = e.get('tester') or 'unknown'
            key = (who, test_id, e.get('step'), e.get('component'))
            prev = latest.get(key)
            if prev is None or _recency(e) > _recency(prev):
                latest[key] = e

    people = {}
    for (who, test_id, _step, _cid), e in latest.items():
        p = people.setdefault(who, {
            'tester': who, 'user_id': e.get('user_id') or '',
            'pass': 0, 'fail': 0, 'na': 0, 'total': 0,
            'tests': set(), 'last_at': None})
        status = e.get('status')
        if status in ('pass', 'fail', 'na'):
            p[status] += 1
        p['total'] += 1
        p['tests'].add(test_id)
        if (e.get('updated_at') or '') > (p['last_at'] or ''):
            p['last_at'] = e.get('updated_at')
        if not p['user_id'] and e.get('user_id'):
            p['user_id'] = e['user_id']

    out = []
    for p in people.values():
        p['tests'] = len(p['tests'])
        out.append(p)
    out.sort(key=lambda p: p['last_at'] or '', reverse=True)
    return out


def _latest_from(entries) -> dict:
    latest, history = {}, {}
    for e in entries or []:
        history.setdefault((e.get('step'), e.get('component')), []).append(e)
    for key, group in history.items():
        group.sort(key=_recency, reverse=True)
        latest[key] = group[0]
    return {'latest': latest, 'history': history}


def _apply_results(record: dict, prefetched=None) -> dict:
    """Overlay the shared result files onto a record's step skeleton."""
    data = (_latest_from(prefetched) if prefetched is not None
            else read_results(record.get('test_id', '')))
    for (step_id, cid), entry in data['latest'].items():
        step = record.get('steps', {}).get(step_id)
        if not step:
            continue
        cell = (step.get('components') or {}).get(cid)
        if cell is None:
            continue
        # 'na' is decided by what output exists, not by a tester, so a stored
        # decision never overrides it.
        if cell.get('status') == STATUS_NA:
            continue
        cell['status'] = entry.get('status', cell.get('status'))
        cell['comment'] = entry.get('comment', '')
        cell['updated_at'] = entry.get('updated_at')
        cell['tester'] = entry.get('tester')
        cell['user_id'] = entry.get('user_id') or ''
        others = data['history'].get((step_id, cid)) or []
        if len(others) > 1:
            cell['also_judged_by'] = [o.get('tester') for o in others[1:]]
            # Earlier decisions in full, so a disagreement can be read rather
            # than only counted: who said what, when, and why.
            cell['history'] = [{'tester': o.get('tester'),
                                'user_id': o.get('user_id') or '',
                                'status': o.get('status'),
                                'comment': o.get('comment') or '',
                                'updated_at': o.get('updated_at')}
                               for o in others[1:]]
    return record


def migrate_inline_results(record: dict) -> int:
    """Move decisions stored inside an old record out into result files.

    Records written before shared storage carry their decisions inline. They
    are copied out once, so existing work shows up alongside everyone else's
    rather than being stranded in the old format.
    """
    moved = 0
    marker = record.get('_results_migrated')
    if marker:
        return 0
    for step_id, step in (record.get('steps') or {}).items():
        for cid, cell in (step.get('components') or {}).items():
            if cell.get('status') in (STATUS_PENDING, STATUS_NA):
                continue
            if not cell.get('updated_at'):
                continue
            tester = cell.get('tester') or record.get('created_by') or 'imported'
            if _result_path(record['test_id'], step_id, cid, tester).exists():
                continue
            write_result_file(record['test_id'], step_id, cid, cell['status'],
                              cell.get('comment', ''), tester)
            moved += 1
    return moved
