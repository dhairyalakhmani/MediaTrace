// Lemnisca Media KB: single-page interface over /api. No build step, no external libraries.
"use strict";

const $ = (sel, el = document) => el.querySelector(sel);
const view = $("#view");
const TYPES = ["codependency", "concentration", "shared_transporter", "solubility", "stability", "transporter", "uptake_rate"];
const TYPE_LABEL = { codependency: "Co-dependency", concentration: "Concentration", shared_transporter: "Shared transporter",
  solubility: "Solubility", stability: "Stability", transporter: "Transporter", uptake_rate: "Uptake rate" };

// ---------- helpers ----------
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const enc = (doi) => encodeURIComponent(doi);
async function api(path, opts = {}) {
  const r = await fetch("/api" + path, opts);
  const body = r.headers.get("content-type")?.includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new Error(body?.detail || body || r.statusText);
  return body;
}
function toast(msg, ms = 3500) {
  const t = $("#toast"); t.textContent = msg; t.classList.add("show");
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove("show"), ms);
}
function openPanel(title, html) { $("#panel-title").textContent = title; $("#panel-body").innerHTML = html; $("#panel").classList.add("open"); }
function closePanel() { $("#panel").classList.remove("open"); }
document.addEventListener("keydown", (e) => e.key === "Escape" && closePanel());

const STATUS = {
  downloaded: ["Downloaded", "b-ok"], pending: ["Pending", "b-muted"], not_available: ["Paywalled / not found", "b-bad"],
  pending_manual: ["Free, needs manual download", "b-warn"], download_failed: ["Download failed", "b-warn"], invalid_doi: ["Invalid DOI", "b-bad"],
};
const EVIDENCE = { full_text: ["Full text", "b-ok"], abstract: ["Abstract only", "b-warn"], metadata_only: ["Title only", "b-bad"] };
const badge = ([label, cls]) => `<span class="badge ${cls}">${esc(label)}</span>`;
const statusBadge = (s) => badge(STATUS[s] || [s, "b-muted"]);
const evBadge = (e) => (e ? badge(EVIDENCE[e] || [e, "b-muted"]) : badge(["No paper", "b-muted"]));
const citeBadge = (c) => c.source_kind === "non_doi" ? badge(["Non-DOI source", "b-info"])
  : c.doi_verified ? (c.issue ? badge(["DOI corrected", "b-warn"]) : badge(["DOI verified", "b-ok"])) : badge(["Unverified", "b-bad"]);

// file picker / drag-drop area
function dropzone(el, accept, onFiles) {
  const input = Object.assign(document.createElement("input"), { type: "file", multiple: true, accept, hidden: true });
  el.appendChild(input);
  el.addEventListener("click", () => input.click());
  input.addEventListener("change", () => { onFiles([...input.files]); input.value = ""; });
  el.addEventListener("dragover", (e) => { e.preventDefault(); el.classList.add("over"); });
  el.addEventListener("dragleave", () => el.classList.remove("over"));
  el.addEventListener("drop", (e) => { e.preventDefault(); el.classList.remove("over"); onFiles([...e.dataTransfer.files]); });
}
const upload = (path, file) => api(`${path}${path.includes("?") ? "&" : "?"}filename=${encodeURIComponent(file.name)}`, { method: "POST", body: file });

// ---------- job indicator ----------
let jobWatchers = [];
async function pollJob() {
  try {
    const j = await api("/jobs/current");
    const chip = $("#jobchip");
    if (j.status === "running") { chip.className = "running"; chip.textContent = `● ${j.step || j.kind}…`; }
    else if (j.status) { chip.className = ""; chip.textContent = j.status === "done" ? `Last task finished: ${j.kind}` : `Last task failed: ${j.kind}`; }
    jobWatchers.forEach((fn) => fn(j));
  } catch { /* server restarting */ }
}
setInterval(pollJob, 1500);

