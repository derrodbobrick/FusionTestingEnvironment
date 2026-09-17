"""
Turn validation-scenario CSVs into pipeline input JSONs.

Each row of these sheets is one valid test scenario, and each becomes one order
JSON containing a single component -- matching the files already produced for
the 3X8X scenarios (3X8X_DV_0001.json, 3X82_SV_0001.json).

Output shape, which the pipeline expects
----------------------------------------
    {
      "order_id": ["2X8X_DV_0001", "string", "identifier for the order"],
      "panels": [],
      "doors":  [{"id": ["D1", "string", "door ID"],
                  "parameters": {"<name>": [value, "<type>", "<description>"]}}],
      "stiles": []
    }

Every value is a triple of value, type and description. Types and descriptions
are harvested from the JSONs the pipeline has already accepted, so generated
files match the existing vocabulary exactly rather than inventing wording.

Reading the sheets
------------------
They carry notes above the table -- one blank row in some, six rows of
commentary in others -- and a header cell may contain an embedded newline
("door_wall_\\nkeep_latching"), so the header spans several physical lines. The
header is therefore FOUND by looking for known parameter names rather than
assumed to be at a fixed offset, and names are normalised on the way in.
"""

import csv
import io
import json
import os
import re
from datetime import datetime
from pathlib import Path

# Columns that describe the sheet rather than the component.
NON_PARAMETER_COLUMNS = {
    'count', 'scenario', 'drawing required?', 'human readable label', 'series',
    'hinging', 'swinging', 'gapless', 'continuous vs standard hdw',
    'continuous v standard hdw', 'special latching', 'ld', 'rd',
    'ld hinging', 'ld swinging', 'rd hinging', 'rd swinging',
}

# Parameters the new 2X8X sheets introduce that appear in no existing JSON.
# Types are unambiguous from the data (every value is 0 or 1); the wording is
# provisional and flagged to the user rather than presented as established.
NEW_PARAMETERS = {
    'door_gapping': ('bool', 'indicates whether the door is gapless'),
    'stile_gapping': ('bool', 'indicates whether the stile is gapless'),
    'continuous_hardware': ('bool', 'indicates whether continuous hardware is used rather than standard hardware'),
    'occupancy_indicator_latch': ('bool', 'indicates whether the door uses an occupancy indicator latch'),
    'floor_anchored': ('bool', 'indicates whether the stile is floor anchored'),
    'ceiling_hung': ('bool', 'indicates whether the stile is ceiling hung'),
}

TRUE_WORDS = {'1', 'true', 'yes', 'y', 't'}
FALSE_WORDS = {'0', 'false', 'no', 'n', 'f'}


def _norm(name: str) -> str:
    """Header cells may wrap across lines; collapse whitespace to one token."""
    return re.sub(r'\s+', '', (name or '')).strip()


def _label(name: str) -> str:
    return re.sub(r'\s+', ' ', (name or '')).strip().lower()


# ------------------------------------------------------- parameter vocabulary

_META_CACHE = {'at': 0.0, 'value': None}


def parameter_meta() -> dict:
    """name -> (type, description), learned from JSONs the pipeline accepted.

    Cached: this walks a few hundred files and the vocabulary does not change
    between requests.
    """
    if _META_CACHE['value'] is not None:
        return _META_CACHE['value']

    meta = {}
    for base in (Path(r'C:\FusionPipeline\all_input_jsons'),):
        if not base.is_dir():
            continue
        for f in base.glob('*.json'):
            try:
                d = json.loads(f.read_text(encoding='utf-8'))
            except Exception:
                continue
            for group in ('doors', 'panels', 'stiles'):
                for entry in (d.get(group) or []):
                    for k, v in (entry.get('parameters') or {}).items():
                        if isinstance(v, list) and len(v) == 3 and k not in meta:
                            meta[k] = (v[1], v[2])
    for k, v in NEW_PARAMETERS.items():
        meta.setdefault(k, v)
    _META_CACHE['value'] = meta
    return meta


# ---------------------------------------------------------------- csv reading

