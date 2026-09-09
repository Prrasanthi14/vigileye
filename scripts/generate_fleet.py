"""Regenerate synthetic fleet biometrics and load them into BigQuery.

    python scripts/generate_fleet.py --days 30 --replace

Reads the existing pilot roster from BigQuery so profiles (shift type, medical
history) stay consistent with the pilots already on file.
"""

import argparse
import logging
import random
import sys
from datetime import date, timedelta

sys.path.insert(0, "src")

from google.cloud import bigquery  # noqa: E402

from vigileye.config import settings  # noqa: E402
from vigileye.ingestion.sync import sync_to_bigquery  # noqa: E402
from vigileye.ingestion.synthetic import PilotProfile, SyntheticProvider  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


def load_profiles(seed: int) -> list[PilotProfile]:
    client = bigquery.Client(project=settings.gcp_project_id)
    query = f"""
        SELECT driver_id, shift_type, medical_history
        FROM `{settings.gcp_project_id}.{settings.bigquery_dataset}.pilots`
        ORDER BY driver_id
    """
    rng = random.Random(seed)
    profiles = []
    for row in client.query(query).result():
        profiles.append(PilotProfile(
            driver_id=row.driver_id,
            shift_type=row.shift_type or "Day",
            medical_history=row.medical_history or "None",
            baseline_hrv=rng.uniform(38, 82),
            baseline_rhr=rng.randint(48, 68),
            sleep_need=rng.uniform(7.0, 8.4),
        ))
    return profiles


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--replace", action="store_true",
                        help="overwrite daily_readings instead of appending")
    args = parser.parse_args()

    profiles = load_profiles(args.seed)
    if not profiles:
        raise SystemExit("No pilots found in BigQuery; seed the pilots table first.")

    end = date.today()
    start = end - timedelta(days=args.days - 1)
    provider = SyntheticProvider(profiles, seed=args.seed)

    written = sync_to_bigquery(
        provider, [p.driver_id for p in profiles], start, end, replace=args.replace
    )
    print(f"Wrote {written} readings for {len(profiles)} pilots ({start} to {end}).")


if __name__ == "__main__":
    main()