function jobBox(j) {
  if (!j || !j.id) return "";
  const icon = (s) => j.status === "failed" && j.step === s ? "✖" : j.steps.indexOf(s) < j.steps.indexOf(j.step) || j.status === "done" ? "✔" : j.step === s ? "●" : "○";
  return `<div class="card">
    <div class="row"><h2 style="margin:0">Task: ${esc(j.kind)}</h2>
      ${j.status === "running" ? badge(["Running", "b-info"]) : j.status === "done" ? badge(["Finished", "b-ok"]) : badge(["Failed", "b-bad"])}</div>
    <div class="steps">${j.steps.map((s) => `<span class="badge b-muted">${icon(s)} ${esc(s)}</span>`).join("")}</div>
    ${j.error ? `<div class="warnbox">${esc(j.error)}</div>` : ""}
    <div class="log">${esc(j.log.slice(-120).join("\n")) || "Starting…"}</div></div>`;
}
async function runJob(path, label) {
  try { await api(path, { method: "POST" }); toast(`${label} started`); pollJob(); }
  catch (e) { toast(e.message, 6000); }
}

// ---------- router ----------
const routes = { dashboard, import: importView, facts, papers, search, access };
function route() {
  const [, name = "dashboard", arg] = location.hash.match(/^#\/([^/?]+)\/?(.*)?$/) || [];
  document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("active", a.getAttribute("href") === `#/${name}`));
  jobWatchers = [];
  closePanel();
  (routes[name] || dashboard)(decodeURIComponent(arg || ""));
}
window.addEventListener("hashchange", route);

// ---------- dashboard ----------
async function dashboard() {
  view.innerHTML = `<h1>Dashboard</h1><p class="sub">What the knowledge base holds, and which papers are still missing.</p><div id="dash" class="empty">Loading…</div>`;
  const [cov, gaps] = await Promise.all([api("/coverage"), api("/gaps?limit=200")]);
  const ev = Object.fromEntries(cov.papers_by_evidence_level.map((r) => [r.evidence_level, r.n]));
  const totalPapers = cov.papers_by_status.reduce((a, r) => a + r.papers, 0);
  const totalFacts = cov.facts_by_type.reduce((a, r) => a + r.n, 0);
  const pct = (n) => totalPapers ? (100 * (n || 0) / totalPapers).toFixed(1) : 0;
  $("#dash").outerHTML = `
    <div class="grid cards">
      <div class="card stat"><div class="n">${totalFacts}</div><div class="l">MediaAssist facts</div></div>
      <div class="card stat"><div class="n">${totalPapers}</div><div class="l">Cited papers</div></div>
      <div class="card stat"><div class="n">${ev.full_text || 0}</div><div class="l">Papers with full text</div></div>
      <div class="card stat"><div class="n">${ev.abstract || 0}</div><div class="l">Abstract only</div></div>
      <div class="card stat"><div class="n">${cov.passages}</div><div class="l">Searchable passages</div></div>
    </div>
    <div class="grid two">
      <div class="card"><h2>What agents can read</h2>
        <div class="meter"><i style="width:${pct(ev.full_text)}%;background:var(--ok)"></i><i style="width:${pct(ev.abstract)}%;background:var(--warn)"></i><i style="width:${pct(ev.metadata_only)}%;background:var(--bad)"></i></div>
        <div class="legend"><span><i class="dot" style="background:var(--ok)"></i>Full text ${pct(ev.full_text)}%</span>
          <span><i class="dot" style="background:var(--warn)"></i>Abstract only ${pct(ev.abstract)}%</span>
          <span><i class="dot" style="background:var(--bad)"></i>Title only ${pct(ev.metadata_only)}%</span></div>
        <table style="margin-top:14px"><tr><th>Fact type</th><th class="num">Facts</th><th class="num">DOI verified</th><th class="num">Full text</th></tr>
          ${cov.facts_by_type.map((r) => `<tr><td><a href="#/facts?type=${r.query_type}">${esc(TYPE_LABEL[r.query_type] || r.query_type)}</a></td><td class="num">${r.n}</td><td class="num">${r.doi_verified}</td><td class="num">${r.with_full_text}</td></tr>`).join("")}
        </table></div>
      <div class="card"><h2>Add papers you've obtained</h2>
        <p class="muted small">Drop PDFs here (any filename). Each one is identified by its DOI or title, checked against the paper's real title, stored, and made searchable. Files that can't be matched are rejected.</p>
        <div class="drop" id="pdfdrop"><strong>Drop PDFs here</strong><br>or click to choose files</div>
        <div id="pdfresults"></div>
        <div class="row" style="margin-top:14px">
          <button onclick="runJob('/jobs/fetch?recheck_missing=true','Re-check missing papers')">Re-check all missing papers</button>
          <button onclick="runJob('/jobs/backup','Backup')">Back up everything</button>
        </div></div>
    </div>
    <div class="card" style="margin-top:16px">
      <div class="row" style="justify-content:space-between"><h2>Missing papers, most important first (${gaps.length})</h2>
        <a class="btn small" href="/api/gaps.csv">Download list (CSV)</a></div>
      <p class="muted small">Sorted by how many facts depend on each paper. Open the publisher page (e.g. through library access), download the PDF, then use Upload on that row or drop it above.</p>
      <table><tr><th class="num">Facts</th><th>Paper</th><th>Status</th><th>What we have</th><th></th></tr>
        ${gaps.map((g) => `<tr>
          <td class="num"><strong>${g.facts_citing}</strong></td>
          <td><a href="#/papers/${enc(g.doi)}">${esc(g.title || g.doi)}</a><div class="muted small">${esc(g.journal || "")} ${g.year || ""} · <span class="mono">${esc(g.doi)}</span></div></td>
          <td>${statusBadge(g.status)}</td><td>${evBadge(g.evidence_level)}</td>
          <td style="white-space:nowrap"><a class="btn small" href="${esc(g.manual_url || g.doi_url)}" target="_blank" rel="noopener">Open publisher page</a>
            <button class="small" data-upload="${esc(g.doi)}">Upload PDF</button></td></tr>`).join("") || `<tr><td colspan="5" class="empty">Nothing missing.</td></tr>`}
      </table></div>
    <div id="jobarea" style="margin-top:16px"></div>`;
  dropzone($("#pdfdrop"), ".pdf,.xml", dropPdfs);
  view.querySelectorAll("[data-upload]").forEach((b) => b.addEventListener("click", () => pickAndUpload(b.dataset.upload, dashboard)));
  jobWatchers.push((j) => { $("#jobarea") && ($("#jobarea").innerHTML = j.status === "running" ? jobBox(j) : ""); });
}

