"""make smoke: against the running app, a real result, not just a status code. Run inside the app container."""

import json
import sys
import urllib.request

BASE = "http://127.0.0.1:8300"


def call(path: str, body: dict | None = None) -> dict:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 (fixed local URL)
        return json.loads(r.read())


def main() -> int:
    assert call("/api/health")["ok"]
    cfg = call("/api/configs/lc500_idvg.json")
    cfg["repeat_measurement"] = True  # plan every device, whatever earlier dry-runs left behind
    p = call("/api/preview", {"cfg": cfg, "options": {"limit": 3}})
    # by hand: 144 devices on the lc500 map; A:2, B:2, C:2 are 200 um apart, 400 um of travel from A:2
    assert len(p["sites"]) == 144, len(p["sites"])
    assert [d["key"] for d in p["plan"]] == ["A:2", "B:2", "C:2"], p["plan"]
    assert abs(p["total_travel_um"] - 400.0) < 1e-9, p["total_travel_um"]
    with urllib.request.urlopen(BASE + "/vendor/plotly.min.js", timeout=10) as r:  # noqa: S310
        assert len(r.read()) > 1_000_000
    print("smoke OK: 144 sites, plan A:2 B:2 C:2, 400 um, Plotly served locally")
    return 0


if __name__ == "__main__":
    sys.exit(main())
