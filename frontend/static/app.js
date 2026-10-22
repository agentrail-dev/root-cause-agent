const $ = (s) => document.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const pct = (x, d = 0) => (x * 100).toFixed(d).replace(".", ",") + " %";
const HUMAN_S = 2700; // référence humaine : 45 min

const STEPS = [
  ["analyze_question", "Je cadre la question"], ["detect_anomaly", "Je cherche quand ça a démarré"],
  ["find_faulty_span", "Je cherche où ça casse"], ["check_business_impact", "Je mesure l'impact métier"],
  ["synthesize", "Je rédige la cause racine"],
];
const state = { services: [], details: {}, thread: [], run: null, history: [], alerts: [], wd: null, alertsErr: null,
  alertOpen: new Set(), fresh: new Map(), justResolved: new Map(), flash: new Map(), seenAlerts: null, alertResolved: new Map(), prevStatus: null, prevOpenN: null, bumpAt: 0, lastOk: null, svcFail: false, alFail: false, bench: { cases: null, casesErr: null, rows: {}, running: false, done: false, err: null, limit: "", llm: false } };
const sorted = () => [...state.services].sort((a, b) => (b.status === "incident") - (a.status === "incident"));
const openInc = () => state.services.filter((s) => s.status === "incident");
const bold = (t) => `<b>${esc(t)}</b>`;

function chart(er) {
  const mins = er.minutes, W = 640, H = 200, P = { l: 44, r: 14, t: 16, b: 26 };
  const max = Math.max(...mins.map((m) => m.error_rate), er.baseline_error_rate || 0, 0.01) * 1.2;
  const x = (i) => P.l + (i * (W - P.l - P.r)) / Math.max(mins.length - 1, 1);
  const y = (v) => P.t + (1 - v / max) * (H - P.t - P.b);
  const pts = mins.map((m, i) => [x(i), y(m.error_rate)]);
  const line = pts.map((p, i) => (i ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1)).join(" ");
  const area = `${line} L${pts.at(-1)[0]} ${H - P.b} L${pts[0][0]} ${H - P.b}Z`;
  const grid = [0, .5, 1].map((f) => `<line x1="${P.l}" x2="${W - P.r}" y1="${y(max * f)}" y2="${y(max * f)}" stroke="rgba(255,255,255,.06)"/><text x="${P.l - 8}" y="${y(max * f) + 4}" text-anchor="end">${pct(max * f)}</text>`).join("");
  const step = Math.ceil(mins.length / 6);
  const lab = mins.map((m, i) => (i % step === 0 ? `<text x="${x(i)}" y="${H - 6}" text-anchor="middle">${esc(m.minute.slice(11, 16))}</text>` : "")).join("");
  const base = er.baseline_error_rate != null ? `<line x1="${P.l}" x2="${W - P.r}" y1="${y(er.baseline_error_rate)}" y2="${y(er.baseline_error_rate)}" stroke="var(--ok)" stroke-dasharray="3 4" opacity=".6"/><text x="${W - P.r}" y="${y(er.baseline_error_rate) - 5}" text-anchor="end" style="fill:var(--ok)">normal ${pct(er.baseline_error_rate)}</text>` : "";
  const oi = mins.findIndex((m) => m.minute === er.onset_minute);
  const onset = oi >= 0 ? `<line x1="${x(oi)}" x2="${x(oi)}" y1="${P.t}" y2="${H - P.b}" stroke="var(--crit)" stroke-dasharray="3 4" opacity=".7"/><text x="${x(oi) - 6}" y="${P.t + 8}" text-anchor="end" style="fill:var(--crit)">début ${esc(er.onset_minute.slice(11, 16))}</text>` : "";
  return `<svg class="chart" viewBox="0 0 ${W} ${H}" width="100%" role="img" aria-label="Taux d'erreur par minute">
    <defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#ff5c63" stop-opacity=".35"/><stop offset="1" stop-color="#ff5c63" stop-opacity="0"/></linearGradient></defs>
    ${grid}${base}<path d="${area}" fill="url(#g)"/><path d="${line}" fill="none" stroke="var(--crit)" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
    ${pts.map((p) => `<circle cx="${p[0]}" cy="${p[1]}" r="3" fill="var(--panel)" stroke="var(--crit)" stroke-width="1.6"/>`).join("")}${onset}${lab}</svg>`;
}

function postmortem(res) {
  const rc = res.root_cause, m = res.metrics;
  return `# Post-mortem — ${res.service}\n\nDébut : ${rc.incident_start}\nComposant fautif : ${rc.faulty_component}\nImpact : ${rc.business_impact}\n\n## Cause racine\n${rc.summary}\n\n## Preuves\n${res.evidence.map((e) => "- " + e).join("\n")}\n\n## Actions suggérées\n${(rc.next_steps || []).map((s) => "- " + s).join("\n")}\n\nDiagnostic en ${m.diagnosis_seconds} s (référence humaine : 45 min).\n`;
}


