"""Build layouts/igzo_v6_GC_FETs.csv: both FETs of every 2T gain cell with a storage-node pad (cells GC_*,
not the *_No_Pad versions) in the IGZO v6 mask; and layouts/igzo_v6_GC_cells.csv: one row per gain cell,
6-pad and 4-pad (_No_Pad), for the 4-probe gain-cell measurements (RBL, RWL, WBL, WWL).

Run from the repo root with the gds env:  python -B layouts/make_igzo_v6_gc_csv.py

Structure
  GC_<write LW>_<read LW> (e.g. GC_L1W1_L2W4: write W1/L1, read W4/L2) holds two standard 3T FET sub-cells
  435 um apart, each with the 3T pad pattern of make_igzo_v6_fet_csv.py. Left = read FET, right = write FET.
  A top-metal (6/2) strip ties the read gate pad to the write sd1 pad (the storage node); checked here.
  The padded cells sit in the even Columns 4-16 (one type per column), Rows 2-42; odd Columns 5-17 hold the
  _No_Pad versions (one box to the right of their 6-pad twin), which have no FET sub-cells: the same two
  channels are drawn flat at the same spots, with only 4 pads - RBL (read sd1), RWL (read sd2), WBL (write sd2),
  WWL (write gate), exactly where the twin has them - and the storage node a pad-less 6/2 strip. Checked here.

Naming (same box grid as the 3T FETs, so ids never collide: GC Columns 4-17, 3T Columns 18-29)
  FET CSV: box_id R20C08, device_id R20C08L = read FET, R20C08R = write FET (6-pad cells only).
  Cell CSV: device_id = box_id (R20C08 6-pad, R20C09 4-pad); x/y = read-FET channel centre, so R20C08 and
  R20C08L are the same stage position; W/L = read FET; pad offsets relative to the read channel.
  x/y in um are relative to the 3T origin FET R42C18L, so these CSVs can be combined with igzo_v6_3T_FETs.csv.
"""
import csv
import os
import re
from collections import Counter

import klayout.db as db

import make_igzo_v6_fet_csv as fet

OUT = 'layouts/igzo_v6_GC_FETs.csv'
OUT_CELLS = 'layouts/igzo_v6_GC_cells.csv'
FET_CSV = 'layouts/igzo_v6_3T_FETs.csv'
GC_RE = re.compile(r'GC_L(\d+)W(\d+)_L(\d+)W(\d+)')
NO_PAD_RE = re.compile(r'(GC_L\d+W\d+_L\d+W\d+)_No_pad', re.IGNORECASE)
PROBED = ('rbl', 'rwl', 'wbl', 'wwl')


def cell_pads(read, write):
    """Pad centres of a padded GC in its own frame: the 4 probed lines + the 2 storage-node pads."""
    at = lambda s, k: db.DPoint(s[3].x, s[3].y) + db.DVector(s[4][k].x, s[4][k].y)
    return {'rbl': at(read, 'sd1'), 'rwl': at(read, 'sd2'), 'sn_read': at(read, 'gate'),
            'sn_write': at(write, 'sd1'), 'wbl': at(write, 'sd2'), 'wwl': at(write, 'gate')}


def no_pad_geometry(ly, cell, twin):
    """Check a _No_Pad cell against its padded twin (gc_geometry result): same read/write channels (spot and
    W/L), its 4 pads = the twin's RBL/RWL/WBL/WWL, 5 nets on 6/2 with the storage node the one without a pad,
    joining the read gate via and the write channel."""
    read, write = twin
    dbu = ly.dbu
    ch = (db.Region(cell.begin_shapes_rec(ly.find_layer(3, 1)))
          - db.Region(cell.begin_shapes_rec(ly.find_layer(6, 2)))).merged()
    chans = sorted((p.bbox().to_dtype(dbu) for p in ch.each()), key=lambda b: b.center().x)
    assert len(chans) == 2, (cell.name, chans)
    for b, s in zip(chans, (read, write)):
        assert (b.center() - db.DPoint(s[3].x, s[3].y)).length() < 1e-3, (cell.name, b, s[3])
        assert abs(b.width() - s[2]) < 1e-3 and abs(b.height() - s[1]) < 1e-3, (cell.name, b, s[:3])
    pad = ly.cell('75um_pad')
    got = sorted(((i.dcplx_trans * pad.dbbox()).center() for i in cell.each_inst()
                  if i.cell_index == pad.cell_index()), key=lambda q: (q.x, q.y))
    want = cell_pads(read, write)
    assert len(got) == 4 and all(min((g - want[k]).length() for g in got) < 1e-3 for k in PROBED), (cell.name, got)

    metal = db.Region(cell.begin_shapes_rec(ly.find_layer(6, 2))).merged()
    polys = list(metal.each())
    assert len(polys) == 5, (cell.name, len(polys))
    hits = [[k for k, poly in enumerate(polys) if poly.inside(want[n].to_itype(dbu))] for n in PROBED]
    assert all(len(h) == 1 for h in hits) and len({h[0] for h in hits}) == 4, (cell.name, hits)
    sn = db.Region(polys[({*range(5)} - {h[0] for h in hits}).pop()])
    via = db.Region(cell.begin_shapes_rec(ly.find_layer(4, 1))).merged()
    rvia = [v for v in via.each() if abs(v.bbox().to_dtype(dbu).center().x - read[3].x) < 1]
    assert rvia and not sn.interacting(db.Region(rvia[0])).is_empty(), (cell.name, 'SN not on the read gate via')
    wch = db.Region(chans[1].enlarged(0.5, 0).to_itype(dbu))
    assert not sn.interacting(wch).is_empty(), (cell.name, 'SN not on the write channel')


