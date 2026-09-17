"""
Parse Anderson Stratos G-code into something a person can check against a drawing.

Nobody can verify "N110 X-95.825 Y0.2174 F200." against a drawing by eye, so
this turns the program into structured geometry: setups, named operations,
tools, and flattened toolpath polylines the UI renders as an XY plot.

Why setups matter
-----------------
A door program contains TWO setups in one file, with the part physically
flipped between them:

    (RIGHT INTERIOR RABBET)        G57      <- setup 1
    (NEW SETUP, ROTATE PART 180)
    (LEFT INTERIOR RABBET)         G59      <- setup 2, part turned over

Those are different coordinate systems on opposite faces. Drawing them on one
plot overlays two unrelated frames and produces a picture that means nothing,
so everything here is grouped per setup and rendered separately -- which also
matches how the part is actually inspected: check one face, flip, check the
other.

Dialect notes (measured from the real output, 2026-08-27)
--------------------------------------------------------
* Arcs are always I/J/K centre offsets; no R-form appears anywhere.
* G17/G18/G19 all occur. G18 and G19 arcs are sub-0.05" lead-ins that ramp in
  Z; they are recorded as 'ramp' rather than projected onto XY as if they were
  flat arcs.
* G28 lines are machine homing, not part geometry, and are skipped.
* Sheet size arrives as macro variables: #527 = X, #528 = Y.
"""

import math
import re
from pathlib import Path

# (T001 D=0.5 CR=0. - ZMIN=-0.75 - FLAT END MILL)
TOOL_RE = re.compile(r'\(\s*(T\d+)\s+D=([\d.]+)[^)]*?ZMIN=(-?[\d.]+)[^)]*?-\s*([A-Z ]+)\)', re.I)
SHEET_RE = re.compile(r'#(\d+)=\s*(-?[\d.]+)\s*\(([^)]*)\)')
COMMENT_RE = re.compile(r'^\(([^)]*)\)\s*$')
WORD_RE = re.compile(r'([A-Z])(-?\d*\.?\d+)')

NEW_SETUP_HINT = 'NEW SETUP'
# Comment lines that are banner/machine noise rather than operation names.
SKIP_COMMENTS = re.compile(r'^\**$|^MACHINE$|^\s*(VENDOR|MODEL|DESCRIPTION)\b|^T\d+\s+D=', re.I)

ARC_SEGMENT_DEG = 6.0     # chord resolution when flattening arcs


def _fmt(v):
    return None if v is None else round(v, 4)


class _State:
    def __init__(self):
        self.x = self.y = self.z = 0.0
        self.motion = 0          # 0,1,2,3
        self.plane = 17
        self.absolute = True
        self.offset = None       # G54..G59
        self.tool = None
        self.feed = None
        # Canned drilling cycle (G81/G82/G83). While active, an X/Y line is a
        # HOLE at that position, not a traverse -- treating them as rapids
        # draws a line straight through the hole pattern and reports the whole
        # operation as having no cutting depth.
        self.canned = None
        self.canned_z = None      # depth of the active cycle
        self.canned_r = None      # retract plane it plunges from


def _flatten_arc(x0, y0, x1, y1, i, j, clockwise):
    """Arc as a polyline. I/J are centre offsets from the start point."""
    cx, cy = x0 + i, y0 + j
    r = math.hypot(x0 - cx, y0 - cy)
    if r <= 0:
        return [(x1, y1)]
    a0 = math.atan2(y0 - cy, x0 - cx)
    a1 = math.atan2(y1 - cy, x1 - cx)
    sweep = a1 - a0
    if clockwise:
        while sweep >= 0:
            sweep -= 2 * math.pi
    else:
        while sweep <= 0:
            sweep += 2 * math.pi
    # A full circle arrives as start == end; the loop above would collapse it.
    if abs(sweep) < 1e-9:
        sweep = -2 * math.pi if clockwise else 2 * math.pi
    steps = max(2, int(abs(math.degrees(sweep)) / ARC_SEGMENT_DEG) + 1)
    pts = []
    for s in range(1, steps + 1):
        a = a0 + sweep * (s / steps)
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


