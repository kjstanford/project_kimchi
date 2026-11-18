"""Build layouts/localbg_v5_R1_<region>.csv: the 210 transistors of each of the six TFT_region1 blocks of the
local-bottom-gate EBL die layouts/250405_localBG_v5_different_contact_with_big_pads.GDS (15 x 15 mm).

Run from the repo root with the gds env:  make layout-localbg-v5 GDS_PY=".../envs/gds/python.exe"

The die
  Nine TFT blocks on a 3 x 3 grid (3550 um x 2850 um pitch): six TFT_region1 (L x W matrix) and three
  TFT_region2_contact_study (not exported yet). Region names by position: T/M/B (top, middle, bottom) +
  L/C/R (left, centre, right). TFT_region1 sits at TL, TR, ML, MC, BC, BR.
One transistor
  Local bottom gate 1/0 (via 4/0 to the gate pad 5/0, below the channel); source/drain contacts 2/0 (left) and
  3/0 (right), symmetric; S/D pads 6/0 left and right of the channel; channel island on 21/0, 22/0, 23/0, 24/0
  or 0/0 (by width); labels on 7/0, L under the left pad and W under the right pad.
  L = gap between the 2/0 and 3/0 contacts, W = height of the channel island at the channel centre.
A block
  15 rows x 14 columns, 250 um apart in x and 185 um in y. Row 1 is the top row, Column 1 the left column.
  device_id = '<region>-R<row>C<column>', e.g. TL-R15C01.
Coordinates
  'x in um', 'y in um' = channel centre in the die (mask) frame; pad offsets are relative to it
  (sd1 = left pad, sd2 = right pad).
Checks (the script stops on any failure)
  210 devices per block, each in its own grid site; three 80 um pads each; the six blocks identical up to their
  placement; one L per column and one W per row (rows 1-14); every distinct printed label shape belongs to
  exactly one extracted L (or W) value and every value to exactly one label shape.
"""
import csv
import os
from collections import defaultdict

import klayout.db as db

GDS = 'layouts/250405_localBG_v5_different_contact_with_big_pads.GDS'
OUT = 'layouts/localbg_v5_R1_{}.csv'
BLOCK = 'TFT_region1'
PX, PY = 250.0, 185.0            # site pitch in a block, um
N_ROWS, N_COLS = 15, 14
CHANNEL_LAYERS = ('21/0', '22/0', '23/0', '24/0', '0/0')
PAD = 80.0                        # square probe pads, um


def layer_regions(ly, cell):
    """{'layer/datatype': flattened Region of cell}."""
    out = {}
    for li in ly.layer_indexes():
        i = ly.get_info(li)
        out[f'{i.layer}/{i.datatype}'] = db.Region(cell.begin_shapes_rec(li))
    return out


def clipped(reg, box, dbu):
    """Bounding boxes (um) of the pieces of reg inside box (um)."""
    return [p.bbox().to_dtype(dbu) for p in (reg & db.Region(box.to_itype(dbu))).each()]


def containing(polys, x, y, dbu):
    """bbox (um) of the one polygon of polys (merged, dbu) that contains the point (x, y) um."""
    pt = db.DPoint(x, y).to_itype(dbu)
    hits = [p for p in polys if p.inside(pt)]
    assert len(hits) == 1, (x, y, len(hits))
    return hits[0].bbox().to_dtype(dbu)


def label_key(polys, dbu):
    """Shape of one printed label, translated to its own bbox origin (hashable)."""
    bb = db.Box()
    for p in polys:
        bb += p.bbox()
    return tuple(sorted(str(p.moved(-bb.left, -bb.bottom)) for p in polys))