def parse_csv(data) -> dict:
    """Read a scenario sheet: locate the header, return columns and data rows."""
    if isinstance(data, bytes):
        text = data.decode('utf-8-sig', errors='replace')
    else:
        text = data
    records = list(csv.reader(io.StringIO(text)))
    meta = parameter_meta()

    header_index, columns = None, []
    for i, row in enumerate(records[:60]):
        cells = [_norm(c) for c in row]
        labels = {_label(c) for c in row}
        # The header is the first record naming things we recognise: either the
        # scenario column or actual model parameters.
        hits = sum(1 for c in cells if c in meta)
        if ('scenario' in labels and hits >= 1) or hits >= 3:
            header_index, columns = i, cells
            break
    if header_index is None:
        raise ValueError('Could not find a header row. Expected a "Scenario" '
                         'column or recognisable parameter names in the first '
                         '60 rows.')

    scenario_col = next((j for j, c in enumerate(records[header_index])
                         if _label(c) == 'scenario'), None)
    if scenario_col is None:
        raise ValueError('No "Scenario" column found in the header row.')

    param_cols = {}
    unknown = []
    for j, c in enumerate(columns):
        if not c or _label(records[header_index][j]) in NON_PARAMETER_COLUMNS:
            continue
        if c in meta:
            param_cols[j] = c
        else:
            unknown.append(c)

    rows = []
    for r in records[header_index + 1:]:
        if len(r) <= scenario_col:
            continue
        name = (r[scenario_col] or '').strip()
        if not name:
            continue
        rows.append({'scenario': name,
                     'values': {param_cols[j]: (r[j].strip() if j < len(r) else '')
                                for j in param_cols}})

    return {
        'header_index': header_index,
        'preamble_rows': header_index,
        'columns': [c for c in param_cols.values()],
        'unknown_columns': unknown,
        'rows': rows,
        'row_count': len(rows),
        'kind': detect_kind(list(param_cols.values()), rows),
    }


def detect_kind(columns, rows) -> str:
    """door / panel / stile, from the columns and the scenario naming."""
    cols = set(columns)
    if {'left_side_door', 'LD_hinging_right', 'RD_hinging_right'} & cols:
        return 'stile'
    if {'door_hinging_right', 'door_swinging_out'} & cols:
        return 'door'
    if {'cutout_A', 'panel_section'} & cols:
        return 'panel'
    names = ' '.join(r['scenario'] for r in rows[:5]).upper()
    if '_SV_' in names:
        return 'stile'
    if '_DV_' in names:
        return 'door'
    if '_PV_' in names:
        return 'panel'
    return 'door'


# ------------------------------------------------------------- json generation

GROUP_FOR_KIND = {'door': 'doors', 'panel': 'panels', 'stile': 'stiles'}
ID_PREFIX = {'door': 'D', 'panel': 'P', 'stile': 'S'}
ID_LABEL = {'door': 'door ID', 'panel': 'panel ID', 'stile': 'stile ID'}


def _coerce(raw: str, ptype: str):
    """CSV text to the JSON type the pipeline expects; blank means null."""
    s = (raw or '').strip()
    if s == '':
        return None
    if ptype == 'bool':
        low = s.lower()
        if low in TRUE_WORDS:
            return True
        if low in FALSE_WORDS:
            return False
        return None
    if ptype == 'float':
        try:
            return float(s)
        except ValueError:
            return None
    return s


def build_component_json(scenario: str, values: dict, kind: str,
                         series_id: str, null_absent_side: bool = True) -> dict:
    """One scenario row as a complete order JSON."""
    meta = parameter_meta()
    params = {}

    stype, sdesc = meta.get('series_id', ('string', 'series ID of the component'))
    params['series_id'] = [series_id, stype, sdesc]

    for name, raw in values.items():
        ptype, desc = meta.get(name, ('string', ''))
        params[name] = [_coerce(raw, ptype), ptype, desc]

    if kind == 'stile' and null_absent_side:
        # The converted 3X8X stile scenarios null every LD_*/RD_* field when
        # that side has no door, and the model gates on left_side_door /
        # right_side_door. Carrying a height for a door that is not there would
        # not match what the pipeline has been fed before.
        for side, prefix in (('left_side_door', 'LD_'), ('right_side_door', 'RD_')):
            present = params.get(side, [None])[0]
            if present is False:
                for k in list(params):
                    if k.startswith(prefix):
                        params[k][0] = None

    entry = {'id': ['%s1' % ID_PREFIX[kind], 'string', ID_LABEL[kind]],
             'parameters': params}
    doc = {'order_id': [scenario, 'string', 'identifier for the order'],
           'panels': [], 'doors': [], 'stiles': []}
    doc[GROUP_FOR_KIND[kind]] = [entry]
    return doc