def parse(path) -> dict:
    """Parse one program. Returns setups, tools, sheet size, extents, warnings."""
    text = Path(path).read_text(encoding='utf-8', errors='replace')
    lines = text.splitlines()

    tools = {}
    sheet = {}
    for line in lines:
        for m in TOOL_RE.finditer(line):
            tools[m.group(1)] = {'tool': m.group(1), 'diameter': float(m.group(2)),
                                 'zmin': float(m.group(3)), 'kind': m.group(4).strip().title()}
        for m in SHEET_RE.finditer(line):
            label = m.group(3).strip().upper()
            if 'X SHEET' in label:
                sheet['x'] = float(m.group(2))
            elif 'Y SHEET' in label:
                sheet['y'] = float(m.group(2))

    st = _State()
    setups = []
    setup = None
    op = None
    pending_setup_break = False
    # Operation comments appear BEFORE the work-offset line that opens their
    # setup, so the name is held here and the operation is created lazily on
    # the first motion -- otherwise the name lands in the previous setup and
    # the geometry ends up '(unnamed)'.
    pending_op_name = None

    def new_setup(reason):
        nonlocal setup, op
        setup = {'index': len(setups) + 1, 'offset': st.offset, 'reason': reason,
                 'operations': [], 'warnings': []}
        setups.append(setup)
        op = None

    def new_op(name):
        nonlocal op
        if setup is None:
            new_setup('start of program')
        op = {'name': name, 'tool': st.tool, 'segments': [], 'holes': [],
              'min_cut_z': None, 'feeds': set()}
        setup['operations'].append(op)

    def add_point(kind, pts, z, is_arc=False):
        """Append to the current polyline, starting one if the kind changed."""
        nonlocal pending_op_name
        if pending_op_name is not None or op is None:
            new_op(pending_op_name or '(unnamed)')
            pending_op_name = None
        segs = op['segments']
        if not segs or segs[-1]['kind'] != kind:
            segs.append({'kind': kind, 'points': [(st.x, st.y)], 'z': z,
                         'feed': st.feed})
        if is_arc:
            first = len(segs[-1]['points'])
            segs[-1].setdefault('arc_spans', []).append([first, first + len(pts)])
        segs[-1]['points'].extend(pts)
        segs[-1]['z'] = z
        if st.feed:
            segs[-1]['feed'] = st.feed

    for raw in lines:
        line = raw.strip()
        if not line or line in ('%',):
            continue

        c = COMMENT_RE.match(line)
        if c:
            body = c.group(1).strip()
            if NEW_SETUP_HINT in body.upper():
                # Flip marker: the next work offset opens a new setup.
                pending_setup_break = True
                continue
            if SKIP_COMMENTS.match(body) or not body:
                continue
            pending_op_name = body
            continue

        if 'G28' in line:          # machine homing, not part geometry
            continue

        words = WORD_RE.findall(line.split('(')[0].upper())
        if not words:
            continue

        target = {}
        for letter, value in words:
            v = float(value)
            if letter == 'G':
                g = int(v)
                if g in (0, 1, 2, 3):
                    st.motion = g
                elif g in (17, 18, 19):
                    st.plane = g
                elif g == 90:
                    st.absolute = True
                elif g == 91:
                    st.absolute = False
                elif g in (73, 81, 82, 83):
                    st.canned = 'G%d' % g
                elif g == 80:
                    st.canned = None
                elif 54 <= g <= 59:
                    if st.offset != ('G%d' % g) or pending_setup_break or setup is None:
                        st.offset = 'G%d' % g
                        new_setup('flipped 180 degrees' if pending_setup_break
                                  else 'work offset %s' % st.offset)
                        pending_setup_break = False
                    st.offset = 'G%d' % g
            elif letter == 'T':
                st.tool = 'T%03d' % (int(v) % 100 if v >= 100 else int(v))
            elif letter == 'F':
                st.feed = v
                if op:
                    op['feeds'].add(v)
            elif letter in 'XYZIJKR':
                target[letter] = v

        if not any(k in target for k in 'XYZ'):
            continue

        nx = target.get('X', st.x if st.absolute else 0.0)
        ny = target.get('Y', st.y if st.absolute else 0.0)
        nz = target.get('Z', st.z if st.absolute else 0.0)
        if not st.absolute:
            nx, ny, nz = st.x + nx, st.y + ny, st.z + nz

        if st.canned:
            # One hole per position line while the cycle is modal.
            if pending_op_name is not None or op is None:
                new_op(pending_op_name or '(unnamed)')
                pending_op_name = None
            if 'Z' in target:
                st.canned_z = target['Z']
            if 'R' in target:
                st.canned_r = target['R']
            hole_z = st.canned_z if st.canned_z is not None else nz
            plunge = (abs(st.canned_r - hole_z)
                      if st.canned_r is not None and hole_z is not None else None)
            op['holes'].append({'x': _fmt(nx), 'y': _fmt(ny), 'z': _fmt(hole_z),
                                'cycle': st.canned, 'plunge': _fmt(plunge),
                                'feed': st.feed})
            op['min_cut_z'] = hole_z if op['min_cut_z'] is None else min(op['min_cut_z'], hole_z)
            st.x, st.y = nx, ny
            continue

        if st.motion in (2, 3):
            if st.plane == 17:
                pts = _flatten_arc(st.x, st.y, nx, ny,
                                   target.get('I', 0.0), target.get('J', 0.0),
                                   clockwise=(st.motion == 2))
                add_point('cut', pts, nz, is_arc=True)
            else:
                # XZ/YZ arc: a small Z-ramping lead-in. Drawing it as an XY arc
                # would invent geometry that is not there.
                add_point('ramp', [(nx, ny)], nz)
        else:
            kind = 'rapid' if st.motion == 0 else 'cut'
            add_point(kind, [(nx, ny)], nz)

        if op is not None and st.motion != 0:
            op['min_cut_z'] = nz if op['min_cut_z'] is None else min(op['min_cut_z'], nz)

        st.x, st.y, st.z = nx, ny, nz

    return _finalise(setups, tools, sheet, path)


