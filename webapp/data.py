"""Read-only access to saved measurement data for the browser.

Roots are fixed folders of the repository (data/, data_k4200/, data_k4200_stress/ and the web dry-run folder).
Nothing here writes, renames or deletes; every path is resolved and must stay inside its root.
"""
import os

import pandas as pd

from webapp.jsonsafe import clean

DATA_ROOTS = ('data', 'data_k4200', 'data_k4200_stress', '.ignore/web_dryrun')
SHOWN = ('.csv', '.png', '.log')
MAX_POINTS = 200000

# default axes per file prefix: (x column, y columns, log y)
DEFAULT_AXES = [
    ('IdVg_', 'Gate_Voltage', ['Drain_Current', 'Gate_Current'], True),
    ('GC_WBL_sweep_', 'WBL_Voltage', ['RBL_Current', 'WBL_Current', 'WWL_Current'], True),
    ('summary_', None, [], False),
]


class DataError(Exception):
    pass


def roots(base='.'):
    return [r for r in DATA_ROOTS if os.path.isdir(os.path.join(base, r))]


def resolve(root, rel='', base='.'):
    if root not in DATA_ROOTS:
        raise DataError(f"unknown data root {root!r}")
    top = os.path.realpath(os.path.join(base, root))
    path = os.path.realpath(os.path.join(top, rel))
    if path != top and not path.startswith(top + os.sep):
        raise DataError("path outside the data root")
    if not os.path.exists(path):
        raise DataError(f"not found: {root}/{rel}")
    return path


def listdir(root, rel='', base='.'):
    """-> {'dirs': [...], 'files': [{'name', 'size', 'mtime'}]}, folders first, newest files first."""
    path = resolve(root, rel, base)
    if not os.path.isdir(path):
        raise DataError("not a folder")
    dirs, files = [], []
    for e in os.scandir(path):
        if e.is_dir():
            dirs.append(e.name)
        elif e.name.lower().endswith(SHOWN):
            st = e.stat()
            files.append({'name': e.name, 'size': st.st_size, 'mtime': st.st_mtime})
    return {'root': root, 'path': rel, 'dirs': sorted(dirs), 'files': sorted(files, key=lambda f: -f['mtime'])}


def default_axes(name, columns):
    for prefix, x, ys, logy in DEFAULT_AXES:
        if name.startswith(prefix):
            return {'x': x if x in columns else None, 'y': [y for y in ys if y in columns], 'logy': logy}
    return {'x': columns[0] if columns else None, 'y': columns[1:2], 'logy': False}


def read_table(root, rel, base='.'):
    """A CSV as columns -> values (NaN -> null), with suggested axes."""
    path = resolve(root, rel, base)
    if not path.lower().endswith('.csv'):
        raise DataError("not a CSV file")
    df = pd.read_csv(path)
    truncated = len(df) > MAX_POINTS
    df = df.head(MAX_POINTS)
    cols = [str(c) for c in df.columns]
    numeric = [str(c) for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]
    name = os.path.basename(path)
    return clean({'name': name, 'columns': cols, 'numeric': numeric, 'n_rows': len(df), 'truncated': truncated,
                  'data': {str(c): df[c].tolist() for c in df.columns},
                  'axes': default_axes(name, numeric)})


def read_text(root, rel, base='.', max_bytes=2_000_000):
    path = resolve(root, rel, base)
    if not path.lower().endswith('.log'):
        raise DataError("not a log file")
    with open(path, encoding='utf-8', errors='replace') as f:
        return f.read(max_bytes)
