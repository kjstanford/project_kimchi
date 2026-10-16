"""Build layouts/igzo_v6_3T_FETs.csv: every 3-terminal FET (cells W<W>um_L<L>um) in the IGZO v6 mask.

Run from the repo root with the gds env:  python -B layouts/make_igzo_v6_fet_csv.py

Naming
  Boxes are the array_grid sites (765 x 290 um). Row = 42 - site_row (1 = top), Column = site_col + 1.
  box_id R39C29, device_id R39C29L / R39C29R (two FETs per box).
  printed_id: label printed on the mask (layer 26/1 strips, 7-segment digits read vertically), only on
  every other box row; reads 'RRR CC' with CC = Column and RRR = (45 - site_row) / 2.
"""
import csv
import os
import re
from collections import Counter

import klayout.db as db

GDS = 'layouts/20260326_IGZO_mask_set_WITH Z0_v6_TLM_padchange.GDS'
OUT = 'layouts/igzo_v6_3T_FETs.csv'
TOP = '*IGZO Layout'
X0, Y0, PX, PY = -11500.5, -5510.5, 765.0, 290.0   # array_grid site (0, 0) centre and pitch, um
N_ROWS = 42
FET_RE = re.compile(r'W(\d+)um_L(\d+)um')

# Printed IDs: 30 x 145 um strips on layer 26/1 with 5 seven-segment digits cut out, read from the top (high y)
SLOTS = [(14, 38), (40, 64), (74, 98), (100, 125), (126, 145)]  # glyph y-ranges, um from strip bottom


def glyph_key(strip_box, a, b, strip_reg, dbu):
    """Exact shape of the cut-out in one glyph slot, translated to its own bbox origin (hashable)."""
    slot = db.DBox(strip_box.left, strip_box.bottom + a, strip_box.right, strip_box.bottom + b).to_itype(dbu)
    cut = (db.Region(slot) - strip_reg).merged()
    bb = cut.bbox()
    return tuple(sorted(str(q.moved(-bb.left, -bb.bottom)) for q in cut.each()))


def printed_ids(ly, top):
    """{(site_col, site_row): 'RRR CC'} for every box carrying a printed label.

    The label value follows RRR = (45 - site_row) / 2, CC = site_col + 1 (anchored on the user's reading
    '021 29' at site (28, 3)). It is verified, not assumed: every distinct glyph shape across all strips
    must correspond to exactly one digit (closed digits 6/8/9 have a few variants: their islands differ
    slightly in size)."""
    dbu, li = ly.dbu, ly.find_layer(26, 1)
    shapes = db.Region(top.begin_shapes_rec(li))
    out, shape_digit = {}, {}
    for p in shapes.each():
        b = p.bbox().to_dtype(dbu)
        if b.height() < 100:   # islands inside closed glyphs
            continue
        c, r = round((b.left - 352.5 - X0) / PX), round((b.top - Y0) / PY)
        strip_reg = db.Region(top.begin_shapes_rec_touching(li, b.enlarged(-0.2, -0.2).to_itype(dbu)))
        label = f'{(45 - r) // 2:03d}{c + 1:02d}'
        for (a, e), d in zip(SLOTS[::-1], label):
            shape_digit.setdefault(glyph_key(b, a, e, strip_reg, dbu), set()).add(d)
        out[(c, r)] = f'{label[:3]} {label[3:]}'
    bad = {k: v for k, v in shape_digit.items() if len(v) > 1}
    assert not bad, f'glyph shapes read as several digits: {list(bad.values())}'
    digit_shapes = Counter(next(iter(v)) for v in shape_digit.values())
    assert len(digit_shapes) == 10, digit_shapes
    return out


def check_fet_geometry(ly, cell, W, L):
    """Channel = active (3/1) minus S/D metal (6/2): width along x must be L, height W, centred on the origin."""
    dbu = ly.dbu
    act = db.Region(cell.shapes(ly.find_layer(3, 1)))
    ch = (act - db.Region(cell.shapes(ly.find_layer(6, 2)))).bbox().to_dtype(dbu)
    assert abs(ch.width() - L) < 1e-3 and abs(ch.height() - W) < 1e-3, (cell.name, ch)
    assert abs(ch.center().x) < 1e-3 and abs(ch.center().y) < 1e-3, (cell.name, ch)
    pad = ly.cell('75um_pad')
    pads = sorted(((i.dcplx_trans * pad.dbbox()).center() for i in cell.each_inst()), key=lambda q: (q.y, q.x))
    assert len(pads) == 3, (cell.name, len(pads))
    sd1, sd2, gate = pads   # bottom-left, bottom-right, top-right
    via = db.Region(cell.shapes(ly.find_layer(4, 1))).bbox().to_dtype(dbu).center()
    assert gate.y > 0 and via.y > 0, (cell.name, gate, via)   # gate via sits above the channel, traced to top pad
    return gate, sd1, sd2


