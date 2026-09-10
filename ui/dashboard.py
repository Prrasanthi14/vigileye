"""VigilEye pilot dashboard — a thin client over the readiness API."""

import os
from pathlib import Path

import httpx
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

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

st.set_page_config(
    page_title="VigilEye — Fleet Command Center",
    page_icon="🛡️",
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
    if query:
        text = query.strip().lower()
        matches = matches[
            matches["driver_id"].str.lower().str.contains(text, na=False)
            | matches["name"].str.lower().str.contains(text, na=False)
        ]
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

    if query or status_filter:
        st.caption(f"{len(matches)} of {len(fleet_df)} pilots match")

    if len(options) == 1:
        st.warning("No pilots match that search.")
        selected = "🏠 Fleet Command Center"
    else:
        selected = st.selectbox(
            "Select pilot", options,
            index=1 if (query or status_filter) and len(options) > 1 else 0,
            format_func=lambda opt: labels.get(opt, opt),
        )

    st.divider()
    engine = "🧠 AI agent" if health.get("agent_key_configured") else "📐 Rules engine"
    st.markdown(
        f'<div style="font-size:0.8rem; color:gray;">📡 Data: <b>{health["data_source"]}</b>'
        f'<br>⚙️ Decision engine: <b>{engine}</b></div>',
        unsafe_allow_html=True,
    )

if selected == "🏠 Fleet Command Center":
    st.markdown("## Fleet Overview (Latest Sync)")
    st.caption(f"Readiness for {len(fleet_df)} pilots, scored by the same engine as the detail view")

    icon = {"CLEAR": "✅ CLEAR", "PENDING_TEST": "🟡 PENDING", "GROUNDED": "🔴 GROUNDED"}
    grid = fleet_df.assign(Status=fleet_df["status"].map(icon))[
        ["driver_id", "name", "role", "Status", "score",
         "total_sleep_hours", "hrv_ms", "consecutive_duty_days"]
    ]
    grid.columns = ["ID", "Name", "Role", "Readiness Status", "Score",
                    "Sleep (hrs)", "HRV (ms)", "Consecutive Days"]
    st.dataframe(grid, use_container_width=True, hide_index=True, height=600)

else:
    driver_id = selected.split(" - ")[0]
    payload = get_pilot(driver_id)
    snapshot = payload["snapshot"]
    history_df = pd.DataFrame(payload["history"])

    with st.spinner("🧠 Running readiness evaluation..."):
        evaluation = get_evaluation(driver_id)

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
                    Evaluation Date: {str(snapshot.get('last_sync_timestamp', 'now'))[:10]}
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
            f"⚠️ AI agent unavailable — this verdict came from the deterministic rules engine. "
            f"Reason: {evaluation.get('fallback_reason', 'unknown')}"
        )
    st.info(evaluation["reasoning"])
    if evaluation["risk_factors"]:
        st.warning("Risk Factors: " + ", ".join(evaluation["risk_factors"]))

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