def block_devices(ly, cell):
    """Every transistor of one block, in the block's own frame: geometry, pads and label shapes."""
    dbu = ly.dbu
    R = layer_regions(ly, cell)
    labels = list(R['7/0'].merged().each())
    sd_pads, g_pads = list(R['6/0'].merged().each()), list(R['5/0'].merged().each())
    devs = []
    for via in R['4/0'].merged().each():
        vb = via.bbox().to_dtype(dbu)
        x0 = vb.center().x
        gate = clipped(R['1/0'], db.DBox(x0 - 2, vb.top, x0 + 2, vb.top + 15), dbu)
        top = max(b.top for b in gate)
        near = db.DBox(x0 - 1.2, vb.top, x0 + 1.2, top + 1)
        sc = max(clipped(R['2/0'].merged(), near, dbu), key=lambda b: b.right)    # left contact edge
        dc = min(clipped(R['3/0'].merged(), near, dbu), key=lambda b: b.left)     # right contact edge
        assert sc.right < x0 < dc.left, (x0, sc, dc)
        xc, yc = (sc.right + dc.left) / 2, (sc.center().y + dc.center().y) / 2
        strip = db.DBox(xc - 1e-3, yc - 30, xc + 1e-3, yc + 30)
        chan = {k: clipped(R[k], strip, dbu) for k in CHANNEL_LAYERS if k in R}
        chan = {k: v for k, v in chan.items() if v}
        assert len(chan) == 1 and len(next(iter(chan.values()))) == 1, (xc, yc, chan)
        (ch_layer, (ch,)), = chan.items()
        gw = clipped(R['1/0'], db.DBox(x0 - 4, yc - 1e-3, x0 + 4, yc + 1e-3), dbu)
        assert len(gw) == 1 and gw[0].left < sc.right and gw[0].right > dc.left, (xc, yc, gw)   # gate under the gap
        s_pad = containing(sd_pads, xc - 78, yc, dbu)
        d_pad = containing(sd_pads, xc + 78, yc, dbu)
        g_pad = containing(g_pads, xc, yc - 90, dbu)
        assert abs(s_pad.height() - PAD) < 1e-3 and abs(d_pad.height() - PAD) < 1e-3, (xc, yc, s_pad, d_pad)
        assert abs(g_pad.width() - PAD) < 1e-3, (xc, yc, g_pad)
        lab = {}
        for side, box in (('L', db.DBox(xc - 118, yc - 125, xc - 40, yc - 40)),
                          ('W', db.DBox(xc + 40, yc - 125, xc + 125, yc - 40))):
            polys = [p for p in labels if box.contains(p.bbox().to_dtype(dbu).center())]
            assert polys, (xc, yc, side)
            lab[side] = label_key(polys, dbu)
        devs.append({
            'x': xc, 'y': yc, 'L': dc.left - sc.right, 'W': ch.height(), 'channel_layer': ch_layer,
            'gate_w': gw[0].width(), 'contact_h': sc.height(),
            'sd1': (s_pad.left + PAD / 2 - xc, s_pad.center().y - yc),
            'sd2': (d_pad.right - PAD / 2 - xc, d_pad.center().y - yc),
            'gate': (g_pad.center().x - xc, g_pad.bottom + PAD / 2 - yc),
            'label': lab,
        })
    # grid sites: Row 1 = top, Column 1 = left
    x_left, y_top = min(d['x'] for d in devs), max(d['y'] for d in devs)
    for d in devs:
        d['Row'], d['Column'] = round((y_top - d['y']) / PY) + 1, round((d['x'] - x_left) / PX) + 1
        assert abs(d['x'] - x_left - (d['Column'] - 1) * PX) < 1e-3, d
        assert abs(y_top - d['y'] - (d['Row'] - 1) * PY) < 2.0, d          # row 14 sits 1.18 um low
    sites = {(d['Row'], d['Column']) for d in devs}
    assert len(devs) == N_ROWS * N_COLS and sites == {(r, c) for r in range(1, N_ROWS + 1)
                                                      for c in range(1, N_COLS + 1)}, len(devs)
    return devs


def check_tables(devs):
    """One L per column and one W per row in rows 1-14; label shapes <-> values one-to-one."""
    for c in range(1, N_COLS + 1):
        Ls = {round(d['L'], 4) for d in devs if d['Column'] == c and d['Row'] < N_ROWS - 1}
        assert len(Ls) == 1, (c, Ls)
    for r in range(1, N_ROWS):
        Ws = {round(d['W'], 4) for d in devs if d['Row'] == r}
        assert len(Ws) == 1, (r, Ws)
    for side in ('L', 'W'):
        by_key, by_val = defaultdict(set), defaultdict(set)
        for d in devs:
            by_key[d['label'][side]].add(round(d[side], 4))
            by_val[round(d[side], 4)].add(d['label'][side])
        bad = [v for v in by_key.values() if len(v) > 1] + [k for k, v in by_val.items() if len(v) > 1]
        assert not bad, (side, bad)