def _finalise(setups, tools, sheet, path):
    warnings = []

    for s in setups:
        # Per setup: mixing offsets would add the two faces together and make
        # every part look twice its real size.
        all_x, all_y = [], []
        for o in s['operations']:
            o['feeds'] = sorted(o['feeds'])
            for seg in o['segments']:
                seg['points'] = [(_fmt(a), _fmt(b)) for a, b in seg['points']]
                seg['z'] = _fmt(seg['z'])
                if seg['kind'] in ('cut', 'ramp'):
                    all_x.extend(p[0] for p in seg['points'] if p[0] is not None)
                    all_y.extend(p[1] for p in seg['points'] if p[1] is not None)
            # How far the cutter actually travelled while engaged, how long
            # that took at the programmed feed, and the footprint it swept.
            # Rapid distance is kept separate: it is air time, not cutting.
            cut_len = rapid_len = ramp_len = 0.0
            minutes = 0.0
            for seg in o['segments']:
                pts = [q for q in seg['points'] if q[0] is not None and q[1] is not None]
                length = sum(math.dist(pts[k - 1], pts[k]) for k in range(1, len(pts)))
                if seg['kind'] == 'cut':
                    cut_len += length
                elif seg['kind'] == 'ramp':
                    ramp_len += length
                else:
                    rapid_len += length
                    continue
                if seg.get('feed'):
                    minutes += length / seg['feed']
            # Each hole plunges from the retract plane to depth; that is
            # cutting, and omitting it reported drilling operations as doing
            # no work at all.
            drill_len = 0.0
            for h in o.get('holes', []):
                if h.get('plunge'):
                    drill_len += h['plunge']
                    if h.get('feed'):
                        minutes += h['plunge'] / h['feed']
            o['drill_length'] = _fmt(drill_len) if drill_len else None
            cut_len += drill_len
            o['cut_length'] = _fmt(cut_len + ramp_len)
            o['rapid_length'] = _fmt(rapid_len)
            o['cut_minutes'] = round(minutes, 3) if minutes else None
            dia = next((t['diameter'] for t in tools.values()
                        if t['tool'] == o.get('tool')), None)
            if dia is None:
                dia = max([t['diameter'] for t in tools.values()] or [0])
            o['tool_diameter'] = dia or None
            # Swept footprint: path length x cutter width. Not volume -- depth
            # varies within an operation, so reporting cubic inches would be a
            # guess dressed up as a measurement.
            o['swept_area'] = _fmt((cut_len + ramp_len) * dia) if dia else None

            for h in o.get('holes', []):
                all_x.append(h['x'])
                all_y.append(h['y'])
            o['min_cut_z'] = _fmt(o['min_cut_z'])

            # A lateral rapid at or below the depth this operation was cutting
            # would plough through material. Comparing against the operation's
            # own depth avoids assuming where the Z origin sits, which differs
            # between programs.
            depth = o['min_cut_z']
            if depth is not None:
                for seg in o['segments']:
                    if seg['kind'] != 'rapid' or seg['z'] is None:
                        continue
                    if seg['z'] <= depth + 1e-6 and len(seg['points']) > 1:
                        moved = any(abs(seg['points'][k][0] - seg['points'][k - 1][0]) > 1e-4
                                    or abs(seg['points'][k][1] - seg['points'][k - 1][1]) > 1e-4
                                    for k in range(1, len(seg['points'])))
                        if moved:
                            warnings.append({
                                'level': 'high',
                                'setup': s['index'],
                                'operation': o['name'],
                                'message': ('Rapid move at Z%.4f, at or below this '
                                            'operation\'s cutting depth (Z%.4f)'
                                            % (seg['z'], depth)),
                            })
                            break

        s['cut_length'] = _fmt(sum(o.get('cut_length') or 0 for o in s['operations']))
        s['rapid_length'] = _fmt(sum(o.get('rapid_length') or 0 for o in s['operations']))
        mins = sum(o.get('cut_minutes') or 0 for o in s['operations'])
        s['cut_minutes'] = round(mins, 3) if mins else None
        s['hole_count'] = sum(len(o.get('holes') or []) for o in s['operations'])

        s['extents'] = {}
        if all_x and all_y:
            s['extents'] = {
                'min_x': _fmt(min(all_x)), 'max_x': _fmt(max(all_x)),
                'min_y': _fmt(min(all_y)), 'max_y': _fmt(max(all_y)),
                'width': _fmt(max(all_x) - min(all_x)),
                'height': _fmt(max(all_y) - min(all_y)),
            }

        # Cut extents versus the declared sheet. Checked per setup: a door's
        # two faces live in different offsets, so a combined bounding box
        # would report roughly twice the real width.
        #
        # These are TOOL CENTRE paths, so a correct program always overshoots
        # the sheet -- by the tool radius on each side plus the lead-in. Across
        # the observed output that overshoot is consistently about twice the
        # tool diameter (a 96" door cuts to 97.0 with a 0.5" cutter), so the
        # allowance below absorbs it. Without it this fires on every part and
        # is worth nothing. The raw numbers are always reported so the real
        # rule can replace this once the expected offsets are confirmed.
        e = s['extents']
        if e and sheet:
            dia = max([t['diameter'] for t in tools.values()] or [0.5])
            allow = 2 * dia + 0.1
            s['sheet_allowance'] = _fmt(allow)
            for axis, span, label in (('x', e['width'], 'width'), ('y', e['height'], 'height')):
                limit = sheet.get(axis)
                if limit and span > limit + allow:
                    warnings.append({
                        'level': 'high', 'setup': s['index'], 'operation': None,
                        'message': ('Setup %d cut %s %.4f exceeds %s sheet size %.4f by '
                                    'more than the %.2f" tool-path allowance'
                                    % (s['index'], label, span, axis.upper(), limit, allow)),
                    })

    analyse_cutting(setups, sheet)

    # Drop setups that carried no geometry (a header-only leading block).
    setups = [s for s in setups
              if any(o['segments'] or o.get('holes') for o in s['operations'])]
    for n, s in enumerate(setups, 1):
        s['index'] = n

    return {
        'file': str(path),
        'sheet': sheet,
        'tools': sorted(tools.values(), key=lambda t: t['tool']),
        'setups': setups,
        'warnings': warnings,
        'operation_count': sum(len(s['operations']) for s in setups),
        'setup_count': len(setups),
        'totals': {
            'cut_on_material': _fmt(sum(s.get('cut_on_material') or 0 for s in setups)),
            'cut_in_part': _fmt(sum(s.get('cut_in_part') or 0 for s in setups)),
            'cut_outside_part': _fmt(sum(s.get('cut_outside_part') or 0 for s in setups)),
            'lead_in_length': _fmt(sum(s.get('lead_in_length') or 0 for s in setups)),
            'lead_out_length': _fmt(sum(s.get('lead_out_length') or 0 for s in setups)),
            'cut_length': _fmt(sum(s.get('cut_length') or 0 for s in setups)),
            'rapid_length': _fmt(sum(s.get('rapid_length') or 0 for s in setups)),
            'cut_minutes': (round(sum(s.get('cut_minutes') or 0 for s in setups), 3)
                            or None),
            'hole_count': sum(s.get('hole_count') or 0 for s in setups),
        },
    }


