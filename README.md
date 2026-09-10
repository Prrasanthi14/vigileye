# VigilEye

**Agentic go/no-go readiness decisions for pilots, from wearable biometrics.**

A pilot flying several legs a day accumulates fatigue that a duty-hours table cannot see. VigilEye reads each pilot's wearable data — sleep duration and staging, heart-rate variability, resting heart rate, hours awake, consecutive duty days — and returns a **CLEAR / PENDING_TEST / GROUNDED** verdict with the reasoning behind it.

**Live dashboard:** https://vigileye-dashboard-969488392244.us-central1.run.app

> New to the terminology (PVT, HRV, WOCL)? There's a [glossary at the end](#glossary).

---

## System design

Every number below was measured on this deployment, not estimated.

### Scalability

**Stateless services, stateful storage.** The API holds no session state, so Cloud Run scales it horizontally and to zero. Verdicts and readings live in BigQuery; any instance can serve any request.

**The decisive choice is *what* gets computed, not how fast.** A verdict is keyed on `(driver_id, reading_date)` and stored, so cost is **one model call per pilot per day** rather than one per page view. A fleet grid viewed 500 times costs zero model calls.

**Where it stops scaling, stated honestly.** A whole-fleet sweep is O(pilots) in model calls:

| Fleet | Full sweep |
|---|---|
| 100 (this deployment) | **5 min** — measured |
| ~5,000 (IndiGo scale) | ~4.2 hours |
| ~15,000 (major US carrier) | ~12.5 hours |

Past a few hundred pilots the sweep is the wrong shape. The fix is in the design already: the roster stores each pilot's `report_time`, so evaluating a pilot ~2h before they report spreads identical work across 24 hours and touches only pilots actually flying. At 5,000 pilots with 40% on duty that's **1.4 calls/minute** — the same infrastructure, no sweep.

### Latency

Three paths, deliberately different costs:

| Path | Model calls | Measured |
|---|---|---|
| Fleet grid (100 pilots) | **0** | two BigQuery reads |
| Pilot page, verdict fresh | **0** | **2.3 s** |
| Pilot page, verdict absent/stale | 1 | **21.3 s** |

The 9× gap between a stored and a computed verdict is the entire argument for the cache. Supporting measures: batch work runs on 6 concurrent workers; the dashboard caches API responses for 60 s; and the health check allows 60 s because Cloud Run scales to zero and the first request after idle pays for a container boot plus BigQuery auth.

**Latency is also a correctness property here.** `time_awake_since_last_sleep` climbs with the clock, so a verdict has a shelf life regardless of whether the data changed. Past `VERDICT_MAX_AGE_MINUTES` (default 120) it is recomputed rather than served.

### Rate limiting

This one was learned the hard way. A first fleet run used 12 concurrent workers against Gemini Pro's **25 requests/minute** quota: **71 of 100 pilots failed** and silently degraded to the rules engine.

Two mechanisms now:

```python
# src/vigileye/scoring/agent.py
RPM_LIMIT = int(os.getenv("GEMINI_RPM", "20"))   # stay under the per-model quota

def _throttle() -> None:
    while True:
        with _lock:
            now = time.monotonic()
            while _recent_calls and now - _recent_calls[0] > 60:
                _recent_calls.popleft()
            if len(_recent_calls) < RPM_LIMIT:
                _recent_calls.append(now)
                return
            wait = 60 - (now - _recent_calls[0]) + 0.05
        time.sleep(wait)
```

A thread-safe sliding window paces calls below the quota, and `RESOURCE_EXHAUSTED` responses retry with backoff. Re-run after the fix: **71 evaluated, 0 failed.** `GEMINI_RPM` tracks whatever quota the account actually has.

### Fault tolerance and availability

The fourth pillar — the system degrades rather than fails, and *says so*.

- **Agent unavailable → deterministic rules engine.** No blank screen, no 500. A go/no-go tool that returns nothing when an API is down is worse than one that returns a conservative answer.
- **Degradation is never silent.** Every verdict carries `source: "agent" | "rules"` and a `fallback_reason`. The dashboard renders a different heading and a visible warning. This is what caught the 71-failure incident above.
- **Storage failures are contained.** A BigQuery client that fails to initialise leaves the connector returning empty results rather than crashing the service; verdict cache read/write failures are logged and swallowed, because losing the cache must not lose the verdict.
- **A degraded verdict is never cached.** Rules fallbacks are deliberately not stored, so the system retries the agent instead of freezing a downgraded answer.

