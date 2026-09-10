"""VigilEye pilot dashboard — a thin client over the readiness API."""

import os
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from rapidfuzz import fuzz, process

API_BASE = os.getenv("API_BASE_URL", "http://localhost:8000").rstrip("/")
API = f"{API_BASE}/api/v1"
TIMEOUT = httpx.Timeout(60.0)

_METADATA_IDENTITY = (
    "http://metadata.google.internal/computeMetadata/v1/"
    "instance/service-accounts/default/identity"
)


def auth_headers() -> dict[str, str]:
    """ID token for calling the private readiness API from Cloud Run.

    Returns no header locally, where the metadata server is absent and the API
    is reachable without authentication.
    """
    if API_BASE.startswith("http://localhost"):
        return {}
    try:
        r = httpx.get(
            _METADATA_IDENTITY,
            params={"audience": API_BASE},
            headers={"Metadata-Flavor": "Google"},
            timeout=5.0,
        )
        r.raise_for_status()
        return {"Authorization": f"Bearer {r.text}"}
    except httpx.HTTPError:
        return {}

ASSETS = Path(__file__).parent / "assets"
LOGO_PATH = ASSETS / "vigileye-logo.png"      # wordmark, for the sidebar
ICON_PATH = ASSETS / "vigileye-icon.png"      # circular mark, for the browser tab

st.set_page_config(
    page_title="VigilEye — Fleet Command Center",
    page_icon=str(ICON_PATH) if ICON_PATH.exists() else "🛡️",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.html(f"<style>{(Path(__file__).parent / 'style.css').read_text()}</style>")


@st.cache_data(ttl=60)
def get_fleet() -> pd.DataFrame:
    r = httpx.get(f"{API}/fleet", timeout=TIMEOUT, headers=auth_headers())
    r.raise_for_status()
    return pd.DataFrame(r.json())


@st.cache_data(ttl=60)
def get_pilot(driver_id: str) -> dict:
    r = httpx.get(f"{API}/pilots/{driver_id}", timeout=TIMEOUT, headers=auth_headers())
    r.raise_for_status()
    return r.json()


@st.cache_data(ttl=60)
def get_evaluation(driver_id: str) -> dict:
    r = httpx.post(f"{API}/pilots/{driver_id}/evaluate", timeout=TIMEOUT, headers=auth_headers())
    r.raise_for_status()
    return r.json()


@st.cache_data(ttl=60)
def get_usage() -> dict:
    try:
        r = httpx.get(f"{API}/usage", timeout=TIMEOUT, headers=auth_headers())
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError:
        return {}


def force_reevaluation(driver_id: str) -> dict:
    """Run Gemini again now, ignoring any stored verdict."""
    r = httpx.post(f"{API}/pilots/{driver_id}/evaluate", params={"refresh": "true"},
                   timeout=TIMEOUT, headers=auth_headers())
    r.raise_for_status()
    return r.json()


FUZZY_CUTOFF = 72
MAX_AGE_MIN = int(os.getenv("VERDICT_MAX_AGE_MINUTES", "120"))


def format_age(minutes) -> str:
    """Human-readable age of a verdict."""
    if minutes is None or pd.isna(minutes):
        return "not yet judged"
    minutes = int(minutes)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes} min ago"
    hours = minutes / 60
    if hours < 24:
        return f"{hours:.1f} h ago"
    return f"{hours / 24:.1f} d ago"


def fuzzy_match(df: pd.DataFrame, text: str, limit: int = 10) -> pd.DataFrame:
    """Nearest pilots by name or ID, best match first.

    Name and ID are scored as separate fields. Matching against them joined
    dilutes short queries — "lnda" scores too low against "pat-016 linda davis"
    to clear the cutoff, but matches "linda davis" cleanly.
    """
    if df.empty:
        return df

    best: dict[int, float] = {}
    for column in ("name", "driver_id"):
        haystack = df[column].str.lower().tolist()
        for _, score, index in process.extract(
            text, haystack, scorer=fuzz.WRatio, limit=limit, score_cutoff=FUZZY_CUTOFF
        ):
            best[index] = max(best.get(index, 0), score)

    ranked = sorted(best, key=lambda i: -best[i])[:limit]
    return df.iloc[ranked]


def api_health() -> dict:
    # Generous timeout: the API scales to zero, so the first call of an idle
    # period pays for a container boot plus BigQuery client auth.
    try:
        return httpx.get(f"{API_BASE}/health", timeout=60.0, headers=auth_headers()).json()
    except httpx.HTTPError as exc:
        return {"status": "unreachable", "error": str(exc)}


health = api_health()
if health.get("status") != "ok":
    st.error(f"Readiness API unreachable at {API_BASE} — {health.get('error', 'unknown error')}")
    st.stop()