async function dropPdfs(files) {
  const out = $("#pdfresults");
  for (const f of files) {
    const line = document.createElement("div"); line.className = "small"; line.textContent = `${f.name}: checking…`; out.prepend(line);
    try {
      const r = await upload("/inbox", f);
      line.innerHTML = r.result === "stored" ? `<div class="okbox">✔ ${esc(f.name)} → stored as <strong>${esc(r.doi)}</strong> (matched via ${esc(r.matched_via)})</div>`
        : r.result === "already_have" ? `<div class="okbox">${esc(f.name)}: already have this paper (${esc(r.doi)})</div>`
        : `<div class="warnbox">✖ ${esc(f.name)}: not matched: ${esc(r.reason || "")}</div>`;
    } catch (e) { line.innerHTML = `<div class="warnbox">✖ ${esc(f.name)}: ${esc(e.message)}</div>`; }
  }
}

function pickAndUpload(doi, after) {
  const input = Object.assign(document.createElement("input"), { type: "file", accept: ".pdf,.xml" });
  input.onchange = async () => {
    const f = input.files[0]; if (!f) return;
    toast("Checking the file…");
    try { await upload(`/papers/${enc(doi)}/upload`, f); toast("Stored and made searchable ✔"); after?.(doi); }
    catch (e) {
      if (confirm(`${e.message}\n\nUpload anyway?`)) {
        try { await upload(`/papers/${enc(doi)}/upload?force=true`, f); toast("Stored (title not verified)"); after?.(doi); }
        catch (e2) { toast(e2.message, 6000); }
      }
    }
  };
  input.click();
}