def main():
    ly = db.Layout()
    ly.read(GDS)
    top = ly.top_cell()
    blocks = []
    for inst in top.each_inst():
        name = ly.cell(inst.cell_index).name
        if not name.startswith('TFT_region'):
            continue
        t = inst.dcplx_trans
        assert t.angle == 0 and not t.is_mirror() and t.mag == 1, (name, t)
        n = (inst.na, inst.nb) if inst.is_regular_array() else (1, 1)
        for i in range(n[0]):
            for j in range(n[1]):
                d = t.disp + (inst.da * i if i else db.DVector()) + (inst.db * j if j else db.DVector())
                blocks.append((name, d))
    xs, ys = sorted({round(d.x) for _, d in blocks}), sorted({round(d.y) for _, d in blocks}, reverse=True)
    assert len(blocks) == 9 and len(xs) == 3 and len(ys) == 3, blocks
    region = lambda d: 'TMB'[ys.index(round(d.y))] + 'LCR'[xs.index(round(d.x))]

    devs = block_devices(ly, ly.cell(BLOCK))
    check_tables(devs)
    ref = sorted((d['Row'], d['Column'], round(d['L'], 4), round(d['W'], 4), d['channel_layer']) for d in devs)
    fields = ['device_id', 'region', 'Row', 'Column', 'W in um', 'L in um', 'channel_layer', 'gate_width in um',
              'gate_overlap in um', 'contact_height in um', 'x in um', 'y in um',
              'gate_pad_dx in um', 'gate_pad_dy in um', 'sd1_pad_dx in um', 'sd1_pad_dy in um',
              'sd2_pad_dx in um', 'sd2_pad_dy in um']
    r3 = lambda v: round(v, 3) + 0.0
    written = []
    for name, disp in blocks:
        if name != BLOCK:
            continue
        reg = region(disp)
        rows = []
        for d in sorted(devs, key=lambda d: (d['Row'], d['Column'])):
            rows.append({
                'device_id': f"{reg}-R{d['Row']:02d}C{d['Column']:02d}", 'region': reg,
                'Row': d['Row'], 'Column': d['Column'], 'W in um': r3(d['W']), 'L in um': r3(d['L']),
                'channel_layer': d['channel_layer'], 'gate_width in um': r3(d['gate_w']),
                'gate_overlap in um': r3((d['gate_w'] - d['L']) / 2), 'contact_height in um': r3(d['contact_h']),
                'x in um': r3(d['x'] + disp.x), 'y in um': r3(d['y'] + disp.y),
                'gate_pad_dx in um': r3(d['gate'][0]), 'gate_pad_dy in um': r3(d['gate'][1]),
                'sd1_pad_dx in um': r3(d['sd1'][0]), 'sd1_pad_dy in um': r3(d['sd1'][1]),
                'sd2_pad_dx in um': r3(d['sd2'][0]), 'sd2_pad_dy in um': r3(d['sd2'][1]),
            })
        # each placement must hold the same devices as the block cell (it is one cell, placed six times)
        assert sorted((r['Row'], r['Column'], round(r['L in um'], 4), round(r['W in um'], 4), r['channel_layer'])
                      for r in rows) == [(a, b, round(c, 3), round(e, 3), f) for a, b, c, e, f in ref]
        path = OUT.format(reg)
        with open(path, 'w', newline='') as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)
        written.append(reg)
        print(f'{path}: {len(rows)} transistors, block at ({disp.x:g}, {disp.y:g}) um')
    assert sorted(written) == ['BC', 'BR', 'MC', 'ML', 'TL', 'TR'], written
    Ls = sorted({round(d['L'] * 1e3) for d in devs})
    Ws = sorted({round(d['W'] * 1e3) for d in devs})
    print(f'L (nm): {Ls}\nW (nm): {Ws}')
    print('label shapes <-> L and W values: one-to-one')


if __name__ == '__main__':
    os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
    main()
