"""Runtime configuration, resolved from environment variables."""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    gcp_project_id: str
    bigquery_dataset: str
    data_source: str
    gemini_model: str
    gemini_api_key: str | None
    api_base_url: str
    verdict_max_age_minutes: int

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            gcp_project_id=os.getenv("GCP_PROJECT_ID", "gen-lang-client-0469618448"),
            bigquery_dataset=os.getenv("BIGQUERY_DATASET", "vigileye_fleet"),
            data_source=os.getenv("DATA_SOURCE", "bigquery"),
            # Pro, not Flash: a go/no-go call weighs conflicting signals against
            # medical history, and is worth seconds of latency to get right.
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-pro-latest"),
            gemini_api_key=os.getenv("GEMINI_API_KEY"),
            api_base_url=os.getenv("API_BASE_URL", "http://localhost:8000"),
            # Fatigue moves with the clock, so a verdict has a shelf life: past
            # this age it is re-run against whatever the tracker has since landed.
            verdict_max_age_minutes=int(os.getenv("VERDICT_MAX_AGE_MINUTES", "120")),
        )


settings = Settings.from_env()