### Consistency

Worth naming, because it was the original failure mode. This codebase once had **three different definitions of go/no-go** — a two-line heuristic in the dashboard, an eight-factor rules engine, and the agent — so a pilot could read CLEAR in the grid and GROUNDED on their own page.

Now: one scoring engine, one stored verdict per pilot-day, read by both views. Writes invalidate the affected verdict, and ingestion upserts per pilot-day so two rows can never exist for one pilot-day leaving "latest reading" non-deterministic.

---

## How a decision is made

```
Wearable tracker  ──►  BigQuery  ──►  Readiness API  ──►  Gemini agent  ──►  Verdict
 (Oura / synthetic)     (readings)      (FastAPI)          (Pro)            (+ stored)
                                             │                                  │
                                             └────────►  Rules engine  ◄────────┘
                                                        (deterministic fallback)
```

The **agent** makes the call. It receives the pilot's latest biometrics plus a seven-day trend and medical history, and reasons against aviation fatigue-science context: the 02:00–05:00 Window of Circadian Low, healthy sleep architecture thresholds, HRV as a recovery signal, the point past which wakefulness degrades performance comparably to alcohol, and how a condition like sleep apnea amplifies an already-poor night.

The **rules engine** is a deterministic weighted model over the same eight factors. It exists so the system degrades rather than fails when the agent is unavailable — and every verdict records which engine produced it, so a fallback can never be presented as an AI decision.

### The three verdicts

| Verdict | Score | Meaning |
|---|---|---|
| ✅ CLEAR | 70–100 | Fit for duty |
| 🟡 PENDING_TEST | 45–69 | Borderline — take a **PVT** (Psychomotor Vigilance Test); the result decides |
| 🔴 GROUNDED | 0–44 | Unfit. Mandatory rest before returning |

`PENDING_TEST` exists because fatigue is a spectrum. Rather than guess on a borderline pilot, the system escalates to an objective reaction-time test.

---

## Architecture

Two Cloud Run services, deliberately split:

| Service | Access | Role |
|---|---|---|
| `vigileye-api` | **Private** (IAM) | Data access, scoring, agent orchestration |
| `vigileye-dashboard` | Public | Streamlit client — no business logic |

The dashboard holds no decision logic; it calls the API with a Cloud Run ID token. The API is closed to anonymous callers because it serves biometrics and medical history.

```
src/vigileye/
├── config.py            env-driven settings
├── models.py            ReadinessEvaluation, Evaluation (verdict + provenance)
├── data/                storage behind one interface — bigquery | sqlite
├── ingestion/           the ONLY writer of biometrics
│   ├── base.py            WearableProvider + normalized DailyReading
│   ├── oura.py            Oura v2 adapter
│   ├── synthetic.py       physiologically-modelled generator
│   └── sync.py            provider → BigQuery, upsert per pilot-day
├── scoring/
│   ├── agent.py           Gemini, rate-limited to the per-model RPM quota
│   ├── rules.py           deterministic 8-factor fallback
│   ├── cache.py           stored verdicts, keyed (pilot, reading date)
│   └── service.py         agent → fallback, with provenance
└── api/main.py          FastAPI
ui/dashboard.py          Streamlit client
```

### Design decisions worth knowing

**Biometrics have exactly one writer.** There is deliberately *no* API endpoint that creates or edits a reading. Readings are the evidence behind a go/no-go call — an official able to `PUT` a pilot's HRV could clear a grounded pilot by editing the evidence rather than challenging the verdict. Data enters only through `ingestion/`, which runs as an operator job under GCP credentials. Roster fields (name, role, shift, medical history) *are* editable via the API, because they are administrative rather than measured.

**Verdicts are computed once per pilot-day, then stored.** A verdict is a fact about one day's readings, not a per-page-view computation. This keeps the fleet grid and the pilot page showing the same decision, and caps cost at one model call per pilot per day.

**Any write invalidates the affected verdict.** A roster edit, or a tracker re-syncing a corrected reading, drops the stored verdict so the next request re-runs the agent. A verdict about data that no longer exists is never served.

**Rules fallbacks are never cached.** A degraded verdict should be retried, not frozen.

**Pro, not Flash.** A go/no-go call weighs conflicting signals against medical and duty history. That reasoning quality matters more than the seconds of latency it costs, since the agent runs on a single pilot on demand. Override with `GEMINI_MODEL`.

