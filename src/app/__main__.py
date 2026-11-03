"""Native launcher, for the lab PC only (make up-live): the one way to allow live runs, on 127.0.0.1.

    PYTHONPATH=src python -m app --allow-live [--port 8300]

The container never uses this; it runs `uvicorn app.api:app`, which is dry-run only.
"""

import argparse
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8300)
    ap.add_argument("--allow-live", action="store_true", help="allow live runs on the real instruments")
    a = ap.parse_args(argv)
    if a.host not in LOCAL_HOSTS:
        ap.error("the app is published on this machine only: use --host 127.0.0.1")
    os.chdir(REPO)  # configs and layouts use paths relative to the repository root
    import uvicorn

    from app.api import create_app

    print(f"{'LIVE RUNS ALLOWED' if a.allow_live else 'Dry-run only'}: http://{a.host}:{a.port}/", flush=True)
    uvicorn.run(create_app(allow_live=a.allow_live), host=a.host, port=a.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
