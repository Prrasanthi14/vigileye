"""
Patchamomma: Agentic Pilot & Commercial Driver Safety Dashboard
================================================================
A premium Streamlit dashboard for evaluating driver/pilot readiness 
using wearable biometric data and AI-powered fatigue risk analysis.
Now upgraded with an Enterprise Fleet Management view and time-series history!
"""

import streamlit as st
import plotly.graph_objects as go
import pandas as pd
import time as time_module

from data_connector import get_connector
from agent_evaluator import evaluate_readiness, simulate_pvt_test

# Initialize Data Connector
connector = get_connector('bigquery')

# ─── Page Config ───────────────────────────────────────────────
st.set_page_config(
    page_title="Patchamomma — Fleet Command Center",
    page_icon="🛡️",
    layout="wide",
    initial_sidebar_state="expanded"
)

# ─── Custom CSS ────────────────────────────────────────────────
with open("style.css") as css_file:
    st.html(f"<style>{css_file.read()}</style>")


# ─── Sidebar ───────────────────────────────────────────────────
with st.sidebar:
    st.markdown("# 🛡️ Patchamomma")
    st.caption("Enterprise Fleet Command Center")
    st.divider()

    # Load fleet summary for dropdown
    fleet_df = connector.get_fleet_summary()
    pilot_options = ["🏠 Fleet Command Center"] + [f"{row['driver_id']} - {row['name']}" for _, row in fleet_df.iterrows()]
    
    selected_option = st.selectbox(
        ":material/search: **Search or Select Pilot**",
        options=pilot_options,
        index=0
    )
    
    st.divider()
    st.markdown(f'<div style="font-size:0.8rem; color:gray;">📡 Connected to:<br><b>{connector.get_source_name()}</b></div>', unsafe_allow_html=True)


# ─── Main Content ──────────────────────────────────────────────

if selected_option == "🏠 Fleet Command Center":
    # ─── VIEW 1: FLEET GRID ────────────────────────────────
    st.markdown("## Fleet Overview (Last 24 Hours)")
    st.caption(f"Displaying real-time sync data for {len(fleet_df)} pilots")
    
    # Calculate quick status for the grid based on simple heuristics (since running Gemini for 100 pilots takes too long)
    def quick_status(row):
        if row['total_sleep_hours'] < 5 or row['hrv_ms'] < 30: return "🔴 GROUNDED"
        if row['total_sleep_hours'] < 6 or row['consecutive_duty_days'] >= 4: return "🟡 PENDING"
        return "✅ CLEAR"
        
    fleet_df['Status'] = fleet_df.apply(quick_status, axis=1)
    
    # Reorder and rename columns for display
    display_df = fleet_df[['driver_id', 'name', 'role', 'Status', 'total_sleep_hours', 'hrv_ms', 'consecutive_duty_days']].copy()
    display_df.columns = ['ID', 'Name', 'Role', 'Readiness Status', 'Latest Sleep (hrs)', 'Latest HRV (ms)', 'Consecutive Days']
    
    st.dataframe(
        display_df,
        use_container_width=True,
        hide_index=True,
        height=600
    )