# --------------------------------------------------- component bounds & cutting

# Where the part sits on the table, by work offset. Stated by the shop:
#   G57  workstop on the LEFT  -> component wholly in +X, +Y
#   G58/G59  workstops on the RIGHT (same spot) -> component wholly in -X, +Y
# X is positive to the right of the head (head at the far left), Y runs down
# toward the operator, Z is vertical. The component footprint is the declared
# sheet size (#527 x #528).
LEFT_OFFSETS = ('G57',)
RIGHT_OFFSETS = ('G58', 'G59')

# Distance either side of a boundary within which a crossing is treated as
# touching rather than leaving -- cutter radius and rounding noise.
BOUNDARY_EPS = 1e-6


def component_bounds(offset: str, sheet: dict):
    """Rectangle the component occupies in machine coordinates, or None."""
    if not sheet or not sheet.get('x') or not sheet.get('y'):
        return None
    w, h = float(sheet['x']), float(sheet['y'])
    # Y: the component lies on the NEGATIVE side of the G-code Y origin, with
    # its edge on Y=0. The machine's Y runs toward the operator, but programmed
    # Y decreases into the part, so the part occupies [-sheetY, 0].
    #
    # Measured, not assumed. With the part placed in -Y every feature matches
    # its model parameter exactly: door bottom notch 0.4414 (notching_x_dist),
    # rabbets 0.300 (rabbeting_width), panel notch 0.380, panel cutout 15.5
    # (B_354_height). Placing it in +Y gives 0.75 / 0.2 / 0.75 / 0.0 -- all
    # wrong, and inconsistent between features.
    y_lo, y_hi = -h, 0.0
    if offset in RIGHT_OFFSETS:
        return {'min_x': -w, 'max_x': 0.0, 'min_y': y_lo, 'max_y': y_hi, 'side': 'right'}
    if offset in LEFT_OFFSETS:
        return {'min_x': 0.0, 'max_x': w, 'min_y': y_lo, 'max_y': y_hi, 'side': 'left'}
    return None