// ---------- import ----------
function importView() {
  view.innerHTML = `<h1>Import MediaAssist exports</h1>
    <p class="sub">Drop the Excel/CSV files downloaded from MediaAssist. The type (solubility, uptake rate, …) is read from the filename.
      Then press <em>Process</em>: files are stored, every citation's DOI is checked against the real paper title, papers are downloaded where legally available, and their text is extracted.</p>
    <div class="card" style="margin-bottom:16px"><div class="row" style="justify-content:space-between">
      <div><h2 style="margin:0">Harvest everything from MediaAssist</h2>
        <div class="muted small">Downloads every export for every component and query type (about 20 minutes, one polite request at a time), then verifies, downloads papers and extracts text. Already-harvested exports are skipped.</div></div>
      <button class="primary" onclick="runJob('/jobs/harvest','Harvest')">Harvest MediaAssist</button></div></div>
    <div class="drop" id="xdrop"><strong>Or drop MediaAssist files here</strong><br>.xlsx, .xls, .csv, .tsv, or click to choose</div>
    <div class="card" style="margin-top:16px" id="staged"><h2>Ready to process</h2><div class="muted small">No files yet.</div></div>
    <div class="row" style="margin:14px 0">
      <label><input type="checkbox" id="optfetch" checked> Download papers</label>
      <label><input type="checkbox" id="optparse" checked> Extract text</label>
      <button class="primary" id="runbtn" disabled>Process</button>
    </div>
    <div id="jobarea"></div>`;
  const staged = [];
  const render = () => {
    $("#staged").innerHTML = `<h2>Ready to process (${staged.length})</h2>` + (staged.length ? `<table><tr><th>File</th><th>Type</th><th></th></tr>${staged.map((s) =>
      `<tr><td>${esc(s.name)}</td><td>${s.type ? esc(TYPE_LABEL[s.type] || s.type) : `<span class="badge b-bad">choose type</span>`}</td><td class="small">${esc(s.msg || "")}</td></tr>`).join("")}</table>` : `<div class="muted small">No files yet.</div>`);
    $("#runbtn").disabled = !staged.some((s) => s.type);
  };
  dropzone($("#xdrop"), ".xlsx,.xls,.csv,.tsv", async (files) => {
    for (const f of files) {
      const item = { name: f.name, type: null, msg: "uploading…" }; staged.push(item); render();
      try { item.type = (await upload("/import/stage", f)).query_type; item.msg = "ready"; }
      catch (e) {
        const t = prompt(`${e.message}\n\nType one of: ${TYPES.join(", ")}`);
        if (t && TYPES.includes(t.trim())) {
          try { item.type = (await upload(`/import/stage?query_type=${t.trim()}`, f)).query_type; item.msg = "ready"; }
          catch (e2) { item.msg = e2.message; }
        } else item.msg = "skipped (unknown type)";
      }
      render();
    }
  });
  $("#runbtn").onclick = async () => {
    try {
      await api(`/import/run?fetch=${$("#optfetch").checked}&parse_text=${$("#optparse").checked}`, { method: "POST" });
      staged.length = 0; render(); pollJob();
    } catch (e) { toast(e.message, 6000); }
  };
  jobWatchers.push((j) => { $("#jobarea").innerHTML = jobBox(j); if (j.status === "done" && j.kind === "import") $("#jobarea").insertAdjacentHTML("beforeend", `<div class="okbox" style="margin-top:10px">Done. See the <a href="#/dashboard">dashboard</a> for coverage and missing papers.</div>`); });
}

// ---------- facts ----------
const FACT_PREVIEW = {
  concentration: (d) => `${d["Medium"] ?? ""}: ${tidy(d["Concentration (mM)"]) ?? "?"} mM`,
  solubility: (d) => `${d["Aqueous Solubility"] ?? "?"} at ${d["Temperature (°C)"] ?? "?"} °C`,
  stability: (d) => `${d["Stability"] ?? "?"} (${d["Stability Measure"] ?? ""}; ${d["Temperature (°C)"] ?? ""}, pH ${d["pH"] ?? "?"})`,
  uptake_rate: (d) => `${tidy(d["Specific Uptake Rate in pmol/cell/day"]) ?? d["Specific Uptake Rate"] ?? "?"} pmol/cell/day · ${d["Medium/Condition in which uptake rate is measured"] ?? ""}`,
  transporter: (d) => `${d["Transporter"] ?? ""} (${d["Mechanism of Transport"] ?? ""}) · ${d["Cell Type"] ?? ""}`,
  shared_transporter: (d) => `${d["Selected Component"]} + ${d["Other Component"]} share ${d["Shared Transporter"]}`,
  codependency: (d) => `+ ${d["Other component"] ?? d["Component 2"]} (${d["Type of co-dependency"] ?? ""}): ${d["Summary"] ?? ""}`,
};
// Round long decimals for display only (e.g. 0.460208777640588 -> 0.4602); the stored value is untouched.
const tidy = (v) => (/^-?\d+\.\d{5,}$/.test(String(v ?? "")) ? String(+Number(v).toPrecision(4)) : v);
const componentOf = (d) => d["Component Name"] ?? d["Selected component"] ?? d["Selected Component"] ?? d["Component 1"] ?? "";

