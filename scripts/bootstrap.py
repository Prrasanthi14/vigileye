"""Create a working VigilEye database from nothing.

    python scripts/bootstrap.py                      # local SQLite, no GCP needed
    python scripts/bootstrap.py --source bigquery    # into BigQuery

Creates the tables, invents a roster of pilots with distinct names, and fills
in synthetic biometrics. Run this first on a fresh clone — every other script
assumes a roster already exists.
"""

import argparse
import random
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vigileye.config import settings  # noqa: E402
from vigileye.ingestion.synthetic import PilotProfile, SyntheticProvider  # noqa: E402

FIRST_NAMES = [
    "Aditi", "Bjorn", "Carmen", "Dmitri", "Elena", "Farid", "Greta", "Hassan",
    "Ingrid", "Javier", "Keiko", "Lucas", "Mira", "Nikolai", "Olga", "Pedro",
    "Qiang", "Rosa", "Sven", "Tara", "Umberto", "Valentina", "Wei", "Xiomara",
    "Yusuf", "Zara", "Anton", "Beatriz", "Cyril", "Daniela", "Emeka", "Fiona",
    "Gustav", "Hana", "Ivan", "Jolanta", "Kwame", "Lena", "Marco", "Nadia",
    "Oscar", "Petra", "Rafael", "Sofia", "Tomas", "Ursula", "Viktor", "Wanda",
    "Yara", "Zoltan",
]

LAST_NAMES = [
    "Almeida", "Bergstrom", "Chaudhary", "Dimitrov", "Eriksen", "Fontaine",
    "Grigoryan", "Haddad", "Ibrahim", "Jankowski", "Kowalczyk", "Lindqvist",
    "Mbeki", "Nakamura", "Okonkwo", "Petrov", "Quintero", "Rasmussen",
    "Silva", "Takahashi", "Ulrich", "Vasquez", "Wojcik", "Yilmaz", "Zielinski",
    "Andersson", "Baptiste", "Castellano", "Dubois", "Espinoza", "Ferreira",
    "Gallagher", "Hoffmann", "Iversen", "Jimenez", "Kovacs", "Laurent",
    "Moreau", "Nilsson", "Ortega", "Pavlenko", "Reyes", "Sorensen", "Thibault",
    "Varga", "Whitfield", "Xu", "Yoshida", "Zhukov", "Beaumont",
]

ROLES = ["Commercial Pilot", "Regional First Officer", "CDL Long-Haul Driver"]
SHIFTS = ["Day", "Night", "Rotating"]
CONDITIONS = ["None", "None", "None", "None", "Mild Sleep Apnea",
              "History of Insomnia", "Controlled Hypertension"]

PILOTS_COLUMNS = "driver_id, name, role, shift_type, base_timezone, medical_history"
READING_COLUMNS = [
    "driver_id", "date", "report_time", "total_sleep_hours", "deep_sleep_pct",
    "rem_sleep_pct", "light_sleep_pct", "awake_during_sleep_pct", "hrv_ms",
    "resting_hr", "time_awake_since_last_sleep", "consecutive_duty_days",
]


def build_roster(count: int, seed: int) -> list[dict]:
    rng = random.Random(seed)
    pool = [f"{f} {l}" for f in FIRST_NAMES for l in LAST_NAMES]
    rng.shuffle(pool)
    if count > len(pool):
        raise SystemExit(f"Can only generate {len(pool)} distinct names")

    return [
        {
            "driver_id": f"PAT-{i + 1:03d}",
            "name": pool[i],
            "role": rng.choice(ROLES),
            "shift_type": rng.choice(SHIFTS),
            "base_timezone": rng.choice(["UTC", "Asia/Kolkata", "Europe/London",
                                         "America/New_York", "Asia/Dubai"]),
            "medical_history": rng.choice(CONDITIONS),
        }
        for i in range(count)
    ]