DEST_DROPBOX = 'dropbox'
DEST_STAGING = 'staging'


def destination_dir(destination: str = DEST_DROPBOX) -> Path:
    """Where generated JSONs land.

    Default is the folder Fusion actually watches, so a generated scenario is
    queued for processing immediately. Files are written under a .json.tmp name
    and renamed into place -- the monitor globs *.json, so it can never pick up
    a half-written file.
    """
    from config import ORDER_DROPBOX, OUTPUT_BASE
    if destination == DEST_STAGING:
        d = Path(OUTPUT_BASE).parent / 'generated_inputs'
        d.mkdir(parents=True, exist_ok=True)
        return d
    return Path(ORDER_DROPBOX)


def scenario_name(prefix: str, raw: str) -> str:
    """Final scenario identity, used for BOTH the order_id and the filename.

    A prefix matters because separate sheets reuse the same names: the
    2X81/2X82 and 2X86/2X88 stile files both run 2X8X_SV_0001 to _9700. Without
    distinguishing them the second batch would overwrite the first in the flat
    dropbox, and the two would collide again in every output folder downstream.
    """
    return ('%s%s' % (prefix or '', raw)).strip()


def plan(parsed: dict, scenarios, prefix: str = '',
         destination: str = DEST_DROPBOX) -> dict:
    """What generating would do, without doing it.

    Reports how many files already exist at the destination so overwriting is
    a deliberate choice -- the dropbox is watched, and silently replacing a
    queued order is not something to discover afterwards.
    """
    target = destination_dir(destination)
    wanted = set(scenarios or [])
    names, clashes = [], []
    for row in parsed['rows']:
        if wanted and row['scenario'] not in wanted:
            continue
        final = scenario_name(prefix, row['scenario'])
        fname = re.sub(r'[^A-Za-z0-9_.-]', '_', final) + '.json'
        names.append(fname)
        if (target / fname).exists():
            clashes.append(fname)
    return {'directory': str(target), 'reachable': target.is_dir(),
            'count': len(names), 'existing': len(clashes),
            'existing_examples': clashes[:10], 'examples': names[:10]}


def generate(parsed: dict, scenarios, series_id: str, kind: str = None,
             destination: str = DEST_DROPBOX, null_absent_side: bool = True,
             prefix: str = '', overwrite: bool = False) -> dict:
    """Write one JSON per selected scenario. Returns what was written.

    The dropbox is flat and watched, so files go straight in rather than into a
    per-batch subfolder -- the monitor does not descend into subdirectories.
    Each file is written under a .json.tmp name and renamed into place; the
    monitor globs *.json, so it can never pick up a half-written order.
    """
    kind = kind or parsed['kind']
    wanted = set(scenarios or [])
    target = destination_dir(destination)
    if not target.is_dir():
        raise ValueError('Destination folder is not reachable: %s' % target)

    written, skipped = [], []
    for row in parsed['rows']:
        if wanted and row['scenario'] not in wanted:
            continue
        final = scenario_name(prefix, row['scenario'])
        name = re.sub(r'[^A-Za-z0-9_.-]', '_', final) + '.json'
        path = target / name
        if path.exists() and not overwrite:
            skipped.append(name)
            continue
        doc = build_component_json(final, row['values'], kind,
                                   series_id, null_absent_side)
        tmp = path.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(doc, indent=2), encoding='utf-8')
        os.replace(str(tmp), str(path))
        written.append({'scenario': final, 'file': str(path)})

    return {'ok': True, 'kind': kind, 'series_id': series_id,
            'destination': destination, 'directory': str(target),
            'written': len(written), 'skipped': len(skipped),
            'skipped_examples': skipped[:10], 'files': written[:50]}
