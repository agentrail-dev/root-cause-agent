const $ = (s) => document.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const pct = (x, d = 0) => (x * 100).toFixed(d).replace(".", ",") + " %";
const HUMAN_S = 2700; // référence humaine : 45 min

const STEPS = [
  ["analyze_question", "Je cadre la question"], ["detect_anomaly", "Je cherche quand ça a démarré"],
  ["find_faulty_span", "Je cherche où ça casse"], ["check_business_impact", "Je mesure l'impact métier"],
  ["synthesize", "Je rédige la cause racine"],
];
const state = { services: [], thread: [], run: null, history: [], greeted: false };
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
  const list = inc.map((s) => `<p><span class="code">${esc(s.service)}</span> : <span class="crit-t">${pct(s.recent_error_rate)}</span> d'erreurs 5xx, contre ${pct(s.baseline_error_rate)} d'habitude (×${(s.recent_error_rate / (s.baseline_error_rate || .01)).toFixed(0)}) sur ${esc(s.recent_requests)} requêtes en 10 min.</p>`).join("");
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
  $("#chips").innerHTML = state.run ? "" : openInc().map((s) => `<button class="chip" data-inv="${esc(s.service)}"><i class="dot crit"></i>Investiguer ${esc(s.service)}</button>`).join("");
}

function renderSide() {
  $("#svc-count").textContent = state.services.length || "";
  $("#nav").innerHTML = sorted().map((s) => {
    const crit = s.status === "incident";
    return `<button class="svc" data-inv="${esc(s.service)}" title="Investiguer ${esc(s.service)}"><i class="dot ${crit ? "crit" : ""}"></i><span class="svc-name">${esc(s.service)}</span>
      <span class="svc-rate ${crit ? "crit" : ""}">${pct(s.recent_error_rate, crit ? 0 : 1)}</span></button>`;
  }).join("");
  const h = state.history, avg = h.length ? h.reduce((a, b) => a + b, 0) / h.length : null;
  $("#session").innerHTML = `<div class="mini"><span>Diagnostics</span><b>${h.length}</b></div>
    <div class="mini"><span>Durée moyenne</span><b>${avg !== null ? avg.toFixed(1) + " s" : "—"}</b></div>
    <div class="mini"><span>MTTR économisé</span><b>${avg !== null ? pct(1 - avg / HUMAN_S, 1) : "—"}</b></div>`;
}

async function loadServices() {
  const conn = $("#conn");
  try {
    const r = await fetch("/incidents");
    if (!r.ok) throw new Error(r.status);
    state.services = (await r.json()).services;
    conn.className = "live ok"; conn.lastElementChild.textContent = "Données en direct";
  } catch { conn.className = "live ko"; conn.lastElementChild.textContent = "Backend injoignable"; }
  renderSide();
  if (!state.run && !state.thread.length) renderThread(); // l'accueil suit l'état des services
}

async function investigate(payload, label) {
  if (state.run) return;
  const run = { service: payload.service || "…", steps: {} };
  state.run = run;
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
  renderSide(); renderThread();
}

const results = () => state.thread.filter((m) => m.type === "result").map((m) => m.res);
document.addEventListener("click", (e) => {
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

renderThread();
loadServices();
setInterval(() => { if (!state.run) loadServices(); }, 30000);