function greeting() {
  const inc = openInc();
  if (!state.services.length) return "Je n'arrive pas à joindre les sources de données.";
  if (!inc.length) return `<p>Bonjour. Je surveille <b>${state.services.length} services</b> et tout est nominal. Posez-moi une question quand vous voulez.</p>`;
  const list = inc.map((s) => `<p><span class="code">${esc(s.service)}</span> : <span class="crit-t">${pct(s.recent_error_rate)}</span> d'erreurs 5xx${s.baseline_error_rate ? `, contre ${pct(s.baseline_error_rate)} d'habitude (×${ratio(s).toFixed(0)})` : vsNormal(s) ? ` (${vsNormal(s)})` : ""} sur ${esc(s.recent_requests)} requêtes récentes.</p>`).join("");
  return `<p>Bonjour. Je surveille <b>${state.services.length} services</b> et j'ai détecté <b>${inc.length} incident${inc.length > 1 ? "s" : ""} ouvert${inc.length > 1 ? "s" : ""}</b> :</p>${list}
    <p>Je peux corréler les logs, les traces et les commandes pour trouver la cause racine et vous proposer des actions. Je lance l'investigation ?</p>
    <div class="row">${inc.map((s) => `<button class="btn" data-inv="${esc(s.service)}">Investiguer ${esc(s.service)}</button>`).join("")}</div>`;
}

function answer(res) {
  const rc = res.root_cause, m = res.metrics, d = res.data;
  const ops = d.slow_spans?.operations || [], mins = d.error_rate?.minutes || [];
  const steps = rc.next_steps || [];
  return `
  <p>J'ai terminé l'investigation de <span class="code">${esc(res.service)}</span> en <b>${esc(m.diagnosis_seconds)} s</b> (environ 45 min à la main). Voici ce que j'ai trouvé.</p>
  <div class="finding"><div class="lbl">Cause racine</div><div class="comp">${esc(rc.faulty_component)}</div><p>${esc(rc.summary)}</p></div>
  <div class="kv"><div><span>Début de l'incident</span><b>${esc(rc.incident_start)}</b></div>
    <div><span>Impact métier</span><b>${esc(rc.business_impact)}</b></div>
    <div><span>Preuves recoupées</span><b>${esc(m.evidence_count)} sources${m.accuracy == null ? "" : " · précision " + pct(m.accuracy)}</b></div></div>
  <div class="sec"><div class="sec-h">Ce que je recommande</div>
    ${steps.map((s, i) => `<div class="act"><i>${i + 1}</i><div>${esc(s)}</div><button class="cp" data-copy="${esc(s)}">Copier</button></div>`).join("") || `<p>Je n'ai pas assez d'éléments pour proposer une action.</p>`}
    <div class="hint">Propositions déduites des preuves ci-dessous : à valider par l'ingénieur d'astreinte avant exécution.</div></div>
  <details class="ev"><summary>Voir les preuves</summary><div class="ev-b">
    ${mins.length ? `<div><h4>Taux d'erreur 5xx par minute <span>ClickHouse</span></h4>${chart(d.error_rate)}</div>` : ""}
    ${ops.length ? `<div><h4>Opérations fautives <span>OpenSearch</span></h4><table><tr><th>Opération</th><th class="n">Spans lents</th><th class="n">Erreurs</th><th class="n">p99</th></tr>
      ${ops.map((o) => `<tr><td class="mono ${o.operation === d.slow_spans.faulty_operation ? "hot" : ""}">${esc(o.operation)}</td><td class="n">${esc(o.slow_spans)}</td><td class="n">${esc(o.error_count)}</td><td class="n">${esc(o.p99_ms)} ms</td></tr>`).join("")}</table></div>` : ""}
    <div><h4>Impact métier <span>PostgreSQL</span></h4><p><b>${esc(d.business_impact.pending_orders)}</b> commandes bloquées, la plus ancienne depuis ${esc(d.business_impact.oldest_pending ?? "n/a")}.</p></div>
    <div><h4>Éléments relevés</h4><ul class="pl">${res.evidence.map((e) => `<li>${esc(e)}</li>`).join("")}</ul></div></div></details>
  <div class="row"><button class="btn ghost" data-pm="${esc(res.service)}">Télécharger le post-mortem (.md)</button>
    <button class="btn ghost" data-inv="${esc(res.service)}">Relancer l'analyse</button></div>`;
}

function runView(run) {
  const rows = STEPS.map(([id, label]) => {
    const s = run.steps[id];
    if (!s) return "";
    return `<div class="log-i ${s.done ? "done" : "run"}"><span class="ic">${s.done ? "✓" : ""}</span><div><b>${esc(label)}</b>${s.message ? " — " + esc(s.message) : "…"}</div></div>`;
  }).join("");
  return `<p>Je m'en occupe${run.service !== "…" ? ` pour <span class="code">${esc(run.service)}</span>` : ""}.</p><div class="log">${rows}</div>${run.error ? `<div class="err">${esc(run.error)}</div>` : ""}`;
}

function renderThread() {
  const t = $("#thread");
  const items = [`<div class="msg"><div class="avatar"></div><div class="body">${greeting()}</div></div>`];
  for (const m of state.thread) {
    if (m.type === "user") items.push(`<div class="msg user"><div class="bubble-u">${esc(m.text)}</div></div>`);
    else if (m.type === "run") items.push(`<div class="msg"><div class="avatar"></div><div class="body">${runView(m.run)}</div></div>`);
    else if (m.type === "result") items.push(`<div class="msg"><div class="avatar"></div><div class="body">${answer(m.res)}</div></div>`);
  }
  t.innerHTML = items.join("");
  const sc = $("#scroll"); sc.scrollTop = sc.scrollHeight;
  renderChips();
}

function renderChips() {
  const cur = route(), list = cur ? state.services.filter((x) => x.service === cur) : openInc();
  $("#chips").innerHTML = state.run ? "" : list.map((s) => `<button class="chip" data-inv="${esc(s.service)}"><i class="dot crit"></i>Investiguer ${esc(s.service)}</button>`).join("");
}

const hash = () => decodeURIComponent(location.hash.replace(/^#\/?/, ""));
const view = () => { const h = hash(); return h === "alerts" || h === "benchmark" ? h : h.startsWith("service/") ? "service" : "home"; };
const route = () => (hash().startsWith("service/") ? hash().slice(8) : "");
// Animations reprenables : on passe l'âge de l'événement pour que le re-rendu ne rejoue pas l'animation depuis le début.
const ageOf = (m, id, ttl) => { const t = m.get(id); if (!t) return null; const a = Date.now() - t; if (a > ttl) { m.delete(id); return null; } return a; };
const ageStyle = (a) => (a === null ? "" : ` style="--age:-${a}ms"`);
const utc = (t) => (t ? new Date(t.replace(" ", "T") + "Z") : null);
const dur = (ms) => { const s = Math.max(0, Math.round(ms / 1000)); return s < 60 ? s + " s" : s < 3600 ? Math.round(s / 60) + " min" : (s / 3600).toFixed(1).replace(".", ",") + " h"; };
const ago = (t) => (t ? "il y a " + dur(Date.now() - utc(t)) : "—");
function renderSide() {
  const cur = route();
  $("#svc-count").textContent = state.services.length || "";
  const v = view(), openN = state.alerts.filter((x) => !x.resolved_at).length;
  $("#nav-home").classList.toggle("sel", v === "home");
  $("#nav-alerts").classList.toggle("sel", v === "alerts");
  $("#nav-bench").classList.toggle("sel", v === "benchmark");
  if (state.prevOpenN !== null && openN > state.prevOpenN) state.bumpAt = Date.now();
  state.prevOpenN = openN;
  const bump = Date.now() - state.bumpAt < 700, ab = $("#alert-badge");
  ab.textContent = openN || ""; ab.hidden = !openN; ab.classList.toggle("bump", bump);
  ab.style.setProperty("--age", bump ? `-${Date.now() - state.bumpAt}ms` : "0s");
  $("#nav").innerHTML = sorted().map((s) => {
    const crit = s.status === "incident";
    const fl = ageOf(state.flash, s.service, 1500);
    return `<a class="svc ${s.service === cur ? "sel" : ""} ${fl !== null ? "flash" : ""}"${ageStyle(fl)} href="#/service/${encodeURIComponent(s.service)}"><i class="dot ${crit ? "crit" : ""}"></i><span class="svc-name">${esc(s.service)}</span>
      <span class="svc-rate ${crit ? "crit" : ""}">${pct(s.recent_error_rate, crit ? 0 : 1)}</span></a>`;
  }).join("");
  // Durées réelles : diagnostics automatiques du watchdog (/alerts) + diagnostics lancés à la main dans cet onglet.
  const h = [...state.alerts.map((x) => x.metrics?.diagnosis_seconds), ...state.history].filter((x) => typeof x === "number");
  const avg = h.length ? h.reduce((p, c) => p + c, 0) / h.length : null;
  $("#session").innerHTML = avg === null
    ? `<div class="side-t">Diagnostics (watchdog + manuels)</div><div class="side-empty">Aucun diagnostic pour l'instant : le watchdog diagnostique seul les nouveaux incidents.</div>`
    : `<div class="side-t">Diagnostics (watchdog + manuels)</div>
      <div class="mini"><span>Réalisés</span><b>${h.length}</b></div>
      <div class="mini"><span>Durée moyenne</span><b>${avg.toFixed(1).replace(".", ",")} s</b></div>
      <div class="mini" title="Calculé sur les durées réelles, par rapport à une hypothèse de 45 min en manuel (2700 s)"><span>MTTR économisé</span><b>${pct(1 - avg / HUMAN_S, 1)}</b></div>
      <div class="side-empty">vs 45 min en manuel (hypothèse)</div>`;
}

/* ---------- dashboard ---------- */
const ratio = (s) => (s.baseline_error_rate ? s.recent_error_rate / s.baseline_error_rate : null);
// En mode live le niveau normal vaut 0 : on affiche le score de dégradation de l'API plutôt qu'un multiple.
const vsNormal = (s) => (ratio(s) ? "×" + ratio(s).toFixed(0) + " par rapport au normal" : s.score != null ? "score de dégradation " + s.score : "");
const lastResult = (svc) => state.thread.filter((m) => m.type === "result" && m.res.service === svc).map((m) => m.res).pop();

function spark(det, crit) {
  const m = det?.error_rate?.minutes;
  if (!m || m.length < 2) return "";
  const W = 140, H = 40, max = Math.max(...m.map((p) => p.error_rate), 0.02);
  const pts = m.map((p, i) => [(i * W) / (m.length - 1), H - 3 - (p.error_rate / max) * (H - 8)]);
  const line = pts.map((p, i) => (i ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1)).join(" ");
  const c = crit ? "var(--crit)" : "var(--ok)", id = "sp" + Math.random().toString(36).slice(2, 7);
  return `<svg class="spark" viewBox="0 0 ${W} ${H}"><defs><linearGradient id="${id}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="${c}" stop-opacity=".3"/><stop offset="1" stop-color="${c}" stop-opacity="0"/></linearGradient></defs>
    <path d="${line} L${W} ${H} L0 ${H}Z" fill="url(#${id})"/><path d="${line}" fill="none" stroke="${c}" stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round"/></svg>`;
}

function journey() {
  const w = state.wd, n = state.alerts.length;
  const st = (t, d, link) => `<div class="jstep"><b>${t}</b><span>${d}</span>${link || ""}</div>`;
  return `<div class="card"><div class="card-h"><h2>Comment ça marche</h2><span>De la panne à la solution, sans intervention</span></div><div class="journey">
    ${st("1. Surveiller", `${state.services.length} services suivis en continu (logs, traces, commandes).`)}
    ${st("2. Détecter", w ? (w.running ? `Watchdog actif, dernier cycle ${ago(w.last_tick)}.` : "Watchdog inactif : détection manuelle.") : "Watchdog indisponible.", `<a class="link" href="#/alerts">Voir les alertes →</a>`)}
    ${st("3. Diagnostiquer", n ? `${n} diagnostic${n > 1 ? "s" : ""} automatique${n > 1 ? "s" : ""} réalisé${n > 1 ? "s" : ""}, cause racine incluse.` : "L'agent trouve la cause racine en quelques secondes.", `<a class="link" href="#/benchmark">Mesurer sa précision →</a>`)}
    ${st("4. Recommander", "Chaque alerte propose des actions concrètes, à valider par l'ingénieur.")}
  </div></div>`;
}

function overview() {
  const sv = sorted(), inc = openInc();
  const reqs = sv.reduce((a, s) => a + s.recent_requests, 0);
  const errs = sv.reduce((a, s) => a + s.recent_requests * s.recent_error_rate, 0);
  const g = reqs ? errs / reqs : 0;
  return `<div class="page">
    <div class="page-h"><div><h1>Vue d'ensemble</h1><p>Santé de la production, mise à jour automatiquement.</p></div></div>
    ${inc.map((s) => `<div class="banner"><i class="dot crit"></i><div><b>${esc(s.service)}</b> est en incident : ${pct(s.recent_error_rate)} d'erreurs 5xx${vsNormal(s) ? ", " + vsNormal(s) : ""}.</div>
      <button class="btn" data-inv="${esc(s.service)}">Investiguer avec l'agent</button></div>`).join("")}
    ${journey()}
    <div class="kpis">
      <div><div class="k">Services surveillés</div><div class="v">${sv.length}</div><div class="sub">${sv.length - inc.length} sains</div></div>
      <div><div class="k">Incidents ouverts</div><div class="v ${inc.length ? "crit" : "ok"}">${inc.length}</div><div class="sub">${inc.length ? "Action requise" : "Tout va bien"}</div></div>
      <div><div class="k">Erreurs 5xx globales</div><div class="v ${g > 0.05 ? "crit" : ""}">${pct(g, 1)}</div><div class="sub">pondéré par le trafic</div></div>
      <div><div class="k">Requêtes</div><div class="v">${reqs}<small>récentes</small></div><div class="sub">tous services</div></div>
    </div>
    <div class="sec-t"><h2>Services</h2><span>Cliquez sur un service pour le détail</span></div>
    <div class="cards">${sv.map((s) => {
      const crit = s.status === "incident", r = ratio(s);
      const fl = ageOf(state.flash, s.service, 1500);
      return `<a class="scard ${crit ? "crit" : ""} ${fl !== null ? "flash" : ""}"${ageStyle(fl)} href="#/service/${encodeURIComponent(s.service)}">
        <div class="scard-h"><b>${esc(s.service)}</b><span class="badge ${crit ? "crit" : "ok"}">${crit ? "Incident" : "Sain"}</span></div>
        <div class="scard-m"><div><div class="big ${crit ? "crit" : ""}">${pct(s.recent_error_rate, crit ? 0 : 1)}</div>
          <div class="sub">erreurs 5xx${s.baseline_error_rate ? " · normal " + pct(s.baseline_error_rate, 1) : ""}</div></div>${spark(state.details[s.service], crit)}</div>
        <div class="scard-f"><span>${esc(s.recent_requests)} requêtes récentes${crit && vsNormal(s) ? " · " + vsNormal(s).replace("par rapport au normal", "vs normal") : ""}</span>
          <span class="link">Détail →</span></div></a>`;
    }).join("")}</div></div>`;
}

function servicePage(name) {
  const s = state.services.find((x) => x.service === name);
  if (!s) return `<div class="page"><div class="empty">Service inconnu : ${esc(name)}. <a class="link" href="#/">Retour</a></div></div>`;
  const det = state.details[name], crit = s.status === "incident", r = ratio(s), last = lastResult(name);
  const er = det?.error_rate, ops = det?.slow_spans?.operations || [], bi = det?.business_impact;
  return `<div class="page">
    <div class="page-h"><div><h1 class="mono">${esc(name)}</h1><p>${crit ? "Incident en cours : le taux d'erreur dépasse largement le niveau normal." : "Aucune anomalie détectée."}</p></div>
      <div class="row"><span class="badge ${crit ? "crit" : "ok"}">${crit ? "Incident" : "Sain"}</span>
      <button class="btn ${crit ? "" : "ghost"}" data-inv="${esc(name)}">${last ? "Relancer l'agent" : "Investiguer avec l'agent"}</button></div></div>
    <div class="kpis">
      <div><div class="k">Erreurs 5xx</div><div class="v ${crit ? "crit" : "ok"}">${pct(s.recent_error_rate, crit ? 0 : 1)}</div></div>
      <div><div class="k">Niveau normal</div><div class="v">${s.baseline_error_rate ? pct(s.baseline_error_rate, 1) : "—"}</div></div>
      <div><div class="k">Requêtes</div><div class="v">${esc(s.recent_requests)}<small>récentes</small></div></div>
      <div><div class="k">Écart au normal</div><div class="v ${crit ? "crit" : ""}">${r ? "×" + r.toFixed(1).replace(".", ",") : "—"}</div></div></div>
    ${last ? `<div class="card"><div class="card-h"><h2>Dernier diagnostic de l'agent</h2><span>${esc(last.metrics.diagnosis_seconds)} s</span></div><div class="card-b diag">
      <div class="comp">${esc(last.root_cause.faulty_component)}</div><p style="color:var(--soft)">${esc(last.root_cause.summary)}</p>
      <ol class="steps2">${(last.root_cause.next_steps || []).map((x) => `<li>${esc(x)}</li>`).join("")}</ol></div></div>` : ""}
    <div class="two">
      <div class="card"><div class="card-h"><h2>Taux d'erreur 5xx par minute</h2><span>ClickHouse</span></div><div class="card-b">${er?.minutes?.length ? chart(er) : `<div class="empty">${det ? "Pas de données." : "Chargement…"}</div>`}</div></div>
      <div class="stack" style="display:grid;gap:16px">
        <div class="card"><div class="card-h"><h2>Opérations lentes</h2><span>OpenSearch</span></div><div class="card-b">${ops.length ? `<table><tr><th>Opération</th><th class="n">Erreurs</th><th class="n">p99</th></tr>${ops.map((o) => `<tr><td class="mono ${o.operation === det.slow_spans.faulty_operation ? "hot" : ""}">${esc(o.operation)}</td><td class="n">${esc(o.error_count)}</td><td class="n">${esc(o.p99_ms)} ms</td></tr>`).join("")}</table>` : `<div class="empty">Aucune opération lente.</div>`}</div></div>
        ${crit ? `<div class="card"><div class="card-h"><h2>Impact métier</h2><span>PostgreSQL</span></div><div class="card-b">${bi && bi.pending_orders != null ? `<div class="v">${esc(bi.pending_orders)}</div><div class="sub">commandes bloquées depuis ${esc(bi.oldest_pending ?? "n/a")}</div>` : `<div class="empty">Indisponible.</div>`}</div></div>` : ""}
      </div></div></div>`;
}

function renderDash() {
  const v = view(), name = route();
  const crumb = { alerts: "Alertes", benchmark: "Benchmark RCAEval", home: "Vue d'ensemble" };
  $("#crumbs").innerHTML = v === "service" ? `<a href="#/">Vue d'ensemble</a><span>/</span><b>${esc(name)}</b>` : `<b>${crumb[v]}</b>`;
  const el = $("#dash"), top = el.scrollTop;
  el.innerHTML = v === "alerts" ? alertsPage() : v === "benchmark" ? benchPage()
    : v === "service" ? (state.services.length ? servicePage(name) : `<div class="page"><div class="empty">Chargement…</div></div>`) : overview();
  el.scrollTop = top;
  // animation d'entrée seulement au changement de page, pas à chaque rafraîchissement automatique
  if (state.lastView !== hash()) { state.lastView = hash(); el.firstElementChild?.classList.add("enter"); el.scrollTop = 0; }
}


/* ---------- alertes (watchdog) ---------- */
function watchdogCard() {
  const w = state.wd;
  if (!w) return `<div class="card"><div class="card-b empty">${state.alertsErr ? "Watchdog injoignable : " + esc(state.alertsErr) : "Chargement…"}</div></div>`;
  return `<div class="card wd"><div class="wd-main"><span class="badge ${w.running ? "ok" : "off"}">${w.running ? "Watchdog actif" : "Watchdog inactif"}</span>
    <div class="wd-t">${w.running ? "L'agent surveille vos services et diagnostique seul chaque nouvel incident." : "La surveillance automatique est désactivée (variable <span class='code'>WATCHDOG=true</span>). Vous pouvez quand même forcer un cycle."}</div></div>
    <div class="wd-stats"><div><span>Dernier cycle</span><b>${w.last_tick ? ago(w.last_tick) : "aucun"}</b></div>
      <div><span>Intervalle</span><b>${w.interval ? w.interval + " s" : "—"}</b></div>
      <div><span>Cycles</span><b>${esc(w.ticks)}</b></div>
      <div><span>Incidents ouverts</span><b>${w.open_incidents.length}</b></div>
      <div><span>Notification</span><b>${w.webhook_configured ? "Webhook actif" : "Non configurée"}</b></div></div>
    <div class="wd-act"><button class="btn" id="tick" ${state.ticking ? "disabled" : ""}>${state.ticking ? "Cycle en cours…" : "Forcer un cycle"}</button>
      ${state.tickMsg ? `<span class="hint">${esc(state.tickMsg)}</span>` : ""}
      ${w.last_error ? `<span class="err-t">Dernière erreur : ${esc(w.last_error)}</span>` : ""}</div></div>`;
}

function alertCard(a) {
  const open = !a.resolved_at, rc = a.root_cause, m = a.metrics, steps = rc.next_steps || [];
  const isOpen = state.alertOpen.has(a.id) || (open && !state.alertSeen?.has(a.id));
  const when = open ? `ouverte ${ago(a.opened_at)}` : `résolue après ${dur(utc(a.resolved_at) - utc(a.opened_at))}`;
  const fa = ageOf(state.fresh, a.id, 6000), ra = ageOf(state.justResolved, a.id, 3000);
  return `<article class="alert ${open ? "open" : ""} ${fa !== null ? "fresh" : ""} ${ra !== null ? "resolved-now" : ""}"${ageStyle(fa ?? ra)}>
    <div class="alert-h"><span class="badge ${open ? "crit" : "ok"}">${open ? "Ouvert" : "Résolu"}</span>${fa !== null ? `<span class="new-tag">Nouveau</span>` : ""}
      <a class="alert-svc" href="#/service/${encodeURIComponent(a.service)}">${esc(a.service)}</a>
      <span class="hint">${when}${a.error_rate > 0 ? " · " + pct(a.error_rate) + " d'erreurs 5xx" : ""}</span>
      ${a.notified ? `<span class="chip-s">Notification envoyée</span>` : ""}</div>
    <p class="alert-sum">${esc(rc.summary)}</p>
    <div class="alert-imp"><span>Impact</span>${esc(rc.business_impact)}</div>
    <details data-alert="${esc(a.id)}" ${isOpen ? "open" : ""}><summary>Pourquoi l'agent conclut ça, et que faire</summary>
      <div class="alert-d">
        <div><div class="sec-h">Ce qui s'est passé</div><p>L'incident a commencé à <b>${esc(rc.incident_start)}</b> sur <span class="code">${esc(rc.faulty_component)}</span>.</p></div>
        <div><div class="sec-h">Preuves relevées</div><ul class="pl">${(a.evidence || []).map((e) => `<li>${esc(e)}</li>`).join("")}</ul></div>
        <div><div class="sec-h">Actions suggérées</div>${steps.map((x, i) => `<div class="act"><i>${i + 1}</i><div>${esc(x)}</div><button class="cp" data-copy="${esc(x)}">Copier</button></div>`).join("") || `<div class="empty">Aucune action proposée.</div>`}
          <div class="hint" style="margin-top:8px">À valider par l'ingénieur d'astreinte avant exécution.</div></div>
        <div class="hint">Diagnostic automatique en ${esc(m.diagnosis_seconds)} s (vs 45 min à la main) · ${esc(m.evidence_count)} preuves recoupées</div>
      </div></details></article>`;
}

function alertsPage() {
  const list = state.alerts;
  return `<div class="page"><div class="page-h"><div><h1>Alertes</h1><p>Incidents détectés et diagnostiqués automatiquement par le watchdog, du plus récent au plus ancien.</p></div></div>
    ${watchdogCard()}
    <div class="sec-t"><h2>Fil d'alertes</h2><span>${list.length} alerte${list.length > 1 ? "s" : ""}${list.length ? " dont " + list.filter((a) => !a.resolved_at).length + " ouverte" + (list.filter((a) => !a.resolved_at).length > 1 ? "s" : "") : ""} · actualisé automatiquement</span></div>
    ${list.length ? list.map(alertCard).join("") : `<div class="idle"><div><h3>Aucune alerte pour l'instant</h3><p>${state.wd?.running ? "Tout est calme : le watchdog n'a détecté aucun incident." : "Activez le watchdog, ou forcez un cycle pour lancer une détection maintenant."}</p></div></div>`}</div>`;
}

/* ---------- benchmark RCAEval ---------- */
const median = (a) => { const s = [...a].sort((x, y) => x - y), n = s.length; return n ? (n % 2 ? s[(n - 1) / 2] : (s[n / 2 - 1] + s[n / 2]) / 2) : null; };

function benchPage() {
  const b = state.bench, cases = b.cases || [];
  const rows = Object.values(b.rows), valid = rows.filter((r) => !r.error), good = valid.filter((r) => r.ok);
  const acc = valid.length ? good.length / valid.length : null;
  const onsets = valid.map((r) => r.onset_error_seconds).filter((x) => typeof x === "number");
  const secs = valid.map((r) => r.seconds).filter((x) => typeof x === "number");
  const med = median(onsets), avg = secs.length ? secs.reduce((p, c) => p + c, 0) / secs.length : null;
  const total = (+b.limit || 0) ? Math.min(+b.limit, cases.length) : cases.length;
  const intro = `<div class="card"><div class="card-b intro"><div><b>1. Un incident réel</b><span>Chaque cas RCAEval est une panne injectée dans une vraie application, dont on connaît la cause.</span></div>
    <div><b>2. L'agent enquête</b><span>Il analyse logs et traces sans voir la réponse, puis désigne le service fautif.</span></div>
    <div><b>3. On compare</b><span>Son verdict est confronté à la vérité terrain : précision et écart sur l'heure de début.</span></div></div></div>`;
  if (b.casesErr) return `<div class="page"><div class="page-h"><div><h1>Benchmark RCAEval</h1></div></div>${intro}<div class="err">Impossible de charger les cas : ${esc(b.casesErr)}. Chargez le jeu RCAEval (script de chargement) puis rechargez la page.</div></div>`;
  const body = cases.map((c) => {
    const r = b.rows[c.case_id];
    const res = !r ? (b.running ? `<span class="hint">en attente…</span>` : "—") : r.error ? `<span class="badge crit" title="${esc(r.error)}">Erreur</span>` : r.ok ? `<span class="badge ok">Trouvé</span>` : `<span class="badge crit">Raté</span>`;
    return `<tr><td class="mono">${esc(c.case_id)}</td><td>${esc(c.fault_type)}</td><td class="mono">${esc(c.root_cause_service)}</td>
      <td class="mono ${r && !r.error && !r.ok ? "hot" : ""}">${r && !r.error ? esc(r.found) : "—"}</td><td>${res}</td>
      <td class="n">${r && typeof r.onset_error_seconds === "number" ? esc(r.onset_error_seconds) + " s" : "—"}</td>
      <td class="n">${r && typeof r.seconds === "number" ? esc(r.seconds) + " s" : "—"}</td></tr>`;
  }).join("");
  return `<div class="page"><div class="page-h"><div><h1>Benchmark RCAEval</h1><p>Mesure objective de la qualité des diagnostics de l'agent sur des pannes connues.</p></div></div>${intro}
    <div class="card"><div class="card-b bench-ctl"><label>Nombre de cas <input type="number" min="1" max="${cases.length || 1}" placeholder="tous (${cases.length})" value="${esc(b.limit)}" data-bench="limit" ${b.running ? "disabled" : ""}></label>
      <label class="chk"><input type="checkbox" data-bench="llm" ${b.llm ? "checked" : ""} ${b.running ? "disabled" : ""}> Avec le LLM</label>
      <button class="btn" id="run-bench" ${b.running || !cases.length ? "disabled" : ""}>${b.running ? "Benchmark en cours…" : rows.length ? "Relancer le benchmark" : "Lancer le benchmark"}</button></div>
      ${b.running ? `<div class="progress"><i style="width:${total ? (rows.length / total) * 100 : 0}%"></i></div>` : ""}</div>
    ${b.err ? `<div class="err">${esc(b.err)}</div>` : ""}
    <div class="kpis"><div><div class="k">Précision globale</div><div class="v xl ${acc == null ? "" : acc >= 0.7 ? "ok" : acc >= 0.4 ? "warn" : "crit"}">${acc == null ? "—" : pct(acc)}</div><div class="sub">${valid.length ? good.length + " cas trouvés sur " + valid.length : "Lancez le benchmark"}</div></div>
      <div><div class="k">Écart de début (médian)</div><div class="v">${med == null ? "—" : esc(med) + "<small>s</small>"}</div><div class="sub">entre l'heure trouvée et la vraie</div></div>
      <div><div class="k">Durée moyenne</div><div class="v">${avg == null ? "—" : avg.toFixed(1).replace(".", ",") + "<small>s</small>"}</div><div class="sub">par diagnostic</div></div>
      <div><div class="k">Cas évalués</div><div class="v">${valid.length}<small>/ ${total || cases.length}</small></div><div class="sub">${rows.length - valid.length ? rows.length - valid.length + " en erreur" : "aucune erreur"}</div></div></div>
    <div class="card"><div class="card-h"><h2>Résultats par cas</h2><span>${cases.length} cas chargés</span></div><div class="card-b" style="overflow:auto">${cases.length ? `<table>
      <tr><th>Cas</th><th>Type de panne</th><th>Vérité terrain</th><th>Trouvé</th><th>Résultat</th><th class="n">Écart début</th><th class="n">Durée</th></tr>${body}</table>` : `<div class="empty">${b.cases ? "Aucun cas chargé." : "Chargement…"}</div>`}</div></div></div>`;
}

async function loadCases() {
  const b = state.bench;
  try { const r = await fetch("/cases"); if (!r.ok) throw new Error("HTTP " + r.status); b.cases = (await r.json()).cases; b.casesErr = null; }
  catch (e) { b.casesErr = e.message; }
  if (view() === "benchmark") renderDash();
}

async function runBenchmark() {
  const b = state.bench; if (b.running) return;
  Object.assign(b, { running: true, rows: {}, err: null }); renderDash();
  try {
    const r = await fetch("/benchmark", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ limit: +b.limit || 0, use_llm: b.llm }) });
    if (!r.ok) throw new Error("HTTP " + r.status);
    const reader = r.body.getReader(), dec = new TextDecoder(); let buf = "";
    const handle = (ev) => { if (ev.type === "case") { b.rows[ev.case_id] = ev; renderDash(); } };
    for (;;) {
      const { done, value } = await reader.read(); if (done) break;
      buf += dec.decode(value, { stream: true });
      const lines = buf.split("\n"); buf = lines.pop(); lines.filter(Boolean).forEach((l) => handle(JSON.parse(l)));
    }
    if (buf.trim()) handle(JSON.parse(buf));
  } catch (e) { b.err = "Benchmark interrompu : " + e.message; }
  b.running = false; renderDash();
}

