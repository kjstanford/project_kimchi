"""A random subset of a device-id map for a shorter run: every site (Row, Column) measured in `per_site` blocks.

    python -m measure.sample_devices --config configs/yanjie_in203_110cycleHfO2_R1_all_idvg.json --per-site 3 \
        --seed 1 --include TR-R01C14 --out configs/yanjie_in203_110cycleHfO2_R1_sample3_devices.csv

The blocks ('region' column) repeat the same sites. Devices already measured (they have an initial scan in the
data folder, as run_map's resume sees it) count toward their site. For each site, the missing copies are drawn at
random from its unmeasured blocks, preferring the blocks with the fewest devices so far (measured + picked), so
the blocks stay balanced. --include devices are always picked: the device the probes are on must be in a run's
device CSV. Writes the picked devices, in the layout CSVs' own schema, to --out; nothing under data/ is written.
"""
import argparse
import json
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

from measure import device_map as dm
from measure import run_map

SITE = ['Row', 'Column']
BLOCK = 'region'


def load_raw(csv_paths) -> pd.DataFrame:
    """The layout CSVs concatenated, with their own column names."""
    paths = csv_paths if isinstance(csv_paths, (list, tuple)) else [csv_paths]
    return pd.concat([pd.read_csv(p) for p in paths], ignore_index=True)


def measured_ids(cfg: dict, data_dir: str) -> set[str]:
    """device_ids with an initial scan already in the data folder (run_map's resume rule)."""
    full = dm.load_devices(cfg['device_csv'])
    return {r['key'] for _, r in full.iterrows() if run_map.already_measured(cfg, r, data_dir)}


def pick(raw: pd.DataFrame, done: set[str], per_site: int, seed: int, include=()) -> list[str]:
    """device_ids to measure so that every site has per_site devices (measured or picked), where it can."""
    rng = np.random.default_rng(seed)
    ids = raw['device_id'].str.upper()
    unknown = {k.upper() for k in include} - set(ids)
    if unknown:
        raise ValueError(f"--include devices not in the map: {sorted(unknown)}")
    chosen = {k.upper() for k in include} - done
    is_done, is_chosen = ids.isin(done), ids.isin(chosen)
    load = Counter(raw.loc[is_done | is_chosen, BLOCK])
    for b in raw[BLOCK].unique():
        load.setdefault(b, 0)
    sites = raw[SITE].drop_duplicates().to_numpy().tolist()
    for i in rng.permutation(len(sites)):
        at = (raw['Row'] == sites[i][0]) & (raw['Column'] == sites[i][1])
        need = per_site - int((at & (is_done | ids.isin(chosen))).sum())
        cand = raw[at & ~is_done & ~ids.isin(chosen)]
        if need <= 0 or cand.empty:
            continue
        order = sorted(cand.index, key=lambda j: (load[raw.at[j, BLOCK]], rng.random()))
        for j in order[:need]:
            chosen.add(ids[j])
            load[raw.at[j, BLOCK]] += 1
    return [k for k in ids if k in chosen]


def main(argv: list[str] | None = None) -> Path:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--config', required=True, help='run config whose device_csv lists the blocks')
    ap.add_argument('--per-site', type=int, required=True)
    ap.add_argument('--seed', type=int, default=1)
    ap.add_argument('--include', default='', help='device_ids always picked, comma-separated')
    ap.add_argument('--data-dir', help="override the config's data_dir (read only)")
    ap.add_argument('--out', required=True)
    a = ap.parse_args(argv)
    out = Path(a.out)
    if any(out.resolve().is_relative_to(Path(d).resolve()) for d in ('data', 'data_k4200', 'data_k4200_stress')):
        ap.error('--out must not be inside a data folder')
    cfg = json.loads(Path(a.config).read_text())
    raw = load_raw(cfg['device_csv'])
    done = measured_ids(cfg, a.data_dir or cfg['data_dir'])
    keys = pick(raw, done, a.per_site, a.seed, [k for k in a.include.split(',') if k])
    sel = raw[raw['device_id'].str.upper().isin(keys)]
    out.parent.mkdir(parents=True, exist_ok=True)
    sel.to_csv(out, index=False)
    per_block = Counter(raw.loc[raw['device_id'].str.upper().isin(done), BLOCK])
    print(f"{out}: {len(sel)} devices to measure ({len(done)} already measured), seed {a.seed}")
    for b, n in sorted(Counter(sel[BLOCK]).items()):
        print(f"  {b}: {n} new + {per_block.get(b, 0)} measured")
    return out


if __name__ == '__main__':
    main()