def gc_geometry(ly, cell):
    """(read, write) sub-FETs of a padded GC cell: (cell name, W, L, channel centre, pads) each, local coords.
    Checks the 3T geometry of both and that the read gate pad and write sd1 pad are one 6/2 net."""
    subs = []
    for i in cell.each_inst():
        sub = ly.cell(i.cell_index)
        m = fet.FET_RE.fullmatch(sub.name)
        if m:
            t = i.dcplx_trans
            assert t.angle == 0 and not t.is_mirror() and t.mag == 1, (cell.name, sub.name, t)
            W, L = float(m[1]), float(m[2])
            gate, sd1, sd2 = fet.check_fet_geometry(ly, sub, W, L)
            subs.append((sub.name, W, L, t.disp, {'gate': gate, 'sd1': sd1, 'sd2': sd2}))
    assert len(subs) == 2, (cell.name, [s[0] for s in subs])
    read, write = sorted(subs, key=lambda s: s[3].x)

    m = GC_RE.fullmatch(cell.name)   # GC_L<write L>W<write W>_L<read L>W<read W>
    assert (write[2], write[1], read[2], read[1]) == tuple(float(v) for v in m.groups()), (cell.name, read, write)

    metal = db.Region(cell.begin_shapes_rec(ly.find_layer(6, 2))).merged()
    pads = {(role, k): (s[3] + db.DVector(p.x, p.y)).to_itype(ly.dbu)
            for role, s in (('read', read), ('write', write)) for k, p in s[4].items()}
    def net(pt):
        hit = [k for k, poly in enumerate(metal.each()) if poly.inside(pt)]
        assert len(hit) == 1, (cell.name, pt, hit)
        return hit[0]
    nets = {k: net(p) for k, p in pads.items()}
    node = nets[('read', 'gate')]
    assert [k for k, v in nets.items() if v == node] == [('read', 'gate'), ('write', 'sd1')], (cell.name, nets)
    assert len(set(nets.values())) == 5, (cell.name, nets)   # 6 pads, 5 nets
    return read, write