---

## What's actually novel here

Most of this stack is conventional. Four pieces are not, and each exists because of a specific failure this project hit.

### 1. Verdicts carry their own provenance — `src/vigileye/models.py`

**The twist.** This system originally shipped with a Gemini agent that had *never once run in production*. A `json.dumps` failure on a BigQuery `DATE` threw inside a broad `except`, which silently fell back to the rules engine. Every "AI verdict" the dashboard ever displayed came from a hardcoded `if/else` — under a heading that read "AI Cumulative Fatigue Analysis."

The fix isn't better error handling. It's making the fallback *structurally impossible to hide*:

```python
class Evaluation(ReadinessEvaluation):
    """A readiness verdict plus provenance of which engine produced it.

    The dashboard labels AI verdicts differently from deterministic ones, so a
    silent fallback to the rules engine can never be presented as agent output.
    """

    source: Literal["agent", "rules"]
    fallback_reason: str | None = None
```

`source` travels with every verdict through the API and into the UI, which renders a different heading and a visible warning carrying the real reason. A degraded decision announces itself. In a safety system, a verdict you can't attribute is worse than no verdict.

This also caught a later bug on its own: a fleet-wide run silently degraded 71 of 100 pilots to rules after hitting a rate limit, and the provenance labels made it obvious immediately.

### 2. Biometrics have no write path — `src/vigileye/api/main.py`

The most important code here is code that **doesn't exist**. There is no endpoint to create or edit a reading, and that absence is documented in place so nobody helpfully adds one:

```python
# NOTE: there is deliberately no endpoint to write or edit biometric readings.
# Readings are evidence for a go/no-go call, so a human-facing write path would
# let an official clear a grounded pilot by editing the evidence. Readings enter
# only through the ingestion pipeline (vigileye.ingestion), which runs as a job
# under GCP credentials and records the source of every row.
```

Roster fields *are* editable, because they're administrative. Readings are evidence. A test class asserts the boundary holds across every HTTP verb.

### 3. A verdict is a fact about data, not a computation — `src/vigileye/scoring/cache.py`

Verdicts are keyed on `(driver_id, reading_date)` and stored. Consequences that fall out of that framing:

- The fleet grid and the pilot page cannot disagree — same stored row.
- Cost is one model call per pilot per day, not per page view.
- **Any write to the underlying data invalidates the verdict.** A roster edit or a corrected tracker reading drops it, so the next request re-runs the agent. A verdict about data that no longer exists is never served.
- **Rules fallbacks are deliberately not cached** — a degraded verdict should be retried, not frozen.

### 4. Scoring is a data table, not a branch tree — `src/vigileye/scoring/rules.py`

The original rules engine was 174 lines of repeated `if/elif` — and its weights summed to **1.05**, so a well-rested pilot scored 105 and crashed Pydantic validation. Worse, every score was silently inflated ~5%, shifting pilots across the CLEAR/PENDING thresholds.

It's now a declarative table where thresholds are data and the comparison operator is part of the band:

```python
FACTORS = (
    Factor("total_sleep_hours", 0.25, 0, (
        (gt, 7, 100, None),
        (ge, 6, 80, None),
        (ge, 5, 55, "Suboptimal sleep duration (5-6h)"),
        (ge, 4, 20, "Insufficient sleep duration (4-5h)"),
    ), (0, "Severe sleep deprivation (<4h)")),
    ...
)

# Weights total 1.05, not 1.0 — normalize so the score stays within 0-100.
return int(round(weighted / TOTAL_WEIGHT)), risks, note
```

The refactor was verified behaviour-preserving by property-testing old against new across 4,000 randomised pilots — 0 mismatches.

---

## Extending it

Each of these is a single file, and the interface around it is the point.

### Add a wearable vendor (Fitbit, Garmin, Whoop)

**`src/vigileye/ingestion/base.py`** defines the contract. Implement it in a new file beside `oura.py`:

```python
class WearableProvider(ABC):
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def fetch_readings(self, driver_ids: list[str], start: date, end: date) -> list[DailyReading]:
        """Readings for the given pilots over an inclusive date range."""
```

Map the vendor's response into `DailyReading` and nothing downstream changes — scoring, storage, caching and the API are all vendor-agnostic. This interface exists because Google Fit, the original plan, turned out to be closed to new signups and shutting down; the abstraction is insurance against that repeating.

### Retune the fatigue model

