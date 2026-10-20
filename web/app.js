"use strict";
// Books — mobile web front-end for the Tally Web Gateway. No build step, no third-party code.

const $ = (sel, el = document) => el.querySelector(sel);
const view = $("#view");
const banner = $("#banner");
const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
};
const state = { me: null, company: store.get("company") || "", ledgers: [] };

const inr = new Intl.NumberFormat("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const today = () => new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 10);
const shiftDays = (iso, n) => { const d = new Date(iso + "T00:00:00"); d.setDate(d.getDate() + n); return d.toISOString().slice(0, 10); };
const fmtDate = (iso) => iso ? new Date(iso + "T00:00:00").toLocaleDateString("en-IN", { day: "2-digit", month: "short", year: "numeric" }) : "";

function money(v) {
  if (!v || !v.amount || Number(v.amount) === 0) return `<span class="num">${inr.format(0)}</span>`;
  const cls = v.side === "Dr" ? "dr" : "cr";
  return `<span class="num money">₹${inr.format(Number(v.amount))}<span class="side-tag ${cls}">${esc(v.side)}</span></span>`;
}

function showBanner(text) { banner.textContent = text || ""; banner.hidden = !text; }

async function api(path, opts = {}) {
  const res = await fetch(path, { credentials: "same-origin", ...opts });
  const stale = res.headers.get("x-offline-cache");
  let body = null;
  try { body = await res.json(); } catch { /* non-JSON (e.g. Access login page) */ }
  if (res.status === 403 || (!body && res.redirected)) {
    throw new Error("Your sign-in has expired or you don't have access. Reload the page to sign in again.");
  }
  if (!res.ok) throw new Error((body && body.error) || `Request failed (${res.status})`);
  if (stale) showBanner(`You're offline — showing data saved at ${new Date(stale).toLocaleTimeString("en-IN")}.`);
  return body;
}
const q = (params) => new URLSearchParams({ company: state.company, ...params }).toString();

function errorBox(e) { return `<div class="msg err">${esc(e.message || e)}</div>`; }

// ---------------------------------------------------------------- views
async function renderHome() {
  const d = await api(`/api/dashboard?${q({})}`);
  const t = d.tiles;
  const tile = (label, v) => `<div class="tile"><div class="label">${label}</div><div class="val">${money(v)}</div></div>`;
  view.innerHTML = `
    <div class="tiles">
      ${tile("Cash in hand", t.cash)}${tile("Bank", t.bank)}
      ${tile("Receivables", t.receivables)}${tile("Payables", t.payables)}
    </div>
    <h2>Last 7 days</h2>
    ${voucherList(d.recent, "No entries in the last 7 days.")}
    <p class="muted center">Updated ${esc(new Date(d.as_of).toLocaleString("en-IN"))}</p>`;
}

function voucherList(rows, empty, ledger) {
  if (!rows.length) return `<p class="muted">${empty}</p>`;
  return `<div class="list">${rows.map((v) => {
      const amt = ledger ? money(v.effect) : `<span class="num">₹${inr.format(Number(v.amount || 0))}</span>`;
      const lines = v.lines.map((l) => `<div><span>${esc(l.ledger)}</span>${money(l)}</div>`).join("");
      return `<div class="row"><div class="main">
          <div class="title">${esc(v.party || v.lines[0]?.ledger || v.type)}</div>
          <div class="sub">${esc(fmtDate(v.date))} · ${esc(v.type)} ${v.number ? "#" + esc(v.number) : ""}${v.narration ? " · " + esc(v.narration) : ""}</div>
          <div class="vch-lines">${lines}</div>
        </div><div class="amt">${amt}</div></div>`;
    }).join("")}</div>`;
}

async function renderLedgers(params) {
  const term = params.get("q") || "";
  view.innerHTML = `
    <div class="toolbar"><label>Search ledgers or groups
      <input id="lq" type="search" value="${esc(term)}" placeholder="e.g. HDFC, Sundry Debtors" enterkeyhint="search"></label></div>
    <div id="lres"><p class="muted">Loading…</p></div>`;
  const input = $("#lq");
  let timer;
  const load = async () => {
    try {
      const d = await api(`/api/ledgers?${q({ q: input.value })}`);
      $("#lres").innerHTML = d.ledgers.length ? `<div class="list">${d.ledgers.map((l) => `
        <a class="row" href="#/ledger?name=${encodeURIComponent(l.name)}">
          <div class="main"><div class="title">${esc(l.name)}</div><div class="sub">${esc(l.group)}</div></div>
          <div class="amt">${money(l.closing)}</div></a>`).join("")}</div>
        ${d.total > d.ledgers.length ? `<p class="muted">Showing ${d.ledgers.length} of ${d.total}. Refine your search.</p>` : ""}`
        : `<p class="muted">No ledgers match.</p>`;
    } catch (e) { $("#lres").innerHTML = errorBox(e); }
  };
  input.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(load, 250); });
  await load();
}

