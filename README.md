# VigilEye

**Agentic go/no-go readiness decisions for pilots, from wearable biometrics.**

A pilot flying several legs a day accumulates fatigue that a duty-hours table cannot see. VigilEye reads each pilot's wearable data — sleep duration and staging, heart-rate variability, resting heart rate, hours awake, consecutive duty days — and returns a **CLEAR / PENDING_TEST / GROUNDED** verdict with the reasoning behind it.

**Live dashboard:** https://vigileye-dashboard-969488392244.us-central1.run.app

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