/* ---------- alertes : chargement ---------- */
async function loadAlerts() {
  try {
    const [a, w] = await Promise.all([fetch("/alerts"), fetch("/watchdog/status")]);
    if (!a.ok || !w.ok) throw new Error("HTTP " + (a.ok ? w.status : a.status));
    state.alerts = (await a.json()).alerts; state.wd = await w.json(); state.alertsErr = null;
    if (state.seenAlerts) state.alerts.forEach((x) => {  // rien d'animé au premier chargement
      if (!state.seenAlerts.has(x.id)) state.fresh.set(x.id, Date.now());
      if (state.alertResolved.get(x.id) === false && x.resolved_at) state.justResolved.set(x.id, Date.now());
    });
    state.seenAlerts = new Set(state.alerts.map((x) => x.id));
    state.alertResolved = new Map(state.alerts.map((x) => [x.id, !!x.resolved_at]));
    state.alFail = false; liveOk();
  } catch (e) { state.alertsErr = e.message; state.alFail = true; renderLive(); }
  renderSide();
  if (["alerts", "home"].includes(view())) renderDash();
}

async function forceTick() {
  state.ticking = true; state.tickMsg = ""; renderDash();
  try {
    const r = await fetch("/watchdog/tick", { method: "POST" });
    if (!r.ok) throw new Error("HTTP " + r.status);
    const n = (await r.json()).created.length;
    state.tickMsg = n ? `${n} nouvelle${n > 1 ? "s" : ""} alerte${n > 1 ? "s" : ""} créée${n > 1 ? "s" : ""}.` : "Cycle terminé : aucun nouvel incident.";
  } catch (e) { state.tickMsg = "Échec du cycle : " + e.message; }
  state.ticking = false; await loadAlerts(); loadServices();
}

