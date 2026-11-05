"use strict";
// Every value from the server is written with textContent, never innerHTML:
// voucher narrations are untrusted text and must not become markup.
const $ = (id) => document.getElementById(id);
const NUMERIC = new Set(["debit","credit","amount","balance","closing","outstanding",
  "total_amount","net_profit","total_income","total_expense","running_balance","age_days",
  "bill_count","voucher_count","credit_limit"]);
const COUNTS = new Set(["age_days","bill_count","voucher_count","credit_days"]);
const state = { key: null, session: null, reports: [], seq: 0, refresh: null };

function store(k, v) { try { sessionStorage.setItem(k, v); } catch (_) {} }
function load(k) { try { return sessionStorage.getItem(k); } catch (_) { return null; } }
function forget(k) { try { sessionStorage.removeItem(k); } catch (_) {} }

// FastAPI validation errors arrive as a list of {loc, msg}; make them readable.
function errorText(data, status) {
  const d = data && data.detail;
  if (Array.isArray(d)) {
    return d.map((e) => {
      const field = Array.isArray(e.loc) ? e.loc[e.loc.length - 1] : "";
      return (field ? field + ": " : "") + (e.msg || "invalid value");
    }).join("; ");
  }
  if (typeof d === "string" && d) return d;
  return "Request failed (HTTP " + status + ")";
}