**`src/vigileye/scoring/rules.py`** — edit the `FACTORS` table. Weights need not sum to 1.0; `TOTAL_WEIGHT` normalizes whatever you choose. To move the verdict boundaries, edit `status_for()`:

```python
def status_for(score: int) -> tuple[str, str]:
    if score >= 70:
        return "CLEAR", "Proceed with scheduled duties."
    if score >= 45:
        return "PENDING_TEST", "Administer PVT ... before duty."
    return "GROUNDED", "Remove from duty. Minimum 8 hours continuous rest required."
```

### Change what the agent knows

**`src/vigileye/scoring/agent.py`** — `SYSTEM_PROMPT` holds the domain knowledge (WOCL, sleep-architecture thresholds, HRV interpretation, how medical history amplifies risk). Add a regulation or a condition here rather than in the scoring code.

### Swap the model

**`src/vigileye/config.py`** — no code change needed:

```bash
gcloud run services update vigileye-api --region us-central1 \
  --update-env-vars GEMINI_MODEL=gemini-3.1-pro-preview
```

If you move to a model with a different quota, set `GEMINI_RPM` to match — `agent.py` paces calls against it.

### Add a storage backend

**`src/vigileye/data/base.py`** — implement `DataConnector` (see `bigquery.py` and `sqlite.py`), then register it in `data/__init__.py::get_connector()`. `DATA_SOURCE` selects it.

### Replace the simulated PVT with a real one

**`src/vigileye/scoring/service.py`** — `simulate_pvt_test()` currently generates plausible reaction times from the readiness score. Swap the body for a call to a real test harness; the API contract and the UI trigger stay as they are.

### Add role-based access

Not yet built, and the clearest next gap. `api/main.py` would gate `/fleet` and `/pilots/{id}` on the caller's identity so a pilot sees only their own record while a fleet manager sees all. The IAP header (`X-Goog-Authenticated-User-Email`) is the natural source once the project sits under a Cloud Organization.

---

## Running it

