"""Cascade S200 (Velox) chuck control.

Adapted from S200_B1500A_4200A_py_setup/S200_utils/{initialize_s200,chuck_control}.py.
Nothing here touches hardware at import time; connect() must be called explicitly.

Sign convention (kept from the original repo, confirmed in CHH_run.log): the
user-facing (x, y) is the offset of the target device relative to the current
one, and the chuck is commanded by the negated amount.
"""
import math
import time

RESOURCE_STR = 'GPIB0::22::INSTR'


class ProberError(RuntimeError):
    pass


def connect(resource_str=RESOURCE_STR, timeout_ms=20000):
    """Open the real Velox GPIB session. Sends *IDN?."""
    import pyvisa
    rm = pyvisa.ResourceManager()
    res = rm.open_resource(resource_str)
    res.timeout = timeout_ms
    print(f"Prober connected: {res.query('*IDN?').strip()}")
    return rm, res


def parse_reply(reply):
    """Velox replies look like '0: X Y Z' (first field = error code)."""
    reply = reply.strip()
    code, _, rest = reply.partition(':')
    try:
        code = int(code)
    except ValueError:
        raise ProberError(f"Unparseable prober reply: {reply!r}")
    if code != 0:
        raise ProberError(f"Prober error {code}: {rest.strip()}")
    return rest.split()


class VeloxProber:
    """Thin wrapper around a pyvisa-like resource (anything with write/query)."""

    def __init__(self, resource, wait_mode='sleep', xy_wait_s=20, z_wait_s=10,
                 velocity_pct=10, pos_tolerance_um=5.0, pos_tolerance_rel=0.03, max_drift_um=20.0,
                 drift_correct_um=None, drift_limit_um=None, max_fix_moves=3, log=print):
        """Per move, the readback must match the command within
        max(pos_tolerance_um, pos_tolerance_rel * |move|). Across the run, the
        accumulated readback deviation from the intended path must stay below
        max_drift_um (None disables the drift check). With drift_correct_um set, any
        axis whose accumulated drift has reached +/-drift_correct_um is cancelled in
        the next move (None disables the correction). With drift_limit_um set, a landing with any axis
        beyond +/-drift_limit_um gets extra correction moves right away (at most max_fix_moves; still
        beyond after them raises), so every landing stays within the limit."""
        if wait_mode not in ('sleep', 'query'):
            raise ValueError("wait_mode must be 'sleep' or 'query'")
        self.res = resource
        self.wait_mode = wait_mode
        self.xy_wait_s = xy_wait_s
        self.z_wait_s = z_wait_s
        self.velocity = float(velocity_pct)
        self.tol = pos_tolerance_um
        self.tol_rel = pos_tolerance_rel
        self.max_drift = max_drift_um
        self.drift_correct = drift_correct_um
        self.drift_limit = drift_limit_um
        self.max_fix_moves = max_fix_moves
        self.log = log
        self.ref = None              # chuck (x, y) readback at the first move
        self.cmd_total = [0.0, 0.0]  # summed intended chuck displacement since ref (corrections excluded)
        self.n_corrections = 0
        self.n_fix_moves = 0

    def _cmd(self, cmd, wait_s):
        if self.wait_mode == 'query':
            parse_reply(self.res.query(cmd))
        else:
            self.res.write(cmd)
            time.sleep(wait_s)

    def read_position(self):
        x, y, z = (float(v) for v in parse_reply(self.res.query('ReadChuckPosition'))[:3])
        return x, y, z

    def separate(self):
        self.log("Chuck -> separation height")
        self._cmd(f'MoveChuckSeparation {self.velocity:g}.', self.z_wait_s)

    def contact(self):
        self.log("Chuck -> contact height")
        self._cmd(f'MoveChuckContact {self.velocity:g}.', self.z_wait_s)

    def drift(self, x, y):
        """Readback (x, y) minus the intended chuck path, um (chuck coordinates)."""
        return (x - self.ref[0]) - self.cmd_total[0], (y - self.ref[1]) - self.cmd_total[1]

    def move_relative(self, dx_um, dy_um):
        """Move so the probes land on a device offset by (dx, dy) um. Verifies the move."""
        x0, y0, _ = self.read_position()
        if self.ref is None:
            self.ref = (x0, y0)
        corr = [0.0, 0.0]            # chuck-coordinate correction added to this move
        if self.drift_correct is not None:
            corr = [-d if abs(d) >= self.drift_correct else 0.0 for d in self.drift(x0, y0)]
        mx, my = -dx_um + corr[0], -dy_um + corr[1]   # chuck command
        self.log(f"Move dx={dx_um:.3f} dy={dy_um:.3f} um (chuck start {x0:.3f}, {y0:.3f})")
        if any(corr):
            self.n_corrections += 1
            self.log(f"Drift correction: chuck command adjusted by ({corr[0]:+.3f}, {corr[1]:+.3f}) um "
                     f"(accumulated drift reached +/-{self.drift_correct:g} um)")
        self._cmd(f'MoveChuck {mx:.3f} {my:.3f} R Y {self.velocity:g}.', self.xy_wait_s)
        self.cmd_total[0] -= dx_um
        self.cmd_total[1] -= dy_um
        x1, y1, _ = self.read_position()
        ex, ey = (x1 - x0) - mx, (y1 - y0) - my
        tol = max(self.tol, self.tol_rel * math.hypot(mx, my))
        drift = self.drift(x1, y1)
        self.log(f"Chuck now at {x1:.3f}, {y1:.3f} | move error ({ex:+.3f}, {ey:+.3f}) um | "
                 f"accumulated drift ({drift[0]:+.3f}, {drift[1]:+.3f}) um")
        if math.hypot(ex, ey) > tol:
            raise ProberError(f"Chuck position mismatch after move: error ({ex:.3f}, {ey:.3f}) um "
                              f"> tolerance {tol:.2f} um (now {x1:.3f}, {y1:.3f})")
        if self.max_drift is not None and math.hypot(*drift) > self.max_drift:
            raise ProberError(f"Accumulated chuck drift ({drift[0]:.3f}, {drift[1]:.3f}) um exceeds "
                              f"max_drift_um={self.max_drift}")
        if self.drift_limit is None:
            return
        for _ in range(self.max_fix_moves):
            if max(abs(drift[0]), abs(drift[1])) <= self.drift_limit:
                return
            fx, fy = -drift[0], -drift[1]
            self.n_fix_moves += 1
            self.log(f"Drift beyond +/-{self.drift_limit:g} um: correction move ({fx:+.3f}, {fy:+.3f}) um")
            self._cmd(f'MoveChuck {fx:.3f} {fy:.3f} R Y {self.velocity:g}.', self.xy_wait_s)
            x2, y2, _ = self.read_position()
            drift = self.drift(x2, y2)
            self.log(f"Chuck now at {x2:.3f}, {y2:.3f} | accumulated drift ({drift[0]:+.3f}, {drift[1]:+.3f}) um")
        if max(abs(drift[0]), abs(drift[1])) > self.drift_limit:
            raise ProberError(f"Accumulated chuck drift ({drift[0]:.3f}, {drift[1]:.3f}) um still beyond "
                              f"+/-{self.drift_limit:g} um after {self.max_fix_moves} correction moves")
