"""Launch only the disposable, opt-in PostgreSQL Phase 4 normalized-receipt benchmark."""

import argparse
import os
import subprocess
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scales", default="1000,10000,50000,100000")
    parser.add_argument(
        "--profile", choices=["core_sparse", "mixed", "concentrated"], default="core_sparse"
    )
    parser.add_argument("--concurrent-gameplay", action="store_true")
    args = parser.parse_args()
    sizes = [int(value) for value in args.scales.split(",")]
    if not sizes or min(sizes) < 1:
        parser.error("Positive registered-player populations required")
    if os.getenv("POSTGRES_HOST") not in {"127.0.0.1", "localhost"} or not os.getenv(
        "POSTGRES_DB", ""
    ).startswith("phase4_"):
        parser.error("Explicit local disposable POSTGRES_HOST and phase4_* database required")
    env = dict(
        os.environ,
        PHASE4_BENCHMARK="1",
        PHASE4_SCALES=",".join(map(str, sizes)),
        PHASE4_ACTIVITY_PROFILE=args.profile,
        PHASE4_CONCURRENT_GAMEPLAY="1" if args.concurrent_gameplay else "0",
    )
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "apps/arena/tests/phase3/test_phase4_scale.py", "-q"],
        env=env,
        check=False,
    )
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