def build_readings(roster: list[dict], days: int, seed: int) -> list[dict]:
    rng = random.Random(seed)
    profiles = [
        PilotProfile(
            driver_id=p["driver_id"], shift_type=p["shift_type"],
            medical_history=p["medical_history"],
            baseline_hrv=rng.uniform(38, 82), baseline_rhr=rng.randint(48, 68),
            sleep_need=rng.uniform(7.0, 8.4),
        )
        for p in roster
    ]
    end = date.today()
    readings = SyntheticProvider(profiles, seed=seed).fetch_readings(
        [p.driver_id for p in profiles], end - timedelta(days=days - 1), end
    )
    return [r.model_dump() for r in readings]


def write_sqlite(roster: list[dict], readings: list[dict], db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        DROP TABLE IF EXISTS pilots;
        DROP TABLE IF EXISTS daily_readings;
        CREATE TABLE pilots (
            driver_id TEXT PRIMARY KEY, name TEXT, role TEXT,
            shift_type TEXT, base_timezone TEXT, medical_history TEXT
        );
        CREATE TABLE daily_readings (
            driver_id TEXT, date TEXT, report_time TEXT, total_sleep_hours REAL,
            deep_sleep_pct REAL, rem_sleep_pct REAL, light_sleep_pct REAL,
            awake_during_sleep_pct REAL, hrv_ms REAL, resting_hr INTEGER,
            time_awake_since_last_sleep REAL, consecutive_duty_days INTEGER
        );
    """)
    conn.executemany(
        f"INSERT INTO pilots ({PILOTS_COLUMNS}) VALUES (?,?,?,?,?,?)",
        [tuple(p[c.strip()] for c in PILOTS_COLUMNS.split(",")) for p in roster],
    )
    conn.executemany(
        f"INSERT INTO daily_readings ({', '.join(READING_COLUMNS)}) "
        f"VALUES ({', '.join('?' * len(READING_COLUMNS))})",
        [tuple(str(r[c]) if c == "date" else r[c] for c in READING_COLUMNS)
         for r in readings],
    )
    conn.commit()
    conn.close()


def write_bigquery(roster: list[dict], readings: list[dict]) -> None:
    from google.cloud import bigquery

    client = bigquery.Client(project=settings.gcp_project_id)
    dataset_id = f"{settings.gcp_project_id}.{settings.bigquery_dataset}"
    client.create_dataset(bigquery.Dataset(dataset_id), exists_ok=True)

    pilots_schema = [bigquery.SchemaField(c.strip(), "STRING")
                     for c in PILOTS_COLUMNS.split(",")]
    readings_schema = [
        bigquery.SchemaField("driver_id", "STRING"), bigquery.SchemaField("date", "DATE"),
        bigquery.SchemaField("report_time", "STRING"),
        *[bigquery.SchemaField(c, "FLOAT") for c in
          ("total_sleep_hours", "deep_sleep_pct", "rem_sleep_pct", "light_sleep_pct",
           "awake_during_sleep_pct", "hrv_ms", "time_awake_since_last_sleep")],
        bigquery.SchemaField("resting_hr", "INTEGER"),
        bigquery.SchemaField("consecutive_duty_days", "INTEGER"),
    ]

    for table, rows, schema in (
        ("pilots", roster, pilots_schema),
        ("daily_readings",
         [{**r, "date": r["date"].isoformat()} for r in readings], readings_schema),
    ):
        client.load_table_from_json(
            rows, f"{dataset_id}.{table}",
            job_config=bigquery.LoadJobConfig(
                schema=schema, write_disposition="WRITE_TRUNCATE"),
        ).result()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", choices=["sqlite", "bigquery"],
                        default=settings.data_source)
    parser.add_argument("--pilots", type=int, default=100)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--db-path", default="vigileye_fleet.db")
    args = parser.parse_args()

    roster = build_roster(args.pilots, args.seed)
    readings = build_readings(roster, args.days, args.seed)

    if args.source == "sqlite":
        write_sqlite(roster, readings, args.db_path)
        where = args.db_path
    else:
        write_bigquery(roster, readings)
        where = f"{settings.gcp_project_id}.{settings.bigquery_dataset}"

    print(f"Created {len(roster)} pilots and {len(readings)} readings in {where}.")
    print("Next: start the API, then the dashboard (see README).")


if __name__ == "__main__":
    main()
