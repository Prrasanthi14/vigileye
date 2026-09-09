from functools import lru_cache

from ..config import settings
from .base import DataConnector


@lru_cache(maxsize=1)
def get_connector() -> DataConnector:
    if settings.data_source == "bigquery":
        from .bigquery import BigQueryConnector

        return BigQueryConnector(settings.gcp_project_id, settings.bigquery_dataset)

    from .sqlite import SQLiteConnector

    return SQLiteConnector()


__all__ = ["DataConnector", "get_connector"]