def _append_path(paths, p, q):
    """Extend the last polyline if it continues, else start a new one."""
    if paths and paths[-1] and _close(paths[-1][-1], p):
        paths[-1].append(q)
    else:
        paths.append([p, q])


def _close(a, b, tol=1e-6):
    return abs(a[0] - b[0]) < tol and abs(a[1] - b[1]) < tol


def _inflate(b, radius):
    """Grow the part rectangle by the cutter radius.

    Toolpaths are TOOL CENTRE paths. On an edge cut the centre deliberately
    runs outside the part edge by about the radius -- a rabbet along Y=0 is
    programmed at Y=-0.05 with a 0.5" cutter. Testing the centre against the
    bare rectangle therefore reports an edge pass as entirely outside the part,
    which is exactly backwards. Material is removed wherever the cutter disc
    overlaps the part, so the centre is judged against the rectangle grown by
    the radius.
    """
    if not b:
        return None
    r = max(0.0, float(radius or 0.0))
    return {'min_x': b['min_x'] - r, 'max_x': b['max_x'] + r,
            'min_y': b['min_y'] - r, 'max_y': b['max_y'] + r,
            'side': b.get('side')}


def _inside(p, b):
    return (b['min_x'] - BOUNDARY_EPS <= p[0] <= b['max_x'] + BOUNDARY_EPS and
            b['min_y'] - BOUNDARY_EPS <= p[1] <= b['max_y'] + BOUNDARY_EPS)