def main():
    ly = db.Layout()
    ly.read(fet.GDS)
    top = ly.cell(fet.TOP)
    labels = fet.printed_ids(ly, top)
    with open(FET_CSV, newline='') as f:
        ref = next(d for d in csv.DictReader(f) if d['device_id'] == 'R42C18L')
    rx, ry = float(ref['x_mask in um']), float(ref['y_mask in um'])

    rows, cells, geo, no_pad = [], [], {}, Counter()
    for inst in top.each_inst():
        cell = ly.cell(inst.cell_index)
        if not cell.name.startswith('GC_'):
            continue
        m = NO_PAD_RE.fullmatch(cell.name)
        twin = m[1] if m else cell.name
        assert GC_RE.fullmatch(twin), cell.name
        if twin not in geo:
            geo[twin] = gc_geometry(ly, ly.cell(twin))
        read, write = geo[twin]
        if m:
            if cell.name not in no_pad:
                no_pad_geometry(ly, cell, geo[twin])
            no_pad[cell.name] += inst.na * inst.nb if inst.is_regular_array() else 1
        cpads = cell_pads(read, write)
        t = inst.dcplx_trans
        assert t.angle == 0 and not t.is_mirror() and t.mag == 1, (cell.name, t)
        na, nb = (inst.na, inst.nb) if inst.is_regular_array() else (1, 1)
        da = db.DVector(inst.da.x, inst.da.y) if inst.is_regular_array() else db.DVector()
        dbv = db.DVector(inst.db.x, inst.db.y) if inst.is_regular_array() else db.DVector()
        for i in range(na):
            for j in range(nb):
                disp = t.disp + da * i + dbv * j
                c = cell.dbbox().center() + disp
                sc, sr = round((c.x - fet.X0) / fet.PX), round((c.y - fet.Y0) / fet.PY)
                row, col = fet.N_ROWS - sr, sc + 1
                box = f'R{row:02d}C{col:02d}'
                rd = db.DPoint(read[3].x, read[3].y)       # read channel centre, cell frame
                p = rd + disp
                cells.append({
                    'device_id': box, 'box_id': box, 'Row': row, 'Column': col,
                    'printed_id': labels.get((sc, sr), ''), 'pads': 4 if m else 6, 'gc_type': cell.name,
                    'twin_id': f'R{row:02d}C{col + (-1 if m else 1):02d}',
                    'W in um': read[1], 'L in um': read[2], 'write_W in um': write[1], 'write_L in um': write[2],
                    'x in um': round(p.x - rx, 3), 'y in um': round(p.y - ry, 3),
                    'x_mask in um': round(p.x, 3), 'y_mask in um': round(p.y, 3),
                    'write_dx in um': round(write[3].x - read[3].x, 3),
                    **{f'{k}_pad_d{a} in um': round(getattr(cpads[k] - rd, a), 3)
                       for k in PROBED + (() if m else ('sn_read', 'sn_write')) for a in ('x', 'y')},
                })
                if m:
                    continue
                for side, role, (sub, W, L, d, pads), storage in (('L', 'read', read, 'gate'),
                                                                  ('R', 'write', write, 'sd1')):
                    p = disp + d   # channel centre
                    rows.append({
                        'device_id': box + side, 'box_id': box, 'Row': row, 'Column': col,
                        'side': 'left' if side == 'L' else 'right', 'printed_id': labels.get((sc, sr), ''),
                        'W in um': W, 'L in um': L, 'cell': sub,
                        'x in um': round(p.x - rx, 3), 'y in um': round(p.y - ry, 3),
                        'x_mask in um': round(p.x, 3), 'y_mask in um': round(p.y, 3),
                        'gate_pad_dx in um': round(pads['gate'].x, 3), 'gate_pad_dy in um': round(pads['gate'].y, 3),
                        'sd1_pad_dx in um': round(pads['sd1'].x, 3), 'sd1_pad_dy in um': round(pads['sd1'].y, 3),
                        'sd2_pad_dx in um': round(pads['sd2'].x, 3), 'sd2_pad_dy in um': round(pads['sd2'].y, 3),
                        'role': role, 'gc_type': cell.name, 'partner_id': box + ('R' if side == 'L' else 'L'),
                        'storage_pad': storage,
                    })
    rows.sort(key=lambda d: (d['y in um'], d['x in um']))

    ids = Counter(d['device_id'] for d in rows)
    types = Counter(d['gc_type'] for d in rows)
    assert len(rows) == 574 and max(ids.values()) == 1, (len(rows), ids.most_common(3))
    assert len(types) == 7 and set(types.values()) == {82}, types
    assert {d['Column'] for d in rows} == set(range(4, 17, 2)), sorted({d['Column'] for d in rows})
    assert {d['Row'] for d in rows} == set(range(2, 43)), sorted({d['Row'] for d in rows})

    cells.sort(key=lambda d: (d['y in um'], d['x in um']))
    by_id = {d['device_id']: d for d in cells}
    assert len(cells) == 574 and len(by_id) == 574, len(cells)
    assert sorted(no_pad.values()) == [41] * 7, no_pad
    for d in cells:
        tw = by_id[d['twin_id']]
        assert d['Column'] % 2 == (1 if d['pads'] == 4 else 0) and 4 <= d['Column'] <= 17, d
        assert tw['twin_id'] == d['device_id'] and tw['pads'] + d['pads'] == 10, (d, tw)
        assert round(abs(d['x in um'] - tw['x in um']), 3) == fet.PX and d['y in um'] == tw['y in um'], (d, tw)
        assert all(d[f'{k}_pad_d{a} in um'] == tw[f'{k}_pad_d{a} in um'] for k in PROBED for a in 'xy'), (d, tw)
    for d in rows:   # the read FET of a 6-pad cell is the cell's stage position
        if d['role'] == 'read':
            assert (by_id[d['box_id']]['x in um'], by_id[d['box_id']]['y in um']) == (d['x in um'], d['y in um'])

    for path, data in ((OUT, rows), (OUT_CELLS, cells)):
        fields = list(max(data, key=len))
        with open(path, 'w', newline='') as f:
            w = csv.DictWriter(f, fieldnames=fields, restval='')
            w.writeheader()
            w.writerows(data)

    print(f'{OUT}: {len(rows)} FETs = {len(types)} padded GC types x 41 cells x (read + write)')
    print(f'{OUT_CELLS}: {len(cells)} cells = 287 6-pad (even Columns 4-16) + {sum(no_pad.values())} 4-pad '
          f'_No_Pad (odd Columns 5-17, twin + 765 um); 4-pad geometry = twin RBL/RWL/WBL/WWL, SN pad-less, verified')
    for name in sorted(types, key=lambda n: next(d['Column'] for d in rows if d['gc_type'] == n)):
        read, write = geo[name]
        colm = next(d['Column'] for d in rows if d['gc_type'] == name)
        print(f'  C{colm:02d} {name:16s} read {read[0]:11s} (L)  write {write[0]:11s} (R)')
    print(f'storage node (read gate pad = write sd1 pad) verified on 6/2 for every type; '
          f'FETs in labelled boxes: {sum(bool(d["printed_id"]) for d in rows)}')


if __name__ == '__main__':
    os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
    main()
