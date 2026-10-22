"""Device list from the layout CSV: filtering, serpentine ordering, layout->stage transform."""
import math

import pandas as pd

COLS = {'lc': 'Lc in nm', 'L': 'L in um', 'col': 'Column', 'x': 'x in um', 'y': 'y in um'}
# IGZO v6 style CSV (layouts/make_igzo_v6_fet_csv.py): devices keyed by device_id, W per device
COLS_ID = {'key': 'device_id', 'col': 'Column', 'L': 'L in um', 'W': 'W in um', 'x': 'x in um', 'y': 'y in um'}


def device_key(col, L):
    return f"{col}:{float(L):g}"


def load_devices(csv_path):
    """csv_path: one CSV, or a list of CSVs in the same frame and schema (e.g. 3T FETs + gain-cell FETs),
    concatenated so any of their devices can be the origin."""
    if isinstance(csv_path, (list, tuple)):
        dfs = [pd.read_csv(p) for p in csv_path]
        if len({'device_id' in d.columns for d in dfs}) > 1:
            raise ValueError(f"{csv_path}: mixed device_id / Column:L schemas")
        df = pd.concat(dfs, ignore_index=True)
    else:
        df = pd.read_csv(csv_path)
    cols = COLS_ID if 'device_id' in df.columns else COLS
    missing = set(cols.values()) - set(df.columns)
    if missing:
        raise ValueError(f"{csv_path} is missing columns {missing}")
    df = df[list(cols.values()) + [c for c in df.columns if c not in cols.values()]]
    df = df.rename(columns={v: k for k, v in cols.items()})
    if cols is COLS_ID:
        df['key'] = df['key'].str.upper()
        df['lc'] = float('nan')
    else:
        df['key'] = [device_key(c, L) for c, L in zip(df['col'], df['L'])]
    if df['key'].duplicated().any():
        raise ValueError(f"Duplicate device keys: {df.loc[df['key'].duplicated(), 'key'].tolist()}")
    return df


def parse_keys(spec):
    """'A:2,B:0.04' -> ['A:2', 'B:0.04'] (Column:L); tokens without ':' are device_ids ('r42c29r' -> 'R42C29R')."""
    if not spec:
        return []
    out = []
    for tok in spec.split(','):
        if ':' in tok:
            col, L = tok.strip().split(':')
            out.append(device_key(col.strip().upper(), L))
        else:
            out.append(tok.strip().upper())
    return out


def filter_devices(df, only=None, columns=None, L_values=None):
    if only:
        keys = parse_keys(only)
        unknown = set(keys) - set(df['key'])
        if unknown:
            raise ValueError(f"Unknown devices: {sorted(unknown)}")
        df = df[df['key'].isin(keys)]
    if columns:
        df = df[df['col'].astype(str).str.upper().isin([str(c).strip().upper() for c in columns])]
    if L_values:
        df = df[df['L'].round(6).isin([round(float(v), 6) for v in L_values])]
    return df


def serpentine(df, row_tol_um=1.0, first_row='left'):
    """Bottom row first, starting from the `first_row` side ('left' = A->L, 'right' = L->A);
    then alternate direction on each row up."""
    if first_row not in ('left', 'right'):
        raise ValueError("first_row must be 'left' or 'right'")
    df = df.copy()
    df['row'] = (df['y'] / row_tol_um).round().astype(int)
    rows = sorted(df['row'].unique())
    flip = first_row == 'right'
    parts = [df[df['row'] == r].sort_values('x', ascending=((i % 2 == 0) != flip)) for i, r in enumerate(rows)]
    return pd.concat(parts).drop(columns='row').reset_index(drop=True)


def to_stage(x, y, rotate_deg=0, mirror_x=False, mirror_y=False):
    """Layout (x, y) -> stage (x, y). Mirror first, then rotate counter-clockwise."""
    if mirror_x:
        x = -x
    if mirror_y:
        y = -y
    if rotate_deg % 90:
        raise ValueError("rotate_deg must be a multiple of 90")
    t = math.radians(rotate_deg)
    c, s = round(math.cos(t)), round(math.sin(t))
    return c * x - s * y, s * x + c * y


def plan_moves(ordered, origin_key, transform, max_step_um, bounds_margin_um, full_df, split_long=False):
    """Stage targets relative to the device the probes are currently landed on (origin_key).

    Returns a list of dicts: device row fields + target (tx, ty), step (dx, dy) in stage um and
    hops = number of equal sub-moves for that step. A step longer than max_step_um raises, or with
    split_long=True is split into hops of at most max_step_um each.
    Raises before anything moves if any step or target violates the limits.
    """
    if origin_key not in set(full_df['key']):
        raise ValueError(f"Origin device {origin_key} is not in the device CSV")
    o = full_df.set_index('key').loc[origin_key]
    ox, oy = to_stage(o['x'], o['y'], **transform)
    xs, ys = zip(*(to_stage(x, y, **transform) for x, y in zip(full_df['x'], full_df['y'])))
    lo_x, hi_x = min(xs) - ox - bounds_margin_um, max(xs) - ox + bounds_margin_um
    lo_y, hi_y = min(ys) - oy - bounds_margin_um, max(ys) - oy + bounds_margin_um

    plan, cx, cy = [], 0.0, 0.0
    for _, r in ordered.iterrows():
        sx, sy = to_stage(r['x'], r['y'], **transform)
        tx, ty = sx - ox, sy - oy
        if not (lo_x <= tx <= hi_x and lo_y <= ty <= hi_y):
            raise ValueError(f"Target for {r['key']} ({tx:.1f}, {ty:.1f}) is outside the map bounds")
        dx, dy = tx - cx, ty - cy
        hops = 1
        if math.hypot(dx, dy) > max_step_um:
            if not split_long:
                raise ValueError(f"Step to {r['key']} ({dx:.1f}, {dy:.1f}) exceeds max_step_um={max_step_um}")
            hops = math.ceil(math.hypot(dx, dy) / max_step_um)
        plan.append({**r.to_dict(), 'tx': tx, 'ty': ty, 'dx': dx, 'dy': dy, 'hops': hops})
        cx, cy = tx, ty
    return plan
