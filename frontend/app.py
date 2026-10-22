import json
import os
from datetime import datetime

import pandas as pd
import requests
import streamlit as st

BACKEND = os.getenv("BACKEND_URL", "http://localhost:8000")

st.set_page_config(page_title="RootCause · Incident Console", page_icon="🛰️", layout="wide")
st.markdown("""
<style>
.block-container {padding-top: 3.5rem; max-width: 1400px;}
.brand {font-size: 1.5rem; font-weight: 700; letter-spacing: -0.02em;}
.brand span {color: #ff5a5f;}
.pill {display:inline-block; padding: 2px 10px; border-radius: 999px; font-size: .75rem; font-weight: 600;}
.pill.crit {background:#3b1219; color:#ff6b73; border:1px solid #7a1f2b;}
.pill.ok {background:#10281c; color:#4cd68a; border:1px solid #1d5a3a;}
.pill.live {background:#10281c; color:#4cd68a;}
.card {border:1px solid #2a2f3a; border-radius: 12px; padding: 14px 16px; margin-bottom: 10px; background:#12151c;}
.card.crit {border-color:#7a1f2b; background:linear-gradient(180deg,#1b1015,#12151c);}
.rca {border:1px solid #1d5a3a; border-radius:14px; padding:18px 20px; background:linear-gradient(180deg,#0f1f18,#12151c);}
.muted {color:#8b93a7; font-size:.85rem;}
</style>
""", unsafe_allow_html=True)

ss = st.session_state
ss.setdefault("history", [])  # diagnostics de la session
ss.setdefault("result", None)


def fetch_services():
    try:
        return requests.get(f"{BACKEND}/incidents", timeout=20).json()["services"], None
    except Exception as e:
        return [], str(e)


def stream(payload):
    with requests.post(f"{BACKEND}/diagnose", json=payload, stream=True, timeout=180) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if line:
                yield json.loads(line)


def postmortem_md(res) -> str:
    rc, m = res["root_cause"], res["metrics"]
    steps = "\n".join(f"- {s}" for s in rc.get("next_steps", []))
    ev = "\n".join(f"- {e}" for e in res["evidence"])
    return (f"# Post-mortem — {res['service']}\n\n**Début :** {rc['incident_start']}  \n"
            f"**Composant fautif :** `{rc['faulty_component']}`  \n**Impact :** {rc['business_impact']}\n\n"
            f"## Cause racine\n{rc['summary']}\n\n## Preuves\n{ev}\n\n## Actions suggérées\n{steps}\n\n"
            f"_Diagnostic en {m['diagnosis_seconds']} s (référence humaine : 45 min)._\n")


def investigate(service=None, question=None):
    payload = {"service": service} if service else {"question": question}
    ss["result"] = None
    box = st.container()
    spin = None
    with box:
        st.markdown("#### Investigation en cours")
        for ev in stream(payload):
            if ev["type"] == "step_start":
                spin = st.empty()
                spin.info(f"⏳ {ev['label']}…")
            elif ev["type"] == "step_done":
                if spin:
                    spin.empty()
                st.markdown(f"✅ **{ev['label']}** — {ev['message']}")
            elif ev["type"] == "final":
                ss["result"] = ev
                ss["history"].append({"service": ev["service"], "seconds": ev["metrics"]["diagnosis_seconds"],
                                      "at": datetime.now().strftime("%H:%M:%S")})
            elif ev["type"] == "error":
                st.error(ev["message"])
    st.rerun()


# ---------- En-tête ----------
h1, h2 = st.columns([4, 1])
h1.markdown('<div class="brand">🛰️ Root<span>Cause</span> <span class="muted">Incident Console</span></div>',
            unsafe_allow_html=True)
h2.markdown('<div style="text-align:right"><span class="pill live">● LIVE</span> '
            '<span class="muted">ClickHouse · OpenSearch · PostgreSQL</span></div>', unsafe_allow_html=True)

services, err = fetch_services()
if err:
    st.error(f"Backend injoignable ({BACKEND}) : {err}")
    st.stop()
open_inc = [s for s in services if s["status"] == "incident"]
avg = (sum(h["seconds"] for h in ss["history"]) / len(ss["history"])) if ss["history"] else None

k1, k2, k3, k4 = st.columns(4)
k1.metric("Services surveillés", len(services))
k2.metric("Incidents ouverts", len(open_inc), delta="action requise" if open_inc else "tout va bien",
          delta_color="inverse" if open_inc else "normal")