function rangeToolbar(from, to) {
  return `<form class="toolbar" id="range">
    <label>From<input type="date" name="from" value="${from}"></label>
    <label>To<input type="date" name="to" value="${to}"></label>
    <button class="primary">Show</button></form>`;
}
function bindRange(route, extra = {}) {
  $("#range").addEventListener("submit", (ev) => {
    ev.preventDefault();
    const f = new FormData(ev.target);
    location.hash = `#/${route}?${new URLSearchParams({ ...extra, from: f.get("from"), to: f.get("to") })}`;
  });
}

async function renderLedger(params) {
  const name = params.get("name") || "";
  const to = params.get("to") || today(), from = params.get("from") || shiftDays(to, -30);
  view.innerHTML = `<h1>${esc(name)}</h1>${rangeToolbar(from, to)}<div id="lv"><p class="muted">Loading…</p></div>`;
  bindRange("ledger", { name });
  const d = await api(`/api/ledger?${q({ name, from, to })}`);
  $("#lv").innerHTML = voucherList(d.vouchers, "No entries in this period.", true);
}

async function renderDaybook(params) {
  const to = params.get("to") || today(), from = params.get("from") || to;
  view.innerHTML = `${rangeToolbar(from, to)}<div id="db"><p class="muted">Loading…</p></div>`;
  bindRange("daybook");
  const d = await api(`/api/daybook?${q({ from, to })}`);
  $("#db").innerHTML = voucherList(d.vouchers, "No entries for this period.");
}

