"""Keysight B1500A connection.

Adapted from S200_B1500A_4200A_py_setup/b1500A_utils/initialize_smus.py.
Nothing here touches hardware at import time; connect() must be called explicitly.
"""

RESOURCE_STR = 'GPIB0::16::INSTR'

# pymeasure's map plus the B1530A WGFMU, which pymeasure does not know
MODULE_NAMES = {
    'B1510A': 'HPSMU',
    'B1511A': 'MPSMU',
    'B1511B': 'MPSMU',
    'B1517A': 'HRSMU',
    'B1520A': 'MFCMU',
    'B1525A': 'HVSPGU',
    'B1530A': 'WGFMU',
}


def connect(resource_str=RESOURCE_STR, timeout_ms=600000, log=print):
    """Open the real B1500 session (sends UNT? 0) and create smu1..smuN."""
    from pymeasure.instruments.agilent import AgilentB1500
    b1500 = AgilentB1500(resource_str, read_termination='\r\n',
                         write_termination='\r\n', timeout=timeout_ms)

    def patched_query_modules():
        modules = {}
        response = b1500.ask("UNT? 0")
        for i, param in enumerate(response.split(';')):
            if ',' in param:
                model, _status = param.split(',')
                if model in MODULE_NAMES:
                    modules[i + 1] = MODULE_NAMES[model]
        return modules

    b1500.query_modules = patched_query_modules
    b1500.initialize_all_smus()
    log(f"B1500 modules: {b1500.query_modules()}")
    return b1500


def get_smus(b1500, mapping):
    """mapping like {'gate': 'smu3', 'drain': 'smu1', 'source': 'smu2'}."""
    return tuple(getattr(b1500, mapping[t]) for t in ('gate', 'drain', 'source'))


def zero_outputs(b1500):
    """Force 0 V on all channels immediately (DZ)."""
    b1500.force_gnd()


def close(b1500):
    b1500.adapter.close()
