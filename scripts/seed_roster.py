"""Give every pilot a distinct name, preserving their id and profile.

    python scripts/seed_roster.py

Names in the original roster collided (two "Linda Davis", twelve "Johnson"),
which reads as duplicated data even though the pilots are distinct. Ids, roles,
shift types and medical history are left untouched, so readings and verdicts
stay attached to the right pilot.
"""

import random
import sys

sys.path.insert(0, "src")

from google.cloud import bigquery  # noqa: E402

from vigileye.config import settings  # noqa: E402

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


def unique_names(count: int, seed: int = 11) -> list[str]:
    rng = random.Random(seed)
    pool = [f"{first} {last}" for first in FIRST_NAMES for last in LAST_NAMES]
    rng.shuffle(pool)
    if count > len(pool):
        raise ValueError(f"Need {count} names but only {len(pool)} combinations exist")
    return pool[:count]


def main() -> None:
    client = bigquery.Client(project=settings.gcp_project_id)
    table = f"{settings.gcp_project_id}.{settings.bigquery_dataset}.pilots"

    ids = [row.driver_id for row in client.query(
        f"SELECT driver_id FROM `{table}` ORDER BY driver_id"
    ).result()]
    if not ids:
        raise SystemExit("No pilots found; nothing to rename.")

    names = unique_names(len(ids))
    cases = "\n".join(
        f"WHEN '{driver_id}' THEN @name_{index}" for index, driver_id in enumerate(ids)
    )
    params = [
        bigquery.ScalarQueryParameter(f"name_{index}", "STRING", name)
        for index, name in enumerate(names)
    ]

    client.query(
        f"UPDATE `{table}` SET name = CASE driver_id\n{cases}\nELSE name END WHERE TRUE",
        job_config=bigquery.QueryJobConfig(query_parameters=params),
    ).result()

    duplicates = list(client.query(
        f"SELECT name, COUNT(*) n FROM `{table}` GROUP BY name HAVING n > 1"
    ).result())
    print(f"Renamed {len(ids)} pilots; duplicate names remaining: {len(duplicates)}")


if __name__ == "__main__":
    main()
