import json
import os

import pandas as pd
import requests
import streamlit as st

BACKEND = os.getenv("BACKEND_URL", "http://localhost:8000")
DEFAULT_Q = "Pourquoi le taux d'erreur a-t-il augmenté sur le service de paiement ?"

st.set_page_config(page_title="Root Cause Agent", layout="wide")
st.title("🔎 Root Cause Agent")
st.caption("Diagnostic d'incident sur ClickHouse · OpenSearch · PostgreSQL (Aiven)")

left, right = st.columns([3, 2])
with left:
    question = st.text_input("Question de l'ingénieur d'astreinte", value=st.session_state.get("q", DEFAULT_Q))
    c1, c2 = st.columns(2)
    run = c1.button("Diagnostiquer", type="primary", use_container_width=True)
    replay = c2.button("Rejouer la démo", use_container_width=True)
    st.subheader("Timeline des actions")
    timeline = st.container()
with right:
    card = st.container()


def render_data(ev):
    d = ev.get("data") or {}
    if ev.get("step") == "detect_anomaly" and d.get("minutes"):
        df = pd.DataFrame(d["minutes"]).set_index("minute")[["error_rate"]]
        st.line_chart(df)
    elif ev.get("step") == "find_faulty_span" and d.get("operations"):
        st.dataframe(pd.DataFrame(d["operations"]), hide_index=True)
    elif ev.get("step") == "check_business_impact":
        st.json(d, expanded=False)


def render_final(ev):
    rc, m = ev["root_cause"], ev["metrics"]
    with card:
        st.subheader("Cause racine")
        st.success(rc["summary"])
        st.markdown(f"**Début :** {rc['incident_start']}  \n**Composant :** `{rc['faulty_component']}`  \n"
                    f"**Preuve :** {rc['evidence']}  \n**Impact :** {rc['business_impact']}")
        a, b = st.columns(2)
        a.metric("Temps de diagnostic", f"{m['diagnosis_seconds']} s", f"vs {m['human_baseline_seconds'] // 60} min humain", delta_color="off")
        b.metric("Réduction MTTR", f"{m['mttr_reduction']:.1%}")
        c, d = st.columns(2)
        c.metric("Preuves", m["evidence_count"])
        d.metric("Accuracy", "n/a" if m["accuracy"] is None else f"{m['accuracy']:.0%}")


if run or replay:
    if replay:
        question = DEFAULT_Q
    st.session_state["q"] = question
    try:
        with requests.post(f"{BACKEND}/diagnose", json={"question": question}, stream=True, timeout=120) as r:
            r.raise_for_status()
            spinner = None
            for line in r.iter_lines():
                if not line:
                    continue
                ev = json.loads(line)
                if ev["type"] == "step_start":
                    spinner = timeline.empty()
                    spinner.info(f"⏳ {ev['label']}…")
                elif ev["type"] == "step_done":
                    if spinner:
                        spinner.empty()
                    with timeline:
                        st.markdown(f"✅ **{ev['label']}** — {ev['message']}")
                        render_data(ev)
                elif ev["type"] == "final":
                    render_final(ev)
                elif ev["type"] == "error":
                    st.error(ev["message"])
    except requests.RequestException as e:
        st.error(f"Backend injoignable ({BACKEND}) : {e}")
