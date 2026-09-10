"""Token accounting for Gemini calls.

Every model call appends its exact token counts to an append-only ledger, and
every verdict served from the store appends a zero-cost row. That turns "tokens
saved by storing verdicts" into a computed figure rather than an estimate, and
lets a daily budget stop spending before a runaway loop can.

The ledger is separate from the verdict store on purpose: a re-evaluated verdict
overwrites its row there, which would erase the record of what the earlier call
cost.
"""

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any

from ..config import settings

logger = logging.getLogger(__name__)

TABLE = "token_usage"
SCHEMA = [
    ("driver_id", "STRING"), ("called_at", "TIMESTAMP"), ("kind", "STRING"),
    ("model", "STRING"), ("prompt_tokens", "INTEGER"), ("thinking_tokens", "INTEGER"),
    ("output_tokens", "INTEGER"), ("total_tokens", "INTEGER"),
]
MODEL_CALL, STORE_HIT = "model_call", "store_hit"

# Reading today's spend from BigQuery before every call would add a round trip to
# each verdict, so the figure is cached briefly and topped up locally with
# whatever this instance has spent since.
_SPEND_CACHE_SECONDS = 30
_lock = threading.Lock()
_fetched_at: float | None = None
_spent_at_fetch = 0
_spent_since_fetch = 0
_table_ready = False


def _client():
    from google.cloud import bigquery

    return bigquery.Client(project=settings.gcp_project_id)


def _table_id() -> str:
    return f"{settings.gcp_project_id}.{settings.bigquery_dataset}.{TABLE}"


def ensure_table() -> None:
    global _table_ready
    if _table_ready:
        return
    from google.cloud import bigquery

    table = bigquery.Table(_table_id(), schema=[bigquery.SchemaField(n, t) for n, t in SCHEMA])
    _client().create_table(table, exists_ok=True)
    _table_ready = True


def _append(row: dict[str, Any]) -> None:
    # Written inline rather than on a background thread: Cloud Run throttles CPU
    # once a response is sent, so deferred writes can stall or be lost. A failed
    # write is logged and never blocks the verdict it describes.
    try:
        ensure_table()
        errors = _client().insert_rows_json(_table_id(), [row])
        if errors:
            logger.warning("Token ledger insert rejected: %s", errors)
    except Exception as exc:
        logger.warning("Token ledger write failed: %s", exc)


def _row(driver_id: str | None, kind: str, model: str | None,
         prompt: int = 0, thinking: int = 0, output: int = 0, total: int = 0) -> dict:
    return {
        "driver_id": driver_id, "called_at": datetime.now(timezone.utc).isoformat(),
        "kind": kind, "model": model, "prompt_tokens": prompt,
        "thinking_tokens": thinking, "output_tokens": output, "total_tokens": total,
    }


def record_call(driver_id: str | None, usage_metadata: Any, model: str | None) -> int:
    """Record the tokens one Gemini call consumed. Returns the total."""
    global _spent_since_fetch
    prompt = getattr(usage_metadata, "prompt_token_count", 0) or 0
    thinking = getattr(usage_metadata, "thoughts_token_count", 0) or 0
    output = getattr(usage_metadata, "candidates_token_count", 0) or 0
    total = getattr(usage_metadata, "total_token_count", 0) or prompt + thinking + output

    with _lock:
        _spent_since_fetch += total
    _append(_row(driver_id, MODEL_CALL, model, prompt, thinking, output, total))
    return total


def record_store_hit(driver_id: str | None) -> None:
    """Record a verdict served from the store — a model call that was not made."""
    _append(_row(driver_id, STORE_HIT, None))


def tokens_used_today() -> int:
    """Gemini tokens spent so far in the current UTC day, across all instances."""
    global _fetched_at, _spent_at_fetch, _spent_since_fetch
    with _lock:
        if _fetched_at is not None and time.monotonic() - _fetched_at < _SPEND_CACHE_SECONDS:
            return _spent_at_fetch + _spent_since_fetch

    try:
        ensure_table()
        rows = _client().query(
            f"SELECT COALESCE(SUM(total_tokens), 0) AS spent FROM `{_table_id()}` "
            "WHERE DATE(called_at) = CURRENT_DATE()"
        ).result()
        spent = int(next(iter(rows)).spent)
    except Exception as exc:
        logger.warning("Could not read today's token spend: %s", exc)
        with _lock:
            return _spent_at_fetch + _spent_since_fetch

    with _lock:
        _fetched_at, _spent_at_fetch, _spent_since_fetch = time.monotonic(), spent, 0
        return spent


def budget_exhausted() -> tuple[bool, int]:
    """(whether today's budget is spent, tokens used). A budget of 0 means no cap."""
    budget = settings.daily_token_budget
    if budget <= 0:
        return False, 0
    used = tokens_used_today()
    return used >= budget, used


def summarize(aggregate: dict[str, dict[str, int]], budget: int) -> dict[str, Any]:
    """Turn per-kind totals into the usage report, including tokens saved.

    Saving is computed, not assumed: each verdict served from the store would
    otherwise have been a model call, priced at today's measured average.
    """
    calls = aggregate.get(MODEL_CALL, {})
    hits = aggregate.get(STORE_HIT, {})
    n_calls, n_hits = calls.get("n", 0), hits.get("n", 0)
    used = calls.get("total", 0)
    average = round(used / n_calls) if n_calls else 0
    saved = n_hits * average

    return {
        "day_utc": datetime.now(timezone.utc).date().isoformat(),
        "model_calls": n_calls,
        "verdicts_served_from_store": n_hits,
        "tokens_used": used,
        "tokens_by_part": {
            "prompt": calls.get("prompt", 0),
            "thinking": calls.get("thinking", 0),
            "output": calls.get("output", 0),
        },
        "avg_tokens_per_call": average,
        "tokens_saved_by_store": saved,
        "saving_pct": round(100 * saved / (saved + used), 1) if saved + used else 0.0,
        "daily_budget": budget if budget > 0 else None,
        "budget_remaining": max(budget - used, 0) if budget > 0 else None,
    }


def summary() -> dict[str, Any]:
    """Today's token report, read from the ledger."""
    aggregate: dict[str, dict[str, int]] = {}
    try:
        ensure_table()
        for r in _client().query(
            f"""
            SELECT kind, COUNT(*) AS n,
                   COALESCE(SUM(prompt_tokens), 0) AS prompt,
                   COALESCE(SUM(thinking_tokens), 0) AS thinking,
                   COALESCE(SUM(output_tokens), 0) AS output,
                   COALESCE(SUM(total_tokens), 0) AS total
            FROM `{_table_id()}`
            WHERE DATE(called_at) = CURRENT_DATE()
            GROUP BY kind
            """
        ).result():
            aggregate[r.kind] = {"n": r.n, "prompt": r.prompt, "thinking": r.thinking,
                                 "output": r.output, "total": r.total}
    except Exception as exc:
        logger.warning("Could not read token ledger: %s", exc)
    return summarize(aggregate, settings.daily_token_budget)