else:
    # ─── VIEW 2: PILOT DRILL-DOWN ──────────────────────────
    driver_id = selected_option.split(" - ")[0]
    
    # Fetch data
    driver_data = connector.fetch_latest_data(driver_id)
    history_df = connector.get_pilot_history(driver_id, days=30)
    
    if not driver_data:
        st.error(f"Could not find data for {driver_id}")
        st.stop()
        
    # Inject 7-day trend into payload for Gemini evaluator to consider cumulative fatigue
    recent_7 = history_df.tail(7)
    driver_data['7_day_avg_sleep'] = round(recent_7['total_sleep_hours'].mean(), 1)
    driver_data['7_day_avg_hrv'] = round(recent_7['hrv_ms'].mean(), 1)

    with st.spinner("🧠 Running agentic evaluation..."):
        evaluation = evaluate_readiness(driver_data)

    # Driver Info Bar
    initials = "".join([n[0] for n in driver_data.get("name", "??").split()[:2]])
    medical_history = driver_data.get('medical_history', 'None')
    medical_badge = f'<span style="background: rgba(255, 193, 7, 0.2); color: #FFC107; padding: 2px 8px; border-radius: 4px; font-size: 0.75rem; margin-left: 8px;">⚕️ {medical_history}</span>' if medical_history != 'None' else ''
    
    st.markdown(f"""
    <div class="driver-info">
        <div class="driver-avatar">{initials}</div>
        <div>
            <div class="driver-name">{driver_data.get('name', 'Unknown Driver')} ({driver_id}){medical_badge}</div>
            <div class="driver-role">{driver_data.get('role', '')} • {driver_data.get('shift_type', '')} Shift • Report: {driver_data.get('report_time', '--:--')}</div>
        </div>
    </div>
    """, unsafe_allow_html=True)

    # Hero Status Card
    status = evaluation.status
    score = evaluation.score

    status_class = {"CLEAR": "status-clear", "PENDING_TEST": "status-pending", "GROUNDED": "status-grounded"}.get(status, "status-pending")
    status_icon = {"CLEAR": "✅", "PENDING_TEST": "⚠️", "GROUNDED": "🛑"}.get(status, "⚠️")

    with st.container(key="hero-card"):
        col_status, col_score, col_gauge = st.columns([2, 1, 2])

        with col_status:
            st.markdown(f"""
            <div style="padding: 24px 28px;">
                <div class="status-badge {status_class}">
                    {status_icon} {status}
                </div>
                <div style="margin-top: 16px; font-size: 0.82rem; color: rgba(232,236,241,0.5);">
                    Evaluation Date: {str(driver_data.get('last_sync_timestamp', 'now'))[:10]}
                </div>
            </div>
            """, unsafe_allow_html=True)

        with col_score:
            st.markdown(f"""
            <div style="padding: 24px 16px; text-align: center;">
                <div style="font-size: 4.5rem; font-weight: 900; line-height: 1; color: #E8ECF1;">{score}</div>
                <div class="score-label">Readiness Score</div>
            </div>
            """, unsafe_allow_html=True)

        with col_gauge:
            gauge_color = {"CLEAR": "#00D4AA", "PENDING_TEST": "#FFC107", "GROUNDED": "#FF3D57"}.get(status, "#FFC107")
            fig_gauge = go.Figure(go.Indicator(
                mode="gauge+number",
                value=score,
                number={"suffix": "", "font": {"size": 1, "color": "rgba(0,0,0,0)"}},
                gauge={
                    "axis": {"range": [0, 100], "tickwidth": 0, "tickcolor": "rgba(0,0,0,0)", "tickfont": {"size": 1}},
                    "bar": {"color": gauge_color, "thickness": 0.85},
                    "bgcolor": "rgba(255,255,255,0.03)",
                    "borderwidth": 0,
                }
            ))
            fig_gauge.update_layout(paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", height=160, margin=dict(l=20, r=20, t=30, b=10))
            st.plotly_chart(fig_gauge, use_container_width=True)

    # Agent Analysis
    st.markdown('<div class="section-title">:material/psychology: AI Cumulative Fatigue Analysis</div>', unsafe_allow_html=True)
    st.info(evaluation.reasoning)
    if evaluation.risk_factors:
        st.warning("Risk Factors: " + ", ".join(evaluation.risk_factors))

    # Historical Time-Series Chart
    st.markdown('<div class="section-title">:material/show_chart: 30-Day Historical Trend</div>', unsafe_allow_html=True)
    
    fig_hist = go.Figure()
    fig_hist.add_trace(go.Scatter(
        x=history_df['date'], y=history_df['total_sleep_hours'], 
        mode='lines+markers', name='Sleep (hrs)', line=dict(color='#00D4AA', width=3)
    ))
    fig_hist.add_trace(go.Scatter(
        x=history_df['date'], y=history_df['hrv_ms'], 
        mode='lines', name='HRV (ms)', yaxis='y2', line=dict(color='#6366F1', width=2, dash='dot')
    ))
    
    fig_hist.update_layout(
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#E8ECF1", family="Inter"),
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        margin=dict(l=0, r=0, t=40, b=0),
        xaxis=dict(showgrid=False),
        yaxis=dict(title="Sleep Hours", showgrid=True, gridcolor="rgba(255,255,255,0.04)"),
        yaxis2=dict(title="HRV (ms)", overlaying="y", side="right", showgrid=False)
    )
    st.plotly_chart(fig_hist, use_container_width=True)
    
    # Biometric Metrics Row
    st.markdown('<div class="section-title">:material/monitoring: Latest Sync Biometrics</div>', unsafe_allow_html=True)
    m1, m2, m3, m4, m5 = st.columns(5)

    def render_metric(col, label, value, unit, detail, threshold_ok):
        with col:
            safe_label = label.replace(" ", "-").replace("(", "").replace(")", "").lower()
            with st.container(key=f"metric-card-{safe_label}"):
                indicator = "🟢" if threshold_ok else "🔴"
                st.markdown(f"""
                <div style="padding: 20px;">
                    <div class="metric-label">{label}</div>
                    <div class="metric-value">{value}<span style="font-size:0.9rem; font-weight:400; color:rgba(232,236,241,0.4);"> {unit}</span></div>
                    <div style="font-size: 0.85rem; color: rgba(232, 236, 241, 0.6); margin-top:4px;">{indicator} {detail}</div>
                </div>
                """, unsafe_allow_html=True)

    render_metric(m1, "Sleep Duration", driver_data.get("total_sleep_hours", 0), "hrs", "≥6h optimal", driver_data.get("total_sleep_hours", 0) >= 6)
    render_metric(m2, "Deep Sleep", f"{driver_data.get('deep_sleep_pct', 0)}%", "", "≥13% optimal", driver_data.get("deep_sleep_pct", 0) >= 13)
    render_metric(m3, "REM Sleep", f"{driver_data.get('rem_sleep_pct', 0)}%", "", "≥15% optimal", driver_data.get("rem_sleep_pct", 0) >= 15)
    render_metric(m4, "HRV (RMSSD)", driver_data.get("hrv_ms", 0), "ms", "≥50ms recovery", driver_data.get("hrv_ms", 0) >= 50)
    render_metric(m5, "7-Day Avg Sleep", driver_data.get("7_day_avg_sleep", 0), "hrs", "Cumulative fatigue check", driver_data.get("7_day_avg_sleep", 0) >= 6)

    # PVT Test
    if evaluation.status == "PENDING_TEST":
        st.markdown('<div class="section-title">:material/science: Trigger PVT</div>', unsafe_allow_html=True)
        if st.button("🧪 Trigger Psychomotor Vigilance Test", use_container_width=True):
            with st.spinner("Running PVT..."):
                time_module.sleep(1)
                pvt = simulate_pvt_test(evaluation.score)
                st.success(f"Result: **{pvt['result']}** | Mean RT: {pvt['mean_reaction_ms']}ms | Lapses: {pvt['lapses']}")