def main():
    ly = db.Layout()
    ly.read(GDS)
    top = ly.cell(TOP)
    labels = printed_ids(ly, top)

    rows, geo = [], {}
    for inst in top.each_inst():
        cell = ly.cell(inst.cell_index)
        m = FET_RE.fullmatch(cell.name)
        if not m:
            continue
        W, L = float(m[1]), float(m[2])
        if cell.name not in geo:
            geo[cell.name] = check_fet_geometry(ly, cell, W, L)
        gate, sd1, sd2 = geo[cell.name]
        na, nb = (inst.na, inst.nb) if inst.is_regular_array() else (1, 1)
        da = db.DVector(inst.da.x, inst.da.y) if inst.is_regular_array() else db.DVector()   # already in um
        dbv = db.DVector(inst.db.x, inst.db.y) if inst.is_regular_array() else db.DVector()
        t = inst.dcplx_trans
        assert t.angle == 0 and not t.is_mirror() and t.mag == 1, (cell.name, t)
        for i in range(na):
            for j in range(nb):
                p = t.disp + da * i + dbv * j   # channel centre (cell origin)
                sc, sr = round((p.x - X0) / PX), round((p.y - Y0) / PY)
                side = 'L' if p.x < X0 + sc * PX else 'R'
                row, col = N_ROWS - sr, sc + 1
                box = f'R{row:02d}C{col:02d}'
                rows.append({
                    'device_id': box + side, 'box_id': box, 'Row': row, 'Column': col,
                    'side': 'left' if side == 'L' else 'right', 'printed_id': labels.get((sc, sr), ''),
                    'W in um': W, 'L in um': L, 'cell': cell.name,
                    'x_mask in um': round(p.x, 3), 'y_mask in um': round(p.y, 3),
                    'gate_pad_dx in um': round(gate.x, 3), 'gate_pad_dy in um': round(gate.y, 3),
                    'sd1_pad_dx in um': round(sd1.x, 3), 'sd1_pad_dy in um': round(sd1.y, 3),
                    'sd2_pad_dx in um': round(sd2.x, 3), 'sd2_pad_dy in um': round(sd2.y, 3),
                })

    ref = min(rows, key=lambda d: (d['y_mask in um'], d['x_mask in um']))   # bottom-left FET
    for d in rows:
        d['x in um'] = round(d['x_mask in um'] - ref['x_mask in um'], 3)
        d['y in um'] = round(d['y_mask in um'] - ref['y_mask in um'], 3)
    rows.sort(key=lambda d: (d['y in um'], d['x in um']))

    ids = Counter(d['device_id'] for d in rows)
    types = Counter((d['W in um'], d['L in um']) for d in rows)
    assert len(rows) == 984 and max(ids.values()) == 1, (len(rows), ids.most_common(3))
    assert len(types) == 24 and set(types.values()) == {41}, types

    fields = ['device_id', 'box_id', 'Row', 'Column', 'side', 'printed_id', 'W in um', 'L in um', 'cell',
              'x in um', 'y in um', 'x_mask in um', 'y_mask in um', 'gate_pad_dx in um', 'gate_pad_dy in um',
              'sd1_pad_dx in um', 'sd1_pad_dy in um', 'sd2_pad_dx in um', 'sd2_pad_dy in um']
    with open(OUT, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    print(f'{OUT}: {len(rows)} FETs, {len(types)} W/L types x 41, origin {ref["device_id"]} ({ref["cell"]})')
    print(f'printed IDs decoded: {len(labels)} (no glyph shape read as two digits); '
          f'FETs in labelled boxes: {sum(bool(d["printed_id"]) for d in rows)}')
    print(f'pads rel. channel: gate {geo[ref["cell"]][0]}, sd1 {geo[ref["cell"]][1]}, sd2 {geo[ref["cell"]][2]}')


if __name__ == '__main__':
    os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
    main()