def _clip_span(p, q, b):
    """Fraction of segment p->q that lies inside rectangle b (Liang-Barsky)."""
    t0, t1 = 0.0, 1.0
    dx, dy = q[0] - p[0], q[1] - p[1]
    for num, den in ((b['min_x'] - p[0], dx), (p[0] - b['max_x'], -dx),
                     (b['min_y'] - p[1], dy), (p[1] - b['max_y'], -dy)):
        if den == 0:
            if num > BOUNDARY_EPS:
                return None
            continue
        t = num / den
        if den > 0:
            if t > t1:
                return None
            t0 = max(t0, t)
        else:
            if t < t0:
                return None
            t1 = min(t1, t)
    return (t0, t1) if t1 > t0 else None


def _split_leads(points):
    """Split a cut polyline into lead-in, the profile pass, and lead-out.

    The profile is the single longest straight move in the polyline: lead-ins
    and lead-outs are short arcs approximated by many tiny steps, so the real
    cut stands out by an order of magnitude. Measured on a stile rabbet:
    1.56" lead-in, 97.43" profile, 1.56" lead-out.
    """
    if len(points) < 2:
        return 0, 0, [0.0]
    steps = [math.dist(points[i - 1], points[i]) for i in range(1, len(points))]
    longest = max(range(len(steps)), key=lambda i: steps[i])
    return longest, longest + 1, steps