async function facts() {
  const params = new URLSearchParams(location.hash.split("?")[1] || "");
  view.innerHTML = `<h1>Facts</h1><p class="sub">Every MediaAssist record, with its citation and what we hold of the cited paper. Click a row for the evidence.</p>
    <div class="filters">
      <input id="fcomp" list="complist" placeholder="Component (e.g. Alanine)" value="${esc(params.get("component") || "")}">
      <datalist id="complist"></datalist>
      <select id="ftype"><option value="">All types</option>${TYPES.map((t) => `<option value="${t}" ${params.get("type") === t ? "selected" : ""}>${TYPE_LABEL[t]}</option>`).join("")}</select>
      <input type="search" id="ftext" placeholder="Anything else: cell line, medium, author…">
    </div><div class="card" id="factlist"><div class="empty">Loading…</div></div>`;
  api("/components").then((cs) => { $("#complist").innerHTML = cs.map((c) => `<option value="${esc(c.component)}">`).join(""); });
  const load = async () => {
    const q = new URLSearchParams({ limit: 500 });
    if ($("#fcomp").value) q.set("component", $("#fcomp").value);
    if ($("#ftype").value) q.set("query_type", $("#ftype").value);
    if ($("#ftext").value) q.set("text", $("#ftext").value);
    const rows = await api("/facts?" + q);
    $("#factlist").innerHTML = rows.length ? `<table><tr><th>Component</th><th>Type</th><th>Fact</th><th>Citation</th><th>Paper</th></tr>${rows.map((f) => `
      <tr class="click" data-id="${f.fact_id}"><td><strong>${esc(componentOf(f.data))}</strong></td><td>${esc(TYPE_LABEL[f.query_type])}</td>
        <td>${esc((FACT_PREVIEW[f.query_type] || (() => ""))(f.data)).slice(0, 180)}</td><td>${citeBadge(f.citation)}</td><td>${evBadge(f.paper?.evidence_level)}</td></tr>`).join("")}</table>
      <div class="muted small" style="margin-top:8px">${rows.length} facts${rows.length === 500 ? " (first 500; narrow the filters)" : ""}</div>` : `<div class="empty">No facts match.</div>`;
    $("#factlist").querySelectorAll("tr.click").forEach((tr) => tr.onclick = () => showFact(+tr.dataset.id));
  };
  let t; ["fcomp", "ftype", "ftext"].forEach((id) => $("#" + id).addEventListener("input", () => { clearTimeout(t); t = setTimeout(load, 250); }));
  await load();
  if (params.get("fact")) showFact(+params.get("fact"));
}

