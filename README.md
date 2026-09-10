# VigilEye

**Agentic go/no-go readiness decisions for pilots, from wearable biometrics.**

A pilot flying several legs a day accumulates fatigue that a duty-hours table cannot see. VigilEye reads each pilot's wearable data — sleep duration and staging, heart-rate variability, resting heart rate, hours awake, consecutive duty days — and returns a **CLEAR / PENDING_TEST / GROUNDED** verdict with the reasoning behind it.

**Live dashboard:** https://vigileye-dashboard-969488392244.us-central1.run.app

> New to the terminology (PVT, HRV, WOCL)? There's a [glossary at the end](#glossary).

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

### What happens when you click a pilot

The diagram above is where the *data* comes from. This is what happens on a *request* — and the short answer is that the agent runs one pilot at a time, on demand, not as a batch over everyone:

```
You open a pilot in the dashboard
          │
          ▼
POST /api/v1/pilots/{id}/evaluate
          │
          ▼
  Read that pilot's latest row from BigQuery
          │
          ▼
  Is a verdict already stored for (this pilot, this reading date),
  and less than 2 hours old?
          │
    ┌─────┴─────┐
   YES          NO
    │            │
    │            ▼
    │      Call Gemini Pro  ──── fails? ───►  Rules engine
    │       (~21 s)          (no key,          (instant)
    │            │            quota, outage)        │
    │            ▼                                  ▼
    │      Store the verdict                 source="rules"
    │            │                           + the reason why
    ▼            ▼                                  │
 Return stored ──┴──────────────────────────────────┘
   (~2 s)                    │
                             ▼
              Dashboard shows the verdict, labelled
              with which engine produced it
```

Two other entry points exist:

- **Opening the fleet grid** reads stored verdicts only — **zero** model calls, however many times you load it.
- **`POST /api/v1/fleet/evaluate`** sweeps every pilot, one model call each, skipping any whose verdict is current. It is triggered by an operator; nothing runs it on a timer.

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

## System design

Numbers below were measured on this deployment.

### Scalability

The API keeps no state between requests, so Cloud Run runs as many copies as needed and scales to zero when idle. Readings and verdicts live in BigQuery, so any copy can serve any request.

The bigger lever is how often the model is called. A verdict is stored against `(pilot, reading date)`, so the agent runs **once per pilot per day** rather than once per page view. Opening the fleet grid 500 times costs no model calls at all.

A whole-fleet sweep costs one model call per pilot, so it grows linearly:

| Fleet | Full sweep |
|---|---|
| 100 (this deployment) | **5 min**, measured |
| ~5,000 | ~4.2 hours |
| ~15,000 | ~12.5 hours |

Past a few hundred pilots, sweeping everyone stops working. The roster already stores each pilot's `report_time`, so the same work can be spread out by evaluating each pilot shortly before they report — which also only touches pilots actually flying that day. At 5,000 pilots with 40% on duty that is about 1.4 calls per minute. This is not built yet.

### Latency

Three paths, with different costs:

| Path | Model calls | Time |
|---|---|---|
| Fleet grid, 100 pilots | 0 | two BigQuery reads |
| Pilot page, verdict already stored | 0 | **2.3 s** |
| Pilot page, no stored verdict | 1 | **21.3 s** |

That 2.3s vs 21.3s difference is why verdicts are stored. Batch runs use 6 concurrent workers. The dashboard holds API responses for 60 seconds. The health check allows 60 seconds because the API scales to zero, so the first request after an idle period waits for a container to start and BigQuery to authenticate.

Verdicts also expire. `time_awake_since_last_sleep` grows over the day, so a verdict gets less accurate with age even if the stored reading has not changed. Past `VERDICT_MAX_AGE_MINUTES` (default 120) it is recomputed.

### Rate limiting

Gemini Pro allows 25 requests per minute. An early fleet run used 12 workers at once, exceeded that, and 71 of 100 pilots fell back to the rules engine.

Two things prevent it now. Calls are paced below the quota:

```python
# src/vigileye/scoring/agent.py
RPM_LIMIT = int(os.getenv("GEMINI_RPM", "20"))

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

And a `RESOURCE_EXHAUSTED` response is retried with a delay. After the fix the same run gave 71 evaluated, 0 failed. `GEMINI_RPM` should be set to match whatever quota the account has.

### Fault tolerance

If the agent cannot run, the deterministic rules engine answers instead, so the system returns a verdict rather than an error.

Every verdict records which engine produced it (`source`) and, for a fallback, why (`fallback_reason`). The dashboard shows a different heading and a warning. This is how the 71-failure run above was noticed.

A BigQuery client that fails to start leaves the connector returning empty results rather than crashing. Verdict store read and write failures are logged and ignored, so losing the store does not lose the verdict. Rules fallbacks are not stored, so the agent is retried next time instead of a downgraded answer being kept.

### Consistency

This project previously had three different definitions of go/no-go: a short heuristic in the dashboard, the rules engine, and the agent. A pilot could show CLEAR in the grid and GROUNDED on their own page.

There is now one scoring path and one stored verdict per pilot-day, read by both views. Editing a pilot's roster record clears their verdict, and ingestion replaces rather than appends per pilot-day, so two rows for one day cannot exist and make "latest reading" ambiguous.

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

## Running it yourself

### Option 0 — just open the live one

**https://vigileye-dashboard-969488392244.us-central1.run.app**

Nothing to install. Everything below is for running your own copy.

---

### Option A — on your machine, no Google Cloud account needed

Uses SQLite instead of BigQuery, so there is no project to create and no billing to enable. Takes about five minutes.

**Requirements:** Python 3.11 or newer, and `git`.

**Step 1 — get the code and install it**

```bash
git clone <repo-url>
cd vigileye
pip install -e ".[api,ui,dev]"
```

**Step 2 — get a free Gemini API key**

1. Open **https://aistudio.google.com/apikey**
2. Sign in with any Google account
3. Click **Create API key** → **Create API key in new project**
4. Copy the key

**Step 3 — configure**

```bash
cp .env.example .env
```

Edit `.env` so it contains at least:

```
GEMINI_API_KEY=paste-your-key-here
DATA_SOURCE=sqlite
```

**Step 4 — create the database**

```bash
python scripts/bootstrap.py --source sqlite
```

Expected output:

```
Created 100 pilots and 3000 readings in vigileye_fleet.db.
Next: start the API, then the dashboard (see README).
```

**Step 5 — start the backend** (leave it running)

```bash
uvicorn vigileye.api.main:app --port 8000
```

Check it in a second terminal:

```bash
curl http://localhost:8000/health
```

You should see `"data_connected":true` and `"agent_key_configured":true`.

**Step 6 — start the dashboard** (a third terminal)

```bash
API_BASE_URL=http://localhost:8000 streamlit run ui/dashboard.py
```

Open **http://localhost:8501**.

**What you should see:** a fleet of 100 pilots, GROUNDED ones first. Click any pilot — the first evaluation takes ~20 seconds because it calls the model; the verdict is then stored, so reopening is instant. The sidebar shows whether decisions are coming from the **AI agent** or the **rules engine**.

> **No Gemini key?** It still runs. Every verdict comes from the deterministic rules engine and the UI labels it plainly, with the reason. That is the fallback behaviour working, and is worth seeing.

---

### Option B — with your own Google Cloud project

Only needed to use BigQuery, as the live deployment does.

**Step 1 — create a project** at https://console.cloud.google.com and note the project ID.

**Step 2 — enable BigQuery and sign in**

```bash
gcloud services enable bigquery.googleapis.com
gcloud auth application-default login
```

**Step 3 — configure `.env`**

```
GCP_PROJECT_ID=your-project-id
BIGQUERY_DATASET=vigileye_fleet
DATA_SOURCE=bigquery
GEMINI_API_KEY=paste-your-key-here
```

**Step 4 — create the dataset and fill it**

```bash
python scripts/bootstrap.py --source bigquery
```

This refuses to run if the dataset already holds pilots, so it cannot overwrite a working deployment by accident. Pass `--force` to overwrite deliberately.

**Step 5 and 6** are the same as Option A.

---

### Running the tests

```bash
pytest        # 65 tests
```

No credentials, no network access and no model calls: the suite runs against SQLite with the agent and verdict store stubbed.

### Regenerating the data

```bash
python scripts/bootstrap.py --source sqlite --pilots 50 --days 60   # rebuild from scratch
python scripts/generate_fleet.py --days 30 --replace                # readings only, BigQuery
```

The generator models physiology rather than drawing independent random numbers: day-to-day persistence, accumulating sleep debt, per-pilot baselines, night-shift degradation, sleep apnea suppressing deep sleep, and occasional acute disruptions. On the current dataset that produces a sleep-to-HRV correlation of about 0.47, in the range seen in real wearable data.

### Deploying to Cloud Run

```bash
# 1. API — private, so only the dashboard can reach the biometric data
gcloud run deploy vigileye-api --source . --region us-central1 \
  --timeout 900 --no-allow-unauthenticated \
  --set-env-vars "GCP_PROJECT_ID=your-project,BIGQUERY_DATASET=vigileye_fleet,DATA_SOURCE=bigquery"

gcloud run services update vigileye-api --region us-central1 \
  --update-env-vars GEMINI_API_KEY=paste-your-key-here

# 2. Dashboard
gcloud builds submit --config cloudbuild.ui.yaml \
  --substitutions _IMAGE=gcr.io/your-project/vigileye-ui:v1
gcloud run deploy vigileye-dashboard --region us-central1 \
  --image gcr.io/your-project/vigileye-ui:v1 --allow-unauthenticated \
  --set-env-vars API_BASE_URL=https://your-api-url

# 3. Let the dashboard call the private API
gcloud run services add-iam-policy-binding vigileye-api --region us-central1 \
  --member="serviceAccount:PROJECT_NUMBER-compute@developer.gserviceaccount.com" \
  --role="roles/run.invoker"
```

Opening the API URL in a browser returns **403**. That is correct — it has no identity token. Use the dashboard URL.


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
