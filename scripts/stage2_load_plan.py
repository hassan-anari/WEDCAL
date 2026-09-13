#!/usr/bin/env python3
"""Emit deterministic Stage-2 load scenario manifests; this does not generate bulk data."""

import argparse
import json
import random

PROFILES = (50_000, 100_000, 250_000, 500_000, 1_000_000)
SCENARIOS = (
    "api_reads",
    "bootstrap",
    "base_tap",
    "telegram_ingress",
    "activity_ingestion",
    "outbox_drain",
    "creator_settlement",
    "referral_reconciliation",
    "ranking_rebuild",
    "weekly_dlx_settlement",
    "season_close",
)

parser = argparse.ArgumentParser()
parser.add_argument("--users", type=int, choices=PROFILES, required=True)
parser.add_argument("--seed", type=int, default=11)
parser.add_argument("--scenario", choices=SCENARIOS, required=True)
args = parser.parse_args()
random.seed(args.seed)
print(
    json.dumps(
        {
            "users": args.users,
            "seed": args.seed,
            "scenario": args.scenario,
            "sample_correlation": random.getrandbits(64),
        },
        indent=2,
    )
)