### Prerequisites
Python 3.11+, a GCP project with BigQuery, and a Gemini API key from [AI Studio](https://aistudio.google.com/apikey).

```bash
cp .env.example .env      # fill in GCP_PROJECT_ID and GEMINI_API_KEY
pip install -e ".[api,ui,dev]"
```

### Locally

```bash
# terminal 1 — the API
uvicorn vigileye.api.main:app --port 8000

# terminal 2 — the dashboard
API_BASE_URL=http://localhost:8000 streamlit run ui/dashboard.py
```

### Tests

```bash
pytest                    # 56 tests, no GCP credentials or API calls needed
```

The suite runs against SQLite with the agent and verdict store stubbed, so it is hermetic.

### Seeding data

```bash
python scripts/seed_roster.py                       # 100 pilots, unique names
python scripts/generate_fleet.py --days 30 --replace  # 30 days of biometrics
```

The synthetic generator models real physiology rather than independent random values: day-to-day persistence (AR(1)), accumulating sleep debt, per-pilot baselines, night-shift degradation, sleep apnea suppressing deep sleep, and ~7% daily acute disruptions. It reproduces a sleep↔HRV correlation of ~0.47, within the range observed in real wearable data.

### Deploying

```bash
gcloud run deploy vigileye-api --source . --region us-central1 --timeout 900
gcloud builds submit --config cloudbuild.ui.yaml --substitutions _IMAGE=gcr.io/PROJECT/vigileye-ui:vN
gcloud run deploy vigileye-dashboard --region us-central1 --image gcr.io/PROJECT/vigileye-ui:vN
```

---

## API

Private — every request needs a Google identity token.

```bash
TOKEN=$(gcloud auth print-identity-token)
API=https://vigileye-api-969488392244.us-central1.run.app
```

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Data source, agent model, whether a key is configured |
| `GET` | `/api/v1/fleet` | Every pilot with their current verdict |
| `GET` | `/api/v1/pilots/{id}?days=7` | Snapshot + history (`days` 1–365, default 30) |
| `POST` | `/api/v1/pilots/{id}/evaluate?refresh=true` | Go/no-go verdict; `refresh` forces a fresh run |
| `POST` | `/api/v1/pilots/{id}/pvt` | Simulated Psychomotor Vigilance Test |
| `POST` | `/api/v1/fleet/evaluate` | Evaluate the whole fleet (scheduled job; ~5 min) |
| `POST` | `/api/v1/pilots` | Create a pilot |
| `PATCH` | `/api/v1/pilots/{id}` | Update roster fields |
| `DELETE` | `/api/v1/pilots/{id}` | Remove a pilot and their readings |

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" $API/api/v1/pilots/PAT-004/evaluate
```

```json
{
  "status": "GROUNDED",
  "score": 25,
  "source": "agent",
  "reasoning": "Critical fatigue risk identified. The driver suffers from both acute (4.76 hours) and chronic (5.8 hours 7-day average) sleep deprivation...",
  "recommended_action": "Ground immediately. Require a mandatory uninterrupted rest period of at least 10 to 12 hours.",
  "risk_factors": ["Acute sleep deprivation at 4.76 hours", "..."]
}
```

---

## Security posture, and what production would need

The current deployment is a **demo running entirely on synthetic data**. No real pilot has a record in it. The choices below are deliberate for that context, and each has a named production path.

**What is already enforced:**
- The API is private; anonymous requests get `403`. Biometrics and medical history are never on an open endpoint.
- Service-to-service auth via Cloud Run ID tokens.
- No HTTP write path for biometrics — the anti-tamper boundary described above.
- Input validation at the boundary; out-of-range biometrics are rejected rather than coerced.
- Verdict provenance, so a degraded decision is visibly labelled.

**Known gaps, deliberately open:**

| Gap | Why it is acceptable now | Production path |
|---|---|---|
| Dashboard is public | Synthetic data only; judges and reviewers need to open it | IAP once the project sits under a Cloud Organization (it requires one) |
| No role-based access | Single demo audience | A pilot should see only their own readiness; fleet managers and flight surgeons see all |
| Gemini key is a plain env var | Demo project | Secret Manager |
| PVT is simulated | Demonstrates the escalation path | Integrate a real reaction-time test |
| Oura adapter untested | No ring or token available | Verify response mapping against the live API |
| `gemini-pro-latest` is a moving pointer | Fine while iterating | Pin a version and re-validate on upgrade — a safety decision engine should not change silently |

Loading real pilot data would make the first two rows blocking, not optional: medical history is regulated health data.

---

## Glossary

Aviation fatigue science and wearable metrics carry a lot of shorthand. Everything used in this project, in one place:

| Term | Stands for | What it means here |
|---|---|---|
| **PVT** | Psychomotor Vigilance Test | A real reaction-time test used in aviation and military fatigue management. Measures mean reaction time and *lapses* (attention failures). VigilEye escalates borderline pilots to a PVT instead of guessing. |
| **HRV** | Heart Rate Variability | Beat-to-beat variation, in milliseconds. **Higher is better** — it signals nervous-system recovery. Below ~35 ms suggests poor recovery or stress. The single strongest fatigue signal here. |
| **RMSSD** | Root Mean Square of Successive Differences | The specific way HRV is calculated by most wearables. When the dashboard says "HRV (RMSSD)", this is the method. |
| **WOCL** | Window of Circadian Low | The 02:00–05:00 body-clock trough where alertness bottoms out. Reporting for duty inside this window is an independent risk factor, regardless of how well the pilot slept. |
| **Resting HR** | Resting Heart Rate | Beats per minute at rest. **Lower is better.** Moves inversely to HRV — an elevated resting HR alongside suppressed HRV indicates accumulated strain. |
| **Deep sleep %** | — | Share of sleep in slow-wave stages. Healthy is >13%. The first stage to suffer under sleep debt or sleep apnea. |
| **REM sleep %** | Rapid Eye Movement | Share of sleep in REM. Healthy is >15%. Tied to cognitive recovery. |
| **Sleep architecture** | — | The overall split across deep / REM / light / awake. Two pilots can sleep 7 hours and recover very differently depending on this split. |
| **Acute vs chronic fatigue** | — | *Acute* is one bad night. *Chronic* is a 7-day average below ~6 hours. Chronic restriction dramatically amplifies the risk of any acute loss — which is why the agent receives both. |
| **Consecutive duty days** | — | Days flown without a rest period. Drives cumulative fatigue. |
| **Go/no-go** | — | The aviation term for a binary fitness-for-duty decision made before a flight. |
| **FRMS** | Fatigue Risk Management System | The regulatory framework (FAA/EASA) this problem sits inside. |
| **Agent** | — | In this codebase, specifically the Gemini model call that produces a verdict — as opposed to the deterministic *rules engine*. |

---