async function api(path, body) {
  const res = await fetch(path, {
    method: body ? "POST" : "GET",
    headers: { "Authorization": "Bearer " + state.key,
               ...(body ? { "Content-Type": "application/json" } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(errorText(data, res.status));
  return data;
}

function inr(v) {
  const n = Number(v);
  if (!isFinite(n)) return String(v);
  return n.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function freshness(lag, status, live) {
  const b = $("fresh");
  b.hidden = false;
  const stale = status === "failed" || (lag != null && lag > 10);
  b.className = "badge " + (stale ? "stale" : "ok");
  b.textContent = live ? "Live from Tally"
    : status === "failed" ? "Last sync failed"
    : lag == null ? "Sync status unknown" : `Synced ${Math.round(lag)} min ago`;
}

function companyName() {
  const o = $("company").selectedOptions[0];
  return o ? o.textContent : "";
}

async function refreshCompanies() {
  const me = await api("/v1/me");
  const sel = $("company");
  const keep = sel.value;
  sel.replaceChildren();
  for (const c of me.companies) {
    const o = document.createElement("option");
    o.value = c.company_id; o.textContent = c.name;
    o.dataset.lag = c.lag_minutes ?? ""; o.dataset.status = c.sync_status ?? "";
    sel.appendChild(o);
  }
  if (keep && [...sel.options].some((o) => o.value === keep)) sel.value = keep;
  showCompanyFreshness();
  return me;
}

function showCompanyFreshness() {
  const o = $("company").selectedOptions[0];
  if (o) freshness(o.dataset.lag === "" ? null : Number(o.dataset.lag), o.dataset.status, false);
}

async function connect(key) {
  state.key = key;
  const me = await refreshCompanies();
  store("tak", key);
  $("whoami").textContent = `${me.id} · ${me.role}${me.may_unmask ? "" : " · masked view"}`;
  state.reports = await api("/v1/reports");
  const rs = $("report-id");
  rs.replaceChildren();
  for (const r of state.reports) {
    const o = document.createElement("option");
    o.value = r.id; o.textContent = r.title; rs.appendChild(o);
  }
  renderParams();
  $("signin").hidden = true;
  $("workspace").hidden = false;
  $("signout").hidden = false;
  clearInterval(state.refresh);
  state.refresh = setInterval(() => { refreshCompanies().catch(() => {}); }, 60000);
}

function signOut() {
  clearInterval(state.refresh);
  forget("tak");
  state.key = null; state.session = null; state.reports = []; state.seq++;
  $("key").value = "";
  clearResult();
  $("workspace").hidden = true;
  $("signout").hidden = true;
  $("fresh").hidden = true;
  $("whoami").textContent = "";
  $("signin").hidden = false;
}

function clearResult() {
  $("result").hidden = true;
  $("rows").replaceChildren();
  $("answer").textContent = "";
  $("error").hidden = true;
}

function onCompanyChange() {
  state.seq++;            // any reply still in flight belongs to the old company
  state.session = null;   // placeholders are per company conversation
  clearResult();
  showCompanyFreshness();
}

function renderParams() {
  const r = state.reports.find((x) => x.id === $("report-id").value);
  const box = $("report-params");
  box.replaceChildren();
  if (!r) return;
  for (const p of r.params) {
    const l = document.createElement("label");
    l.textContent = p.name.replace(/_/g, " ") + (p.required ? "" : " (optional)");
    let input;
    if (p.type === "group") {
      input = document.createElement("select");
      for (const g of ["Sundry Debtors", "Sundry Creditors"]) {
        const o = document.createElement("option"); o.value = g; o.textContent = g; input.appendChild(o);
      }
    } else {
      input = document.createElement("input");
      input.type = p.type === "date" ? "date" : (p.type === "int" || p.type === "numeric") ? "number" : "text";
      if (p.type === "numeric") input.step = "any";
      if (p.type === "int") input.step = "1";
      if (p.default != null) input.value = p.default;
    }
    input.name = p.name;
    l.appendChild(input);
    box.appendChild(l);
  }
}

function show(res, company) {
  $("error").hidden = true;
  $("result").hidden = false;
  $("answer").textContent = res.answer;
  $("explanation").hidden = !res.explanation;
  $("explanation-text").textContent = res.explanation || "";
  const w = $("warnings");
  w.replaceChildren();
  for (const msg of res.warnings || []) {
    const li = document.createElement("li"); li.textContent = msg; w.appendChild(li);
  }
  const asOf = res.data_as_of ? new Date(res.data_as_of).toLocaleString("en-IN") : "unknown";
  $("meta").textContent = [
    company && `Company: ${company}`,
    res.report_title && `Report: ${res.report_title}`,
    `Data as of ${asOf}${res.live ? " (live from Tally)" : ""}`,
    `Routed by: ${res.source}`,
    `${res.row_count} rows`,
    `${res.latency_ms} ms`,
  ].filter(Boolean).join(" · ");
  const t = $("rows");
  t.replaceChildren();
  if (res.rows && res.rows.length) {
    const head = t.createTHead().insertRow();
    for (const c of res.columns) {
      const th = document.createElement("th"); th.textContent = c.replace(/_/g, " "); head.appendChild(th);
    }
    const body = t.createTBody();
    for (const row of res.rows) {
      const tr = body.insertRow();
      for (const c of res.columns) {
        const td = tr.insertCell();
        const v = row[c];
        if (NUMERIC.has(c) && v !== null && v !== "") {
          td.className = "num";
          td.textContent = COUNTS.has(c) ? String(v) : inr(v);
        } else td.textContent = v == null ? "" : String(v);
      }
    }
  }
  if (res.session_id) state.session = res.session_id;
  if (res.live || res.lag_minutes != null) freshness(res.lag_minutes, null, res.live);
}

function fail(err) {
  clearResult();          // never leave an old answer under a new error
  $("error").hidden = false;
  $("error").textContent = err.message;
}

async function run(form, fn) {
  const btn = form.querySelector("button[type=submit]");
  const seq = ++state.seq;
  const company = companyName();
  btn.disabled = true;
  $("company").disabled = true;
  clearResult();
  try {
    const res = await fn();
    if (seq === state.seq) show(res, company);   // ignore replies for a superseded request
  } catch (e) {
    if (seq === state.seq) fail(e);
  } finally {
    btn.disabled = false;
    $("company").disabled = false;
  }
}

$("signin-form").addEventListener("submit", (e) => {
  e.preventDefault();
  $("signin-error").hidden = true;
  connect($("key").value.trim()).catch((err) => {
    state.key = null;
    $("signin-error").hidden = false; $("signin-error").textContent = err.message;
  });
});
$("signout").addEventListener("click", signOut);
$("company").addEventListener("change", onCompanyChange);
$("report-id").addEventListener("change", renderParams);
for (const b of document.querySelectorAll(".tabs button")) {
  b.addEventListener("click", () => {
    for (const x of document.querySelectorAll(".tabs button")) x.classList.toggle("active", x === b);
    $("tab-ask").hidden = b.dataset.tab !== "ask";
    $("tab-forms").hidden = b.dataset.tab !== "forms";
  });
}
$("ask-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const q = $("question").value.trim();
  if (q.length < 2) { fail(new Error("Please type a question.")); return; }
  run(e.target, () => api("/v1/ask", {
    question: q, company_id: Number($("company").value), session_id: state.session }));
});
$("report-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const params = {};
  for (const el of $("report-params").querySelectorAll("input,select")) {
    if (el.value !== "") params[el.name] = el.type === "number" ? Number(el.value) : el.value;
  }
  run(e.target, () => api("/v1/report", {
    report_id: $("report-id").value, company_id: Number($("company").value), params,
    session_id: state.session }));
});
const saved = load("tak");
if (saved) connect(saved).catch(() => { forget("tak"); state.key = null; });