k3.metric("Diagnostic moyen", f"{avg:.1f} s" if avg else "—", "vs 45 min en manuel", delta_color="off")
k4.metric("MTTR économisé", f"{1 - avg / 2700:.1%}" if avg else "—")
st.divider()

left, right = st.columns([2, 3], gap="large")

# ---------- Gauche : incidents + santé ----------
with left:
    st.subheader("Incidents")
    if not open_inc:
        st.success("Aucun incident ouvert.")
    for s in open_inc:
        st.markdown(f'<div class="card crit"><span class="pill crit">CRITIQUE</span> &nbsp;<b>{s["service"]}</b>'
                    f'<div class="muted">Taux d\'erreur 5xx : <b>{s["recent_error_rate"]:.0%}</b> '
                    f'(normal : {s["baseline_error_rate"]:.0%}) · {s["recent_requests"]} requêtes / 10 min</div></div>',
                    unsafe_allow_html=True)
        if st.button("🔎 Investiguer", key=f"inv-{s['service']}", type="primary", use_container_width=True):
            investigate(service=s["service"])

    st.subheader("Santé des services")
    df = pd.DataFrame(services)
    df["statut"] = df["status"].map({"incident": "🔴 Incident", "healthy": "🟢 OK"})
    df["erreurs 5xx"] = df["recent_error_rate"].map("{:.1%}".format)
    st.dataframe(df[["service", "statut", "erreurs 5xx", "recent_requests"]].rename(
        columns={"recent_requests": "req / 10 min"}), hide_index=True, use_container_width=True)

    with st.expander("💬 Poser une question libre"):
        q = st.text_input("Question", value="Pourquoi le taux d'erreur a-t-il augmenté sur le service de paiement ?",
                          label_visibility="collapsed")
        if st.button("Diagnostiquer", use_container_width=True):
            investigate(question=q)

# ---------- Droite : rapport ----------
with right:
    res = ss["result"]
    if not res:
        st.markdown('<div class="card"><b>Aucune investigation sélectionnée</b>'
                    '<div class="muted">Choisissez un incident à gauche : l\'agent corrèle logs, traces et '
                    'données métier, puis produit la cause racine et un post-mortem.</div></div>',
                    unsafe_allow_html=True)
    else:
        rc, m = res["root_cause"], res["metrics"]
        st.markdown(f'<div class="rca"><span class="pill ok">CAUSE RACINE IDENTIFIÉE</span> '
                    f'<span class="muted">· {res["service"]} · en {m["diagnosis_seconds"]} s</span>'
                    f'<h3 style="margin:.6rem 0 .3rem">{rc["faulty_component"]}</h3>'
                    f'<div>{rc["summary"]}</div></div>', unsafe_allow_html=True)
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Temps de diagnostic", f"{m['diagnosis_seconds']} s")
        c2.metric("Réduction MTTR", f"{m['mttr_reduction']:.1%}")
        c3.metric("Preuves", m["evidence_count"])
        c4.metric("Précision", "n/a" if m["accuracy"] is None else f"{m['accuracy']:.0%}")

        t1, t2, t3 = st.tabs(["Preuves", "Actions suggérées", "Post-mortem"])
        with t1:
            d = res["data"]
            if d["error_rate"].get("minutes"):
                st.caption("Taux d'erreur 5xx par minute (ClickHouse)")
                st.line_chart(pd.DataFrame(d["error_rate"]["minutes"]).set_index("minute")[["error_rate"]])
            if d["slow_spans"].get("operations"):
                st.caption("Opérations lentes / en erreur (OpenSearch)")
                st.dataframe(pd.DataFrame(d["slow_spans"]["operations"]), hide_index=True, use_container_width=True)
            st.caption("Impact métier (PostgreSQL)")
            st.write(f"**{d['business_impact']['pending_orders']}** commandes bloquées, "
                     f"la plus ancienne depuis {d['business_impact'].get('oldest_pending', 'n/a')}")
            for e in res["evidence"]:
                st.markdown(f"- {e}")
        with t2:
            st.caption("Suggestions générées à partir des preuves — à valider par l'ingénieur.")
            for i, step in enumerate(rc.get("next_steps", []), 1):
                st.markdown(f"**{i}.** {step}")
        with t3:
            md = postmortem_md(res)
            st.markdown(md)
            st.download_button("⬇️ Télécharger le post-mortem (.md)", md, file_name=f"postmortem-{res['service']}.md")