async function loadDetail(name) {
  try { const r = await fetch("/services/" + encodeURIComponent(name)); if (r.ok) state.details[name] = await r.json(); } catch {}
}

function liveOk() { state.lastOk = Date.now(); renderLive(); const c = $("#conn"); c.classList.add("tick"); setTimeout(() => c.classList.remove("tick"), 250); }
function renderLive() {
  const c = $("#conn"), off = state.svcFail || state.alFail, age = state.lastOk ? Math.round((Date.now() - state.lastOk) / 1000) : null;
  c.className = "live " + (off ? "ko" : state.lastOk ? "ok" : "") + (c.classList.contains("tick") ? " tick" : "");
  c.lastElementChild.textContent = off ? `Hors ligne${age !== null ? " · dernière mise à jour il y a " + age + " s" : ""}` : age === null ? "Connexion…" : `En direct · mis à jour il y a ${age} s`;
}
setInterval(renderLive, 1000);

async function loadServices() {
  try {
    const r = await fetch("/incidents");
    if (!r.ok) throw new Error(r.status);
    state.services = (await r.json()).services;
    const prev = state.prevStatus;
    state.prevStatus = Object.fromEntries(state.services.map((x) => [x.service, x.status]));
    if (prev) state.services.forEach((x) => { if (prev[x.service] && prev[x.service] !== x.status) state.flash.set(x.service, Date.now()); });
    state.svcFail = false; liveOk();
  } catch { state.svcFail = true; renderLive(); }
  renderSide(); renderDash();
  await Promise.all(state.services.map((x) => loadDetail(x.service)));
  renderDash();
  if (!state.run && !state.thread.length) renderThread(); // l'accueil suit l'état des services
}