def analyse_cutting(setups, sheet):
    """Measure how much of each operation actually cuts the component.

    Lead-in and lead-out outside the part are excluded: they are approach
    geometry, not material removal. Anything inside the component rectangle is
    counted and marked so the UI can draw it distinctly.
    """
    for setup in setups:
        bounds = component_bounds(setup.get('offset'), sheet)
        setup['bounds'] = bounds
        in_part = out_part = lead_in = lead_out = 0.0

        for op in setup['operations']:
            op_in = op_out = op_lead_in = op_lead_out = 0.0
            cutting_zone = _inflate(bounds, (op.get('tool_diameter') or 0) / 2.0)
            for seg in op['segments']:
                pts = [p for p in seg['points'] if p[0] is not None and p[1] is not None]
                seg['inside_length'] = 0.0
                seg['outside_length'] = 0.0
                if len(pts) < 2:
                    continue

                # Split the drawn path at the boundary so the UI can colour
                # material removal separately from approach travel, rather than
                # colouring a whole segment by majority.
                seg['in_paths'], seg['out_paths'] = [], []

                lead_end, tail_start, steps = _split_leads(pts)
                if seg['kind'] == 'cut':
                    op_lead_in += sum(steps[:lead_end])
                    op_lead_out += sum(steps[tail_start:])

                for i in range(1, len(pts)):
                    p, q = pts[i - 1], pts[i]
                    d = math.dist(p, q)
                    if d == 0:
                        continue
                    if cutting_zone is None:
                        seg['inside_length'] += d
                        continue
                    span = _clip_span(p, q, cutting_zone)
                    if span is None:
                        seg['outside_length'] += d
                        _append_path(seg['out_paths'], p, q)
                    else:
                        t0, t1 = span
                        seg['inside_length'] += d * (t1 - t0)
                        seg['outside_length'] += d * (1 - (t1 - t0))
                        a = (p[0] + (q[0] - p[0]) * t0, p[1] + (q[1] - p[1]) * t0)
                        b2 = (p[0] + (q[0] - p[0]) * t1, p[1] + (q[1] - p[1]) * t1)
                        if t0 > 0:
                            _append_path(seg['out_paths'], p, a)
                        _append_path(seg['in_paths'], a, b2)
                        if t1 < 1:
                            _append_path(seg['out_paths'], b2, q)

                for bucket in ('in_paths', 'out_paths'):
                    seg[bucket] = [[(_fmt(x), _fmt(y)) for x, y in run]
                                   for run in seg[bucket] if len(run) > 1]
                seg['inside_length'] = _fmt(seg['inside_length'])
                seg['outside_length'] = _fmt(seg['outside_length'])
                if seg['kind'] in ('cut', 'ramp'):
                    op_in += seg['inside_length'] or 0
                    op_out += seg['outside_length'] or 0

            # Drilled holes remove material inside the part too.
            for h in op.get('holes', []):
                if h.get('plunge'):
                    op_in += h['plunge']

            # Extent of material actually removed from the component, as the
            # cutter DISC sweeps (centre +/- radius) clipped to the part. This
            # is the figure the shop quotes: a door bottom notch measures
            # 3.31" in table X, matching notching_y_dist in the model.
            rad = (op.get('tool_diameter') or 0) / 2.0
            cut_pts = [q for seg in op['segments'] if seg['kind'] in ('cut', 'ramp')
                       for q in seg['points']
                       if q[0] is not None and q[1] is not None]
            cut_pts += [(h['x'], h['y']) for h in op.get('holes', [])
                        if h.get('x') is not None]
            if cut_pts and bounds:
                xs = [q[0] - rad for q in cut_pts] + [q[0] + rad for q in cut_pts]
                ys = [q[1] - rad for q in cut_pts] + [q[1] + rad for q in cut_pts]
                lo_x = max(min(xs), bounds['min_x']); hi_x = min(max(xs), bounds['max_x'])
                lo_y = max(min(ys), bounds['min_y']); hi_y = min(max(ys), bounds['max_y'])
                op['cut_extent_x'] = _fmt(max(0.0, hi_x - lo_x))
                op['cut_extent_y'] = _fmt(max(0.0, hi_y - lo_y))
            else:
                op['cut_extent_x'] = op['cut_extent_y'] = None

            shop = cut_on_material(op, setup.get('offset'), sheet)
            op.update(shop)

            op['cut_in_part'] = _fmt(op_in)
            op['cut_outside_part'] = _fmt(op_out)
            op['lead_in_length'] = _fmt(op_lead_in)
            op['lead_out_length'] = _fmt(op_lead_out)
            in_part += op_in
            out_part += op_out
            lead_in += op_lead_in
            lead_out += op_lead_out

        setup['cut_on_material'] = _fmt(sum(o.get('cut_on_material') or 0
                                            for o in setup['operations']))
        setup['cut_in_part'] = _fmt(in_part)
        setup['cut_outside_part'] = _fmt(out_part)
        setup['lead_in_length'] = _fmt(lead_in)
        setup['lead_out_length'] = _fmt(lead_out)
    return setups


# ------------------------------------------------- shop cut-on-material method

# From "Rabbet Cut Calculation Instructions.md" (Anderson Code Check):
#
#     Cut on Material = Linear Distance on Material + 1.136" x on-material arcs
#
# The linear distance is the long G1 where only X changes, with its X range
# CLAMPED to the material -- [0, sheetX] for G57, [-sheetX, 0] for the mirrored
# G58/G59. Note this clamps the tool CENTRE, with no radius inflation.
#
# Each insertion arc adjacent to that linear cut adds a fixed 1.136", but only
# when the arc lies on the material; an arc wholly outside the bounds is
# approach and adds nothing. Through-rabbeting (the cut running out past an
# edge) therefore contributes no arc at that end.
#
# The constant is geometric, not magic:
#     R_feature x cos(arcsin(-cy / R_feature))
#   = 2.3 x cos(arcsin(-2.0 / 2.3)) = 1.136"
# and holds while tool diameter 0.5", insertion radius 2.3" and arc centre Y
# 2.0" are unchanged. ARC_CONSTANT is recomputed from those inputs so a change
# to the tooling only needs the inputs edited here.
RABBET_INSERTION_RADIUS = 2.3
RABBET_ARC_CENTRE_Y = 2.0