with st.sidebar:
    if LOGO_PATH.exists():
        st.image(str(LOGO_PATH), use_container_width=True)
    else:
        st.markdown("# 🛡️ VigilEye")
    st.caption("Pilot Readiness Command Center")
    st.divider()

    fleet_df = get_fleet()

    query = st.text_input("🔎 **Search pilot**", placeholder="Name or ID, e.g. PAT-004 or Casey")
    status_filter = st.multiselect(
        "Filter by status", ["GROUNDED", "PENDING_TEST", "CLEAR"],
        default=[], placeholder="Any status",
    )

    matches = fleet_df
    fuzzy_hit = False
    if query:
        text = query.strip().lower()
        exact = matches[
            matches["driver_id"].str.lower().str.contains(text, na=False, regex=False)
            | matches["name"].str.lower().str.contains(text, na=False, regex=False)
        ]
        if exact.empty:
            # Nothing matched literally — fall back to fuzzy, so a misspelling
            # ("davies", "PAT-04") still finds the pilot instead of dead-ending.
            matches = fuzzy_match(matches, text)
            fuzzy_hit = not matches.empty
        else:
            matches = exact

    if status_filter:
        matches = matches[matches["status"].isin(status_filter)]

    badge = {"CLEAR": "✅", "PENDING_TEST": "🟡", "GROUNDED": "🔴"}
    options = ["🏠 Fleet Command Center"] + [
        f"{row.driver_id} - {row.name}" for row in matches.itertuples()
    ]
    labels = {opt: opt for opt in options}
    for row in matches.itertuples():
        key = f"{row.driver_id} - {row.name}"
        labels[key] = f"{badge.get(row.status, '')} {key}"

    filtering = bool(query or status_filter)
    if filtering:
        note = " (closest matches)" if fuzzy_hit else ""
        st.caption(f"{len(matches)} of {len(fleet_df)} pilots match{note}")

    if len(options) == 1:
        st.warning("No pilots match that search.")
        selected = "🏠 Fleet Command Center"
    else:
        # Jump straight to a pilot only when the filter leaves exactly one.
        # With several matches, stay on the fleet view so every match is visible
        # in the table rather than silently landing on the first one.
        selected = st.selectbox(
            "Select pilot", options,
            index=1 if filtering and len(options) == 2 else 0,
            format_func=lambda opt: labels.get(opt, opt),
        )

    st.divider()
    engine = "🧠 Gemini (GenAI)" if health.get("agent_key_configured") else "📐 Rules engine"
    st.markdown(
        f'<div style="font-size:0.8rem; color:gray;">📡 Data: <b>{health["data_source"]}</b>'
        f'<br>⚙️ Decision engine: <b>{engine}</b></div>',
        unsafe_allow_html=True,
    )

    tokens = get_usage()
    if tokens:
        budget = tokens.get("daily_budget")
        spent = f'{tokens["tokens_used"]:,}' + (f" of {budget:,}" if budget else "")
        st.markdown(
            f'<div style="font-size:0.8rem; color:gray; margin-top:6px;">'
            f'🔢 Gemini tokens today: <b>{spent}</b>'
            f'<br>♻️ Saved by stored verdicts: <b>{tokens["tokens_saved_by_store"]:,}</b>'
            f' ({tokens["verdicts_served_from_store"]} reuses)</div>',
            unsafe_allow_html=True,
        )

if selected == "🏠 Fleet Command Center":
    st.markdown("## Fleet Overview")
    if filtering:
        st.caption(f"Showing {len(matches)} of {len(fleet_df)} pilots matching your filter")
    else:
        st.caption(f"Readiness for {len(fleet_df)} pilots, scored by the same engine as the detail view")

    icon = {"CLEAR": "✅ CLEAR", "PENDING_TEST": "🟡 PENDING", "GROUNDED": "🔴 GROUNDED"}

    # Risk first. This dashboard exists to surface exceptions, so the pilots who
    # cannot fly lead; a reader should never scroll past cleared crew to find
    # them. Column headers still re-sort by name, ID or score on click.
    order = {"GROUNDED": 0, "PENDING_TEST": 1, "CLEAR": 2}
    # The table shows the filtered set: filtering only the sidebar dropdown
    # left the grid showing all 100 pilots, which read as the filter doing nothing.
    ranked = matches.assign(_rank=matches["status"].map(order)).sort_values(
        ["_rank", "score"], ascending=[True, True]
    )

    stale_count = int(ranked["stale"].sum()) if "stale" in ranked else 0
    if stale_count:
        st.warning(
            f"⏱️ {stale_count} verdict(s) older than {MAX_AGE_MIN // 60}h — "
            "re-evaluated automatically when that pilot is next opened."
        )

    grid = ranked.assign(
        Status=ranked["status"].map(icon),
        Judged=ranked["age_minutes"].map(format_age),
    )[["driver_id", "name", "role", "Status", "score", "Judged",
       "total_sleep_hours", "hrv_ms", "consecutive_duty_days"]]
    grid.columns = ["ID", "Name", "Role", "Readiness Status", "Score", "Verdict age",
                    "Sleep (hrs)", "HRV (ms)", "Consecutive Days"]
    st.dataframe(grid, use_container_width=True, hide_index=True, height=600)