async function showFact(id) {
  openPanel("Fact", `<div class="empty">Loading…</div>`);
  const f = await api(`/facts/${id}/evidence?limit=5`);
  const c = f.citation, p = f.paper;
  const rows = Object.entries(f.data).filter(([k, v]) => v && !/citation|source/i.test(k));
  $("#panel-title").textContent = `${componentOf(f.data)}: ${TYPE_LABEL[f.query_type]}`;
  $("#panel-body").innerHTML = `
    <div class="kv">${rows.map(([k, v]) => `<div>${esc(k)}</div><div>${/^https?:/.test(v) ? `<a href="${esc(v)}" target="_blank" rel="noopener">${esc(v)}</a>` : esc(v)}</div>`).join("")}</div>
    <h2>${(f.citations || []).length > 1 ? `Citations (${f.citations.length})` : "Citation"}</h2>
    ${(f.citations && f.citations.length ? f.citations : [c]).map((x) => `<div class="passage" style="border-color:var(--line)">
      <div class="row">${citeBadge(x)} ${x.paper ? evBadge(x.paper.evidence_level) : ""}
        ${x.doi ? `<a class="mono small" href="#/papers/${enc(x.doi)}">${esc(x.doi)}</a>` : ""}
        ${x.paper && x.paper.status === "downloaded" ? `<a class="btn small" href="/api/papers/${enc(x.doi)}/file" target="_blank">Open PDF</a>` : ""}</div>
      <p style="margin:6px 0">${esc(x.text || "")}</p>
      ${x.url ? `<p class="small"><a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.url)}</a></p>` : ""}
      ${x.issue ? `<div class="warnbox"><strong>Note:</strong> ${esc(x.issue)}</div>` : ""}</div>`).join("")}
    ${p ? `<h2 style="margin-top:18px">Paper ${evBadge(p.evidence_level)}</h2>
      <p><a href="#/papers/${enc(c.doi)}">${esc(p.title || c.doi)}</a> <span class="mono muted">${esc(c.doi)}</span></p>
      ${p.status === "downloaded" ? `<p><a class="btn small" href="/api/papers/${enc(c.doi)}/file" target="_blank">Open PDF</a></p>` : `<div class="warnbox">We don't have this paper's full text${p.evidence_level === "abstract" ? "; the passages below come from its abstract" : ""}.</div>`}
      <h2 style="margin-top:18px">Supporting passages</h2>
      ${f.supporting_passages.map(passageHtml).join("") || `<div class="muted">No passages.</div>`}` : ""}`;
}

function passageHtml(ps, terms) {
  let text = esc(ps.text);
  (terms || []).forEach((t) => { if (t.length > 2) text = text.replace(new RegExp(`(${t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")})`, "gi"), "<mark>$1</mark>"); });
  return `<div class="passage"><div class="meta">${esc(ps.section || ps.kind)}${ps.page ? ` · page ${ps.page}` : ""} · <span class="mono">${esc(ps.passage_id)}</span>
    ${ps.evidence_level ? evBadge(ps.evidence_level) : ""}</div>${ps.title ? `<div class="small"><a href="#/papers/${enc(ps.doi)}">${esc(ps.title)}</a></div>` : ""}${text}</div>`;
}

// ---------- papers ----------
async function papers(doi) {
  view.innerHTML = `<h1>Papers</h1><p class="sub">Every paper cited by MediaAssist, and whether we hold its full text.</p>
    <div class="filters"><input type="search" id="pq" placeholder="Title, DOI or journal">
      <select id="ps"><option value="">All statuses</option>${Object.entries(STATUS).map(([k, [l]]) => `<option value="${k}">${l}</option>`).join("")}</select></div>
    <div class="card" id="plist"><div class="empty">Loading…</div></div>`;
  const load = async () => {
    const q = new URLSearchParams(); if ($("#pq").value) q.set("q", $("#pq").value); if ($("#ps").value) q.set("status", $("#ps").value);
    const rows = await api("/papers?" + q);
    $("#plist").innerHTML = rows.length ? `<table><tr><th>Paper</th><th class="num">Facts</th><th>Status</th><th>Source</th></tr>${rows.map((p) => `
      <tr class="click" data-doi="${esc(p.doi)}"><td>${esc(p.title || p.doi)}<div class="muted small">${esc(p.journal || "")} ${p.year || ""} · <span class="mono">${esc(p.doi)}</span></div></td>
        <td class="num">${p.facts_citing}</td><td>${statusBadge(p.status)}</td><td class="small">${esc(p.source || "")}</td></tr>`).join("")}</table>` : `<div class="empty">No papers match.</div>`;
    $("#plist").querySelectorAll("tr.click").forEach((tr) => tr.onclick = () => showPaper(tr.dataset.doi));
  };
  let t; ["pq", "ps"].forEach((id) => $("#" + id).addEventListener("input", () => { clearTimeout(t); t = setTimeout(load, 250); }));
  await load();
  if (doi) showPaper(doi);
}