async function investigate(payload, label) {
  if (state.run) return;
  const run = { service: payload.service || "…", steps: {} };
  state.run = run;
  setAgent(true);
  state.thread.push({ type: "user", text: label }, { type: "run", run });
  renderThread();
  try {
    const r = await fetch("/diagnose", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    if (!r.ok) throw new Error("HTTP " + r.status);
    const reader = r.body.getReader(), dec = new TextDecoder();
    let buf = "", final = null;
    const handle = (ev) => {
      if (ev.type === "step_start") run.steps[ev.step] = { running: true };
      else if (ev.type === "step_done") run.steps[ev.step] = { done: true, message: ev.message };
      else if (ev.type === "final") final = ev;
      else if (ev.type === "error") throw new Error(ev.message);
      if (ev.data?.service_name) run.service = ev.data.service_name;
      renderThread();
    };
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const lines = buf.split("\n"); buf = lines.pop();
      lines.filter(Boolean).forEach((l) => handle(JSON.parse(l)));
    }
    if (buf.trim()) handle(JSON.parse(buf));
    if (!final) throw new Error("Flux interrompu sans résultat");
    state.thread.push({ type: "result", res: final });
    state.history.push(final.metrics.diagnosis_seconds);
  } catch (e) { run.error = "Je n'ai pas pu terminer : " + e.message; }
  state.run = null;
  renderSide(); renderDash(); renderThread();
}

