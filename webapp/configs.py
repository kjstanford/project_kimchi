"""Run configs: list and load configs/*.json, validate a config, and preview its probe plan.

Everything here reuses measure.run_map / measure.device_map and only reads files (the plan preview reads
data_dir to mark devices that are already measured, exactly as a run would skip them).
"""
import json
import os

from measure import device_map as dm
from measure import run_map

CONFIG_DIR = 'configs'
REQUIRED = {'sample': ('name', 'die_idx'), 'motion': ('transform', 'max_step_um', 'bounds_margin_um'),
            'instruments': ('prober_resource',)}
RUN_OPTIONS = ('only', 'columns', 'L_values', 'limit', 'origin', 'keep_order', 'origin_offset')


def list_configs(config_dir=CONFIG_DIR):
    return sorted(f for f in os.listdir(config_dir) if f.endswith('.json'))


def load_config(name, config_dir=CONFIG_DIR):
    """name must be a bare file name inside config_dir (no paths)."""
    if os.path.basename(name) != name or not name.endswith('.json'):
        raise ValueError(f"not a config name: {name!r}")
    path = os.path.join(config_dir, name)
    if not os.path.isfile(path):
        raise FileNotFoundError(name)
    with open(path) as f:
        return json.load(f)


def validate(cfg):
    """-> {'ok', 'errors', 'experiment', 'smu_tool'}. Runs the same checks a run makes before connecting."""
    errors = []
    if not isinstance(cfg, dict):
        return {'ok': False, 'errors': ['config must be a JSON object'], 'experiment': None, 'smu_tool': None}
    for k in ('device_csv', 'data_dir', 'origin_device', 'smu_map'):
        if k not in cfg:
            errors.append(f"missing '{k}'")
    for block, keys in REQUIRED.items():
        if not isinstance(cfg.get(block), dict):
            errors.append(f"missing block '{block}'")
            continue
        errors += [f"missing '{block}.{k}'" for k in keys if k not in cfg[block]]
    exp = tool = None
    try:
        exp = run_map.experiment(cfg)
    except (ValueError, KeyError) as e:
        errors.append(str(e))
    try:
        tool = run_map.smu_tool(cfg)[0]
    except (ValueError, KeyError) as e:
        errors.append(str(e))
    csvs = cfg.get('device_csv')
    for p in (csvs if isinstance(csvs, list) else [csvs] if csvs else []):
        if not os.path.isfile(p):
            errors.append(f"device_csv not found: {p}")
    for seg in cfg.get('segments') or []:
        if not os.path.isfile(seg.get('device_csv', '')):
            errors.append(f"segment device_csv not found: {seg.get('device_csv')}")
    if not errors and exp:
        try:
            if exp == 'stress_stair':
                from measure import stress_stair
                stress_stair.check_config(cfg)
            elif exp in run_map.GC_EXPERIMENTS:
                run_map.GC_FUNCS[exp][0](cfg)
        except Exception as e:      # the experiment's own check: any rejection is reported, not raised
            errors.append(f"{exp} settings: {e}")
    return {'ok': not errors, 'errors': errors, 'experiment': exp, 'smu_tool': tool}


def check_options(options):
    options = dict(options or {})
    unknown = set(options) - set(RUN_OPTIONS)
    if unknown:
        raise ValueError(f"unknown run options: {sorted(unknown)}")
    out = {k: v for k, v in options.items() if v not in (None, '', [])}
    for k in ('columns', 'L_values'):
        if isinstance(out.get(k), str):
            out[k] = [v.strip() for v in out[k].split(',') if v.strip()]
    if 'limit' in out:
        out['limit'] = int(out['limit'])
        if out['limit'] < 1:
            raise ValueError("limit must be >= 1")
    if isinstance(out.get('origin_offset'), str):
        out['origin_offset'] = tuple(float(v) for v in out['origin_offset'].split(','))
    if 'origin_offset' in out:
        if len(out['origin_offset']) != 2:
            raise ValueError("origin_offset must be 'X,Y' in layout um")
        out['origin_offset'] = tuple(float(v) for v in out['origin_offset'])
    return out


def site_id(c, key, multi):
    """The id a device has on the map: its key, or '<die> Lc<n>|<key>' in a multi-segment run."""
    return f"{run_map.segment_label(c)}|{key}" if multi else key


def plan_entry(p, cfg, multi):
    c = p.get('cfg', cfg)
    return {'site': site_id(c, p['key'], multi), 'key': p['key'], 'L': p.get('L'), 'W': run_map.device_W(c, p),
            'tx': p['tx'], 'ty': p['ty'], 'dx': p['dx'], 'dy': p['dy'], 'hops': p.get('hops', 1),
            'segment': run_map.segment_label(c) if multi else None}


def preview(cfg, options=None, data_dir=None):
    """Plan the run without connecting: every site on the stage (um relative to the origin device) and the
    devices in visiting order. -> {'sites', 'plan', 'origin', 'log'}; raises ValueError on an invalid plan."""
    opts = check_options(options)
    data_dir = data_dir or cfg['data_dir']
    lines = []
    multi = bool(cfg.get('segments'))
    origin_key = dm.parse_keys(opts.get('origin') or cfg['origin_device'])[0]
    t = cfg['motion']['transform']
    if multi:
        plan, sites = run_map.build_multi_plan(cfg, origin_key, opts.get('limit'), data_dir, lines.append)
        o = sites.set_index('key').loc[f'origin|{origin_key}']
        off = (0.0, 0.0)
    else:
        plan, sites = run_map.build_plan(cfg, opts.get('only'), opts.get('columns'), opts.get('L_values'),
                                         opts.get('limit'), origin_key, data_dir, lines.append,
                                         opts.get('keep_order', False), opts.get('origin_offset'))
        o = sites.set_index('key').loc[origin_key]
        off = opts.get('origin_offset') or (0.0, 0.0)
    ox, oy = dm.to_stage(o['x'] + off[0], o['y'] + off[1], **t)
    out_sites = []
    for _, r in sites.iterrows():
        sx, sy = dm.to_stage(r['x'], r['y'], **t)
        out_sites.append({'site': r['key'], 'x': sx - ox, 'y': sy - oy, 'L': r.get('L'),
                          'origin': r['key'] in (origin_key, f'origin|{origin_key}')})
    planned = {p['site'] for p in (plan_entry(q, cfg, multi) for q in plan)}
    measured = []
    if not multi:
        for _, r in sites.iterrows():
            if r['key'] not in planned and run_map.already_measured(cfg, r, data_dir):
                measured.append(r['key'])
    return {'origin': origin_key, 'multi': multi, 'sites': out_sites, 'measured': measured,
            'plan': [plan_entry(p, cfg, multi) for p in plan], 'log': lines}