async function showPaper(doi) {
  openPanel("Paper", `<div class="empty">Loading…</div>`);
  const [p, ps] = await Promise.all([api(`/papers/${enc(doi)}`), api(`/papers/${enc(doi)}/passages?limit=2000`)]);
  $("#panel-title").textContent = p.title || p.doi;
  $("#panel-body").innerHTML = `
    <div class="row">${statusBadge(p.status)} ${evBadge(p.evidence_level)} ${p.is_retracted ? badge(["RETRACTED", "b-bad"]) : ""}</div>
    <div class="kv">
      <div>DOI</div><div><a href="https://doi.org/${esc(p.doi)}" target="_blank" rel="noopener" class="mono">${esc(p.doi)}</a></div>
      <div>Journal</div><div>${esc(p.journal || "")} ${p.year || ""}</div>
      <div>Authors</div><div>${esc((p.authors || []).join(", "))}</div>
      <div>Facts citing it</div><div><a href="#/facts?text=${encodeURIComponent(p.doi)}">${p.facts_citing}</a></div>
      ${p.status === "downloaded" ? `<div>File</div><div>from <strong>${esc(p.source)}</strong> · <span class="mono small">sha256 ${esc((p.sha256 || "").slice(0, 16))}…</span><br><span class="small muted">${esc(p.source_url || "")}</span></div>` : ""}
      ${p.license ? `<div>License</div><div>${esc(p.license)}</div>` : ""}
    </div>
    <div class="row">
      ${p.status === "downloaded" ? `<a class="btn primary" href="/api/papers/${enc(p.doi)}/file" target="_blank">Open PDF</a>` : `<a class="btn" href="${esc(p.manual_url || "https://doi.org/" + p.doi)}" target="_blank" rel="noopener">Open publisher page</a>`}
      <button id="upbtn">${p.status === "downloaded" ? "Replace file" : "Upload PDF"}</button>
    </div>
    ${p.abstract ? `<h2 style="margin-top:18px">Abstract</h2><p>${esc(p.abstract)}</p>` : ""}
    <h2 style="margin-top:18px">Text (${ps.length} passages)</h2>
    ${ps.filter((x) => x.kind !== "abstract" && x.kind !== "title").map((x) => passageHtml({ ...x, doi: p.doi })).join("") || `<div class="muted">No full text yet.</div>`}`;
  $("#upbtn").onclick = () => pickAndUpload(p.doi, showPaper);
}

// ---------- search ----------
function search() {
  view.innerHTML = `<h1>Search paper text</h1><p class="sub">Full-text search across every passage we hold. Use "quotes" for phrases, OR, and -word to exclude.</p>
    <div class="filters"><input type="search" id="sq" placeholder='e.g. glutamine degradation ammonia, or "amino acid" transport' autofocus>
      <select id="sev"><option value="">Any evidence</option><option value="full_text">Full-text papers only</option><option value="abstract">Abstracts only</option></select></div>
    <div id="sres"></div>`;
  const load = async () => {
    const q = $("#sq").value.trim(); if (!q) { $("#sres").innerHTML = ""; return; }
    const params = new URLSearchParams({ q, limit: 50 }); if ($("#sev").value) params.set("evidence_level", $("#sev").value);
    const rows = await api("/passages/search?" + params);
    const terms = q.replace(/["()]/g, " ").split(/\s+/).filter((w) => w && w.toUpperCase() !== "OR" && !w.startsWith("-"));
    $("#sres").innerHTML = `<div class="card">${rows.map((r) => passageHtml(r, terms)).join("") || `<div class="empty">No passages match.</div>`}</div>`;
  };
  let t; ["sq", "sev"].forEach((id) => $("#" + id).addEventListener("input", () => { clearTimeout(t); t = setTimeout(load, 300); }));
}

// ---------- access (who may use the hosted MCP server) ----------
async function access() {
  const info = await api("/access/info");
  view.innerHTML = `<h1>Access</h1>
    <p class="sub">Who can use the hosted MCP server from Claude Code or Claude Desktop. Changes take effect within about 30 seconds, with no redeploy.</p>
    <div class="grid two">
      <div class="card"><h2>Add a person</h2>
        <div class="row"><input id="newname" placeholder="Name, e.g. priya" style="flex:1">
          <button class="primary" id="addbtn">Create token</button></div>
        <div id="newtoken"></div></div>
      <div class="card"><h2>Server</h2>
        <div class="kv"><div>MCP address</div><div class="mono">${esc(info.mcp_url)}</div>
          <div>Login</div><div>a personal token (Authorization: Bearer …)</div></div></div>
    </div>
    <div class="card" style="margin-top:16px"><div class="row" style="justify-content:space-between"><h2 style="margin:0">People with access</h2>
      <label class="small"><input type="checkbox" id="showrevoked"> show revoked</label></div>
      <div id="userlist" style="margin-top:10px"><div class="empty">Loading…</div></div></div>`;
  const fmt = (d) => (d ? new Date(d).toLocaleString() : "never");
  const load = async () => {
    const users = await api(`/access/users?include_revoked=${$("#showrevoked").checked}`);
    $("#userlist").innerHTML = users.length ? `<table><tr><th>Name</th><th>Added</th><th>Last used</th><th>Status</th><th></th></tr>${users.map((u) => `
      <tr><td><strong>${esc(u.name)}</strong></td><td class="small">${fmt(u.created_at)}</td><td class="small">${fmt(u.last_used_at)}</td>
        <td>${u.revoked_at ? badge(["Revoked " + new Date(u.revoked_at).toLocaleDateString(), "b-muted"]) : badge(["Active", "b-ok"])}</td>
        <td>${u.revoked_at ? "" : `<button class="small" data-revoke="${esc(u.name)}">Revoke</button>`}</td></tr>`).join("")}</table>`
      : `<div class="empty">No one has access yet.</div>`;
    view.querySelectorAll("[data-revoke]").forEach((b) => b.onclick = async () => {
      if (!confirm(`Revoke access for ${b.dataset.revoke}? Their token stops working within about 30 seconds.`)) return;
      try { await api(`/access/users/${encodeURIComponent(b.dataset.revoke)}`, { method: "DELETE" }); toast("Access revoked"); load(); }
      catch (e) { toast(e.message, 6000); }
    });
  };
  $("#showrevoked").onchange = load;
  $("#addbtn").onclick = async () => {
    const name = $("#newname").value.trim();
    if (!name) { toast("Enter a name first"); return; }
    try {
      const r = await api(`/access/users?name=${encodeURIComponent(name)}`, { method: "POST" });
      const code = `claude mcp add --scope user --transport http lemnisca-media-kb-remote ${info.mcp_url} --header "Authorization: Bearer ${r.token}"`;
      const desk = JSON.stringify({ mcpServers: { "lemnisca-media-kb-remote": { command: "npx",
        args: ["-y", "mcp-remote@latest", info.mcp_url, "--header", "Authorization:${AUTH_HEADER}"],
        env: { AUTH_HEADER: "Bearer " + r.token } } } }, null, 2);
      $("#newtoken").innerHTML = `<div class="okbox" style="margin-top:12px"><strong>Token for ${esc(r.name)}</strong>. Copy it now; it won't be shown again. Send it privately.</div>
        <div class="row"><input class="mono" id="tok" value="${esc(r.token)}" readonly style="flex:1"><button class="small" data-copy="tok">Copy token</button></div>
        <h2 style="margin-top:14px">Claude Code: they run</h2><textarea class="mono" id="cc" readonly rows="3" style="width:100%">${esc(code)}</textarea>
        <button class="small" data-copy="cc">Copy</button>
        <h2 style="margin-top:14px">Claude Desktop: add to claude_desktop_config.json (needs Node.js)</h2>
        <textarea class="mono" id="cd" readonly rows="9" style="width:100%">${esc(desk)}</textarea><button class="small" data-copy="cd">Copy</button>`;
      view.querySelectorAll("[data-copy]").forEach((b) => b.onclick = () => { const el = $("#" + b.dataset.copy); el.select(); navigator.clipboard?.writeText(el.value); toast("Copied"); });
      $("#newname").value = ""; load();
    } catch (e) { toast(e.message, 6000); }
  };
  load();
}

route();
pollJob();