else:
    driver_id = selected.split(" - ")[0]
    payload = get_pilot(driver_id)
    snapshot = payload["snapshot"]
    history_df = pd.DataFrame(payload["history"])

    with st.spinner("🧠 Running readiness evaluation..."):
        evaluation = get_evaluation(driver_id)

    # A verdict under the staleness threshold is reused rather than recomputed,
    # so opening a pilot does not itself mean a fresh judgement was made.
    if st.session_state.pop(f"refreshed_{driver_id}", False):
        st.toast(f"{driver_id} re-evaluated just now", icon="🧠")

    initials = "".join(n[0] for n in str(snapshot.get("name", "??")).split()[:2])
    medical = snapshot.get("medical_history", "None")
    badge = (
        f'<span style="background: rgba(255,193,7,0.2); color:#FFC107; padding:2px 8px;'
        f' border-radius:4px; font-size:0.75rem; margin-left:8px;">⚕️ {medical}</span>'
        if medical and medical != "None" else ""
    )
    st.markdown(f"""
    <div class="driver-info">
        <div class="driver-avatar">{initials}</div>
        <div>
            <div class="driver-name">{snapshot.get('name', 'Unknown')} ({driver_id}){badge}</div>
            <div class="driver-role">{snapshot.get('role','')} • {snapshot.get('shift_type','')} Shift
                • Report: {snapshot.get('report_time','--:--')}</div>
        </div>
    </div>
    """, unsafe_allow_html=True)

    status, score = evaluation["status"], evaluation["score"]

    stamped = evaluation.get("evaluated_at")
    if stamped:
        age = (datetime.now(timezone.utc)
               - datetime.fromisoformat(stamped)).total_seconds() / 60
        verdict_age = format_age(age)
    else:
        verdict_age = "just now"
    status_class = {"CLEAR": "status-clear", "PENDING_TEST": "status-pending",
                    "GROUNDED": "status-grounded"}.get(status, "status-pending")
    status_icon = {"CLEAR": "✅", "PENDING_TEST": "⚠️", "GROUNDED": "🛑"}.get(status, "⚠️")

    with st.container(key="hero-card"):
        col_status, col_score, col_gauge = st.columns([2, 1, 2])
        with col_status:
            st.markdown(f"""
            <div style="padding: 24px 28px;">
                <div class="status-badge {status_class}">{status_icon} {status}</div>
                <div style="margin-top:16px; font-size:0.82rem; color:rgba(232,236,241,0.5);">
                    Readings from: {str(snapshot.get('last_sync_timestamp', 'now'))[:10]}<br>
                    Verdict reached: {verdict_age}
                </div>
            </div>
            """, unsafe_allow_html=True)
        with col_score:
            st.markdown(f"""
            <div style="padding:24px 16px; text-align:center;">
                <div style="font-size:4.5rem; font-weight:900; line-height:1; color:#E8ECF1;">{score}</div>
                <div class="score-label">Readiness Score</div>
            </div>
            """, unsafe_allow_html=True)
        with col_gauge:
            color = {"CLEAR": "#00D4AA", "PENDING_TEST": "#FFC107",
                     "GROUNDED": "#FF3D57"}.get(status, "#FFC107")
            fig = go.Figure(go.Indicator(
                mode="gauge+number", value=score,
                number={"suffix": "", "font": {"size": 1, "color": "rgba(0,0,0,0)"}},
                gauge={"axis": {"range": [0, 100], "tickcolor": "rgba(232,236,241,0.3)"},
                       "bar": {"color": color, "thickness": 0.75},
                       "bgcolor": "rgba(255,255,255,0.04)", "borderwidth": 0},
            ))
            fig.update_layout(height=180, margin=dict(l=20, r=20, t=30, b=10),
                              paper_bgcolor="rgba(0,0,0,0)", font={"color": "#E8ECF1"})
            st.plotly_chart(fig, use_container_width=True)

    # Provenance: never present a deterministic fallback as agent output.
    if evaluation["source"] == "agent":
        st.markdown('<div class="section-title">🧠 AI Fatigue Analysis</div>',
                    unsafe_allow_html=True)
    else:
        st.markdown('<div class="section-title">📐 Rules-Based Analysis</div>',
                    unsafe_allow_html=True)
        st.warning(
            f"⚠️ Gemini unavailable — this verdict came from the deterministic rules engine. "
            f"Reason: {evaluation.get('fallback_reason', 'unknown')}"
        )
    st.info(evaluation["reasoning"])
    if evaluation["risk_factors"]:
        st.warning("Risk Factors: " + ", ".join(evaluation["risk_factors"]))

    age_col, button_col = st.columns([3, 1])
    with age_col:
        st.caption(
            f"Verdict reached **{verdict_age}** from readings dated "
            f"{str(snapshot.get('last_sync_timestamp', '—'))[:10]}. "
            f"Verdicts under {MAX_AGE_MIN // 60}h old are reused rather than recomputed."
        )
    with button_col:
        if st.button("🔄 Re-evaluate now", use_container_width=True,
                     help="Run Gemini again against the latest readings"):
            with st.spinner("Running Gemini..."):
                force_reevaluation(driver_id)
            get_evaluation.clear()
            get_fleet.clear()
            get_usage.clear()
            st.session_state[f"refreshed_{driver_id}"] = True
            st.rerun()

    if not history_df.empty:
        st.markdown('<div class="section-title">📈 30-Day Historical Trend</div>',
                    unsafe_allow_html=True)
        fig_hist = go.Figure()
        fig_hist.add_trace(go.Scatter(x=history_df["date"], y=history_df["total_sleep_hours"],
                                      mode="lines+markers", name="Sleep (hrs)",
                                      line=dict(color="#00D4AA", width=3)))
        fig_hist.add_trace(go.Scatter(x=history_df["date"], y=history_df["hrv_ms"], mode="lines",
                                      name="HRV (ms)", yaxis="y2",
                                      line=dict(color="#6366F1", width=2, dash="dot")))
        fig_hist.update_layout(
            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            font=dict(color="#E8ECF1", family="Inter"), hovermode="x unified",
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
            margin=dict(l=0, r=0, t=40, b=0), xaxis=dict(showgrid=False),
            yaxis=dict(title="Sleep Hours", showgrid=True, gridcolor="rgba(255,255,255,0.04)"),
            yaxis2=dict(title="HRV (ms)", overlaying="y", side="right", showgrid=False),
        )
        st.plotly_chart(fig_hist, use_container_width=True)

    st.markdown('<div class="section-title">📊 Latest Sync Biometrics</div>',
                unsafe_allow_html=True)

    def metric(col, label, value, unit, detail, ok):
        with col:
            key = label.replace(" ", "-").replace("(", "").replace(")", "").lower()
            with st.container(key=f"metric-card-{key}"):
                st.markdown(f"""
                <div style="padding:20px;">
                    <div class="metric-label">{label}</div>
                    <div class="metric-value">{value}<span style="font-size:0.9rem; font-weight:400;
                        color:rgba(232,236,241,0.4);"> {unit}</span></div>
                    <div style="font-size:0.85rem; color:rgba(232,236,241,0.6); margin-top:4px;">
                        {"🟢" if ok else "🔴"} {detail}</div>
                </div>
                """, unsafe_allow_html=True)

    g = lambda k: snapshot.get(k, 0) or 0
    cols = st.columns(5)
    metric(cols[0], "Sleep Duration", g("total_sleep_hours"), "hrs", "≥6h optimal", g("total_sleep_hours") >= 6)
    metric(cols[1], "Deep Sleep", f"{g('deep_sleep_pct')}%", "", "≥13% optimal", g("deep_sleep_pct") >= 13)
    metric(cols[2], "REM Sleep", f"{g('rem_sleep_pct')}%", "", "≥15% optimal", g("rem_sleep_pct") >= 15)
    metric(cols[3], "HRV (RMSSD)", g("hrv_ms"), "ms", "≥50ms recovery", g("hrv_ms") >= 50)
    metric(cols[4], "7-Day Avg Sleep", g("seven_day_avg_sleep"), "hrs", "Cumulative fatigue check",
           g("seven_day_avg_sleep") >= 6)

    if status == "PENDING_TEST":
        st.markdown('<div class="section-title">🧪 Trigger PVT</div>',
                    unsafe_allow_html=True)
        if st.button("🧪 Trigger Psychomotor Vigilance Test", use_container_width=True):
            with st.spinner("Running PVT..."):
                pvt = httpx.post(f"{API}/pilots/{driver_id}/pvt", timeout=TIMEOUT, headers=auth_headers()).json()
                st.success(f"Result: **{pvt['result']}** | Mean RT: {pvt['mean_reaction_ms']}ms "
                           f"| Lapses: {pvt['lapses']}")