def _arc_constant(radius=RABBET_INSERTION_RADIUS, centre_y=RABBET_ARC_CENTRE_Y):
    ratio = max(-1.0, min(1.0, -centre_y / radius))
    return radius * math.cos(math.asin(ratio))


ARC_CONSTANT = _arc_constant()          # 1.136" for the current tooling

# A move counts as "the long linear cut" when Y barely changes across it.
LINEAR_Y_TOLERANCE = 0.01


def material_x_range(offset: str, sheet: dict):
    """Material span along X for a work offset, per the shop instructions."""
    if not sheet or not sheet.get('x'):
        return None
    w = float(sheet['x'])
    if offset in RIGHT_OFFSETS:
        return (-w, 0.0)
    if offset in LEFT_OFFSETS:
        return (0.0, w)
    return None


def _arc_on_material(points, x_range):
    """An arc is off-material when its X range lies wholly outside the bounds."""
    if not points or not x_range:
        return False
    lo, hi = x_range
    xs = [p[0] for p in points if p[0] is not None]
    if not xs:
        return False
    return not (max(xs) < lo or min(xs) > hi)


def cut_on_material(op: dict, offset: str, sheet: dict) -> dict:
    """Cut length on the component, by the shop's documented method."""
    x_range = material_x_range(offset, sheet)
    result = {'linear_on_material': None, 'arcs_on_material': 0,
              'arc_allowance': None, 'cut_on_material': None,
              'method': 'shop formula (linear clamped + %.3f per on-material arc)'
                        % ARC_CONSTANT}
    if not x_range:
        return result
    lo, hi = x_range

    best = None                     # (length, start_x, end_x, seg, index)
    for seg in op.get('segments', []):
        if seg.get('kind') != 'cut':
            continue
        pts = [p for p in seg.get('points', []) if p[0] is not None and p[1] is not None]
        for i in range(1, len(pts)):
            p, q = pts[i - 1], pts[i]
            if abs(q[1] - p[1]) > LINEAR_Y_TOLERANCE:
                continue            # not the straight run along X
            span = abs(q[0] - p[0])
            if best is None or span > best[0]:
                best = (span, p[0], q[0], seg, i)
    if best is None:
        return result

    _, x0, x1, seg, idx = best
    start, end = min(x0, x1), max(x0, x1)
    linear = max(0.0, min(hi, end) - max(lo, start))
    result['linear_on_material'] = _fmt(linear)

    # The insertion arcs are whatever the path does either side of that run.
    pts = [p for p in seg['points'] if p[0] is not None and p[1] is not None]
    entry, exit_ = pts[:idx], pts[idx:]
    spans = seg.get('arc_spans') or []

    def has_arc(lo, hi):
        # A straight lead-in is not an insertion arc: the 1.136" allowance is
        # the chord of a real G2/G3 insertion curve, so applying it to a
        # straight approach (as on a notch) would inflate the figure.
        return any(a < hi and b > lo for a, b in spans)

    arcs = 0
    if len(entry) > 1 and has_arc(0, idx) and _arc_on_material(entry, x_range):
        arcs += 1
    if len(exit_) > 1 and has_arc(idx, len(pts)) and _arc_on_material(exit_, x_range):
        arcs += 1

    result['arcs_on_material'] = arcs
    result['arc_allowance'] = _fmt(arcs * ARC_CONSTANT)
    result['cut_on_material'] = _fmt(linear + arcs * ARC_CONSTANT)
    return result
