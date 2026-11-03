"""One place that turns measurement values (numpy scalars, NaN, paths) into strict JSON."""
import math

import numpy as np


def clean(obj):
    """Recursively convert to JSON-safe types: NaN/inf -> None, numpy scalars/arrays -> Python."""
    if isinstance(obj, dict):
        return {str(k): clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [clean(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return clean(obj.tolist())
    if isinstance(obj, np.generic):
        obj = obj.item()
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if obj is None or isinstance(obj, (bool, int, str)):
        return obj
    return str(obj)