const results = () => state.thread.filter((m) => m.type === "result").map((m) => m.res);
document.addEventListener("toggle", (e) => {
  const id = e.target?.dataset?.alert; if (!id) return;
  (state.alertSeen ||= new Set()).add(id);
  e.target.open ? state.alertOpen.add(id) : state.alertOpen.delete(id);
}, true);
document.addEventListener("input", (e) => {
  const k = e.target?.dataset?.bench; if (!k) return;
  state.bench[k] = e.target.type === "checkbox" ? e.target.checked : e.target.value;
});
document.addEventListener("click", (e) => {
  if (e.target.closest("#tick")) return forceTick();
  if (e.target.closest("#run-bench")) return runBenchmark();
  const inv = e.target.closest("[data-inv]");
  if (inv) return investigate({ service: inv.dataset.inv }, `Investigue ${inv.dataset.inv}`);
  const cp = e.target.closest("[data-copy]");
  if (cp) { navigator.clipboard?.writeText(cp.dataset.copy); cp.textContent = "Copié"; setTimeout(() => (cp.textContent = "Copier"), 1500); return; }
  const pm = e.target.closest("[data-pm]");
  if (pm) {
    const res = results().filter((r) => r.service === pm.dataset.pm).pop();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([postmortem(res)], { type: "text/markdown" }));
    a.download = `postmortem-${res.service}.md`; a.click(); URL.revokeObjectURL(a.href);
  }
});
$("#ask").addEventListener("submit", (e) => {
  e.preventDefault();
  const q = $("#question").value.trim();
  if (q) { investigate({ question: q }, q); $("#question").value = ""; }
});

function setAgent(open) {
  $(".app").classList.toggle("agent-off", !open);
  $("#toggle-agent").setAttribute("aria-pressed", open);
  if (innerWidth > 1280) { try { localStorage.setItem("agent-open", open ? "1" : "0"); } catch {} }
}
let saved = null; try { saved = localStorage.getItem("agent-open"); } catch {}
setAgent(innerWidth > 1280 ? saved !== "0" : false);
$("#toggle-agent").onclick = () => setAgent($(".app").classList.contains("agent-off"));
$("#close-agent").onclick = () => setAgent(false);
document.addEventListener("keydown", (e) => { if ((e.metaKey || e.ctrlKey) && e.key === "j") { e.preventDefault(); $("#toggle-agent").click(); } });
addEventListener("hashchange", () => {
  renderSide(); renderDash(); renderChips();
  const n = route(); if (n) loadDetail(n).then(renderDash);
  if (view() === "benchmark" && !state.bench.cases) loadCases();
  if (view() === "alerts") loadAlerts();
});
renderThread();
loadServices();
loadAlerts();
if (view() === "benchmark") loadCases();
setInterval(loadAlerts, 8000);
setInterval(() => { if (!state.run) loadServices(); }, 30000);