async function renderNew() {
  if (state.me.role !== "poster") { view.innerHTML = errorBox("Your account can view reports but not post entries."); return; }
  if (!state.ledgers.length) state.ledgers = (await api(`/api/ledgers?${q({})}`)).ledgers;
  $("#ledger-list").innerHTML = state.ledgers.map((l) => `<option value="${esc(l.name)}">`).join("");
  const minDate = shiftDays(today(), -state.me.backdate_days);
  view.innerHTML = `
    <form class="form" id="vf" autocomplete="off">
      <div class="pair">
        <label>Type<select name="type">${state.me.voucher_types.map((t) => `<option>${esc(t)}</option>`).join("")}</select></label>
        <label>Date<input type="date" name="date" value="${today()}" min="${minDate}" max="${today()}" required></label>
      </div>
      <div><div class="muted" id="hint"></div><div id="lines"></div>
        <button type="button" id="add">＋ Add line</button></div>
      <div class="totals"><span>Dr <b class="num" id="tdr">0.00</b></span><span>Cr <b class="num" id="tcr">0.00</b></span><span id="tdiff"></span></div>
      <label>Narration<textarea name="narration" rows="2" maxlength="500" placeholder="What is this for?"></textarea></label>
      <button class="primary" id="post">Post to Tally</button>
      <div id="vmsg"></div>
    </form>`;
  const linesEl = $("#lines"), form = $("#vf");
  const hints = { Receipt: "Receipt: Dr the cash/bank ledger, Cr the party or income.", Payment: "Payment: Dr the party or expense, Cr the cash/bank ledger.",
    Contra: "Contra: move money between cash and bank ledgers.", Journal: "Journal: any balanced adjustment." };
  const addLine = (side) => {
    const n = $("#tpl-line").content.cloneNode(true);
    if (side) n.querySelector(".side").value = side;
    linesEl.appendChild(n);
  };
  const totals = () => {
    let dr = 0, cr = 0;
    linesEl.querySelectorAll(".line").forEach((l) => {
      const a = Math.round((parseFloat(l.querySelector(".amount").value) || 0) * 100);
      l.querySelector(".side").value === "Dr" ? (dr += a) : (cr += a);
    });
    $("#tdr").textContent = inr.format(dr / 100); $("#tcr").textContent = inr.format(cr / 100);
    const diff = dr - cr;
    $("#tdiff").innerHTML = diff ? `<span class="bad">Off by ${inr.format(Math.abs(diff) / 100)}</span>` : (dr ? "Balanced ✓" : "");
    return { dr, cr };
  };
  const setHint = () => { $("#hint").textContent = hints[form.type.value] || ""; };
  addLine("Dr"); addLine("Cr"); setHint();
  form.type.addEventListener("change", setHint);
  $("#add").addEventListener("click", () => addLine());
  linesEl.addEventListener("input", totals);
  linesEl.addEventListener("change", totals);
  linesEl.addEventListener("click", (e) => { if (e.target.classList.contains("rm") && linesEl.children.length > 2) { e.target.closest(".line").remove(); totals(); } });

  let key = crypto.randomUUID();
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const { dr, cr } = totals();
    const msg = $("#vmsg");
    if (!dr || dr !== cr) { msg.innerHTML = errorBox("Debits and credits must match and be more than zero."); return; }
    const lines = [...linesEl.querySelectorAll(".line")].map((l) => ({
      side: l.querySelector(".side").value, ledger: l.querySelector(".ledger").value.trim(), amount: l.querySelector(".amount").value.trim() }))
      .filter((l) => l.ledger || l.amount);
    $("#post").disabled = true;
    try {
      await api("/api/vouchers", { method: "POST",
        headers: { "Content-Type": "application/json", "X-Requested-With": "tally-web", "Idempotency-Key": key },
        body: JSON.stringify({ company: state.company, type: form.type.value, date: form.date.value, narration: form.narration.value, lines }) });
      msg.innerHTML = `<div class="msg ok">Posted to Tally. It appears in Day Book and the Edit Log.</div>`;
      key = crypto.randomUUID();
      linesEl.innerHTML = ""; addLine("Dr"); addLine("Cr"); form.narration.value = ""; totals();
    } catch (e) { msg.innerHTML = errorBox(e); }
    finally { $("#post").disabled = false; }
  });
}

// --------------------------------------------------------------- router
const routes = { "": ["home", renderHome], ledgers: ["ledgers", renderLedgers], ledger: ["ledgers", renderLedger],
  daybook: ["daybook", renderDaybook], new: ["new", renderNew] };

async function route() {
  const [path, qs] = location.hash.replace(/^#\/?/, "").split("?");
  const [tab, fn] = routes[path] || routes[""];
  document.querySelectorAll(".tabs a").forEach((a) => a.classList.toggle("on", a.dataset.tab === tab));
  showBanner(navigator.onLine ? "" : "You're offline. Showing the last saved data where available.");
  view.innerHTML = `<p class="muted center">Loading…</p>`;
  try { await fn(new URLSearchParams(qs || "")); }
  catch (e) { view.innerHTML = errorBox(e); }
  view.focus({ preventScroll: true });
}

async function boot() {
  try {
    state.me = await api("/api/me");
    $("#who").textContent = state.me.email;
    $("#tab-new").hidden = state.me.role !== "poster";
    const { companies } = await api("/api/companies");
    const sel = $("#company");
    const names = companies.map((c) => c.name);
    if (!names.includes(state.company)) state.company = state.me.default_company && names.includes(state.me.default_company) ? state.me.default_company : (names[0] || "");
    sel.innerHTML = names.map((n) => `<option ${n === state.company ? "selected" : ""}>${esc(n)}</option>`).join("") || `<option>No company loaded in Tally</option>`;
    sel.addEventListener("change", () => { state.company = sel.value; state.ledgers = []; store.set("company", sel.value); route(); });
  } catch (e) { view.innerHTML = errorBox(e); return; }
  window.addEventListener("hashchange", route);
  window.addEventListener("online", route);
  route();
}

if ("serviceWorker" in navigator) navigator.serviceWorker.register("sw.js").catch(() => {});
boot();
