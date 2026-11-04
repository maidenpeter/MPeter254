"use strict";

/* CargoShield demo dashboard. Plain JS, no build step. */

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const usd = (v, d = 0) => "$" + Number(v || 0).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
const num = (v, d = 0) => Number(v || 0).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
const compactUsd = (v) => {
  v = Number(v || 0);
  if (v >= 1e6) return "$" + (v / 1e6).toFixed(2) + "M";
  if (v >= 1e4) return "$" + (v / 1e3).toFixed(1) + "k";
  return usd(v);
};
const parseIso = (iso) => new Date(iso.length === 10 ? iso + "T00:00:00" : iso);
const day = (iso) => parseIso(iso).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
const dayShort = (iso) => parseIso(iso).toLocaleDateString("en-GB", { day: "numeric", month: "short" });
const stamp = (iso) => parseIso(iso).toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
const resClass = (r) => String(r || "").replace(/\s+/g, "_");
const STATUS = { accepted: "Accepted", in_review: "In review queue", on_hold: "On hold", rejected: "Rejected", new: "New" };

const state = {
  band: "", q: "", sort: "recent", offset: 0, limit: 60, total: 0,
  demo: null, docs: null, liveTimer: null, stats: null, network: null, openId: null,
};

async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let msg = r.statusText;
    try { const j = await r.json(); msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch (_) { /* not json */ }
    throw new Error(msg || "Request failed");
  }
  const ct = r.headers.get("content-type") || "";
  return ct.includes("application/json") ? r.json() : r;
}

function toast(text, ms = 3200) {
  const t = $("#toast");
  t.textContent = text;
  t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { t.hidden = true; }, ms);
}

/* ---------------------------------------------------------------- KPIs */

function setKpi(id, value, prev) {
  const el = $(id);
  if (el.textContent !== value) {
    el.textContent = value;
    if (prev !== undefined) { el.classList.remove("bump"); void el.offsetWidth; el.classList.add("bump"); }
  }
}

async function loadStats() {
  const s = await api("/api/stats");
  const p = state.stats;
  setKpi("#k-screened", num(s.screened), p && p.screened);
  setKpi("#k-flagged", num(s.flagged_high), p && p.flagged_high);
  $("#k-flagged-foot").textContent = `HIGH, sent to a person · ${num(s.flagged_medium)} more MEDIUM`;
  setKpi("#k-policies", num(s.policies_issued), p && p.policies_issued);
  $("#k-policies-foot").textContent = `${compactUsd(s.sum_insured_usd)} top-up cover in force`;
  setKpi("#k-premium", usd(s.premium_written_usd), p && p.premium_written_usd);
  setKpi("#k-fakes", num(s.fakes_caught), p && p.fakes_caught);
  $("#k-fakes-foot").textContent = `from ${num(s.checks_total)} verification checks`;
  $("#review-count").textContent = s.review_pending;
  state.stats = s;
}

/* ---------------------------------------------------------------- bookings */

function scoreHtml(score, band) {
  return `<span class="score"><span class="bar"><i class="${band}" style="width:${Math.max(score, 3)}%"></i></span>` +
    `<b>${score}</b> <span class="pill ${band}">${band}</span></span>`;
}

function rowHtml(b, fresh = false) {
  const reason = b.top_reason
    ? `${esc(b.top_reason)}${b.reason_count > 1 ? ` <span class="more">+${b.reason_count - 1}</span>` : ""}`
    : `<span class="muted">No risk signals</span>`;
  const tags = (b.has_policy ? `<span class="tag cover">Covered</span>` : "") +
    (b.status === "in_review" ? `<span class="tag">Review</span>` : b.status === "on_hold" ? `<span class="tag">On hold</span>` :
      b.status === "rejected" ? `<span class="tag">Rejected</span>` : "");
  return `<tr data-id="${b.id}" class="${fresh ? "fresh" : ""}">
    <td class="mono">${esc(b.awb)}</td>
    <td>${esc(b.origin)} → ${esc(b.destination)}</td>
    <td>${dayShort(b.flight_date)}</td>
    <td>${esc(b.commodity_text)}${tags}</td>
    <td class="num">${num(b.weight_kg, b.weight_kg < 100 ? 1 : 0)} kg</td>
    <td class="num">${usd(b.declared_value_usd)}</td>
    <td>${scoreHtml(b.score, b.band)}</td>
    <td class="reason-cell">${reason}</td>
  </tr>`;
}

async function loadBookings(reset = true) {
  if (reset) state.offset = 0;
  const params = new URLSearchParams({ sort: state.sort, limit: state.limit, offset: state.offset });
  if (state.band) params.set("band", state.band);
  if (state.q) params.set("q", state.q);
  const data = await api("/api/bookings?" + params);
  state.total = data.total;
  const html = data.items.map((b) => rowHtml(b)).join("");
  const tbody = $("#booking-rows");
  if (reset) tbody.innerHTML = html || `<tr><td colspan="8" class="muted">No bookings match.</td></tr>`;
  else tbody.insertAdjacentHTML("beforeend", html);
  state.offset += data.items.length;
  $("#list-count").textContent = `Showing ${num(Math.min(state.offset, state.total))} of ${num(state.total)} bookings`;
  $("#more").hidden = state.offset >= state.total;
}

function matchesFilter(b) {
  if (state.q) return false;
  if (!state.band) return true;
  if (state.band === "FLAGGED") return b.band !== "LOW";
  return b.band === state.band;
}

/* ---------------------------------------------------------------- live feed */

async function liveTick() {
  try {
    const b = await api("/api/demo/simulate", { method: "POST" });
    if (state.sort === "recent" && matchesFilter(b)) {
      $("#booking-rows").insertAdjacentHTML("afterbegin", rowHtml(b, true));
      state.total += 1; state.offset += 1;
      $("#list-count").textContent = `Showing ${num(state.offset)} of ${num(state.total)} bookings`;
    }
    if (b.band === "HIGH") toast(`New HIGH-risk booking ${b.awb} sent to the review queue`);
    await loadStats();
  } catch (e) { toast("Live feed paused: " + e.message); setLive(false); }
}

function setLive(on) {
  $("#live").checked = on;
  $(".live").classList.toggle("on", on);
  clearInterval(state.liveTimer);
  state.liveTimer = on ? setInterval(liveTick, 3500) : null;
  if (on) liveTick();
}

/* ---------------------------------------------------------------- drawer */

function openDrawer() {
  $("#drawer").classList.add("open");
  $("#drawer").setAttribute("aria-hidden", "false");
  $("#scrim").hidden = false;
}

function closeDrawer() {
  $("#drawer").classList.remove("open");
  $("#drawer").setAttribute("aria-hidden", "true");
  $("#scrim").hidden = true;
  state.openId = null;
  if (state.network) { state.network.destroy(); state.network = null; }
}

async function openBooking(id) {
  state.openId = id;
  $("#drawer-body").innerHTML = `<p class="muted">Loading…</p>`;
  openDrawer();
  try {
    const d = await api(`/api/bookings/${id}`);
    if (state.openId !== id) return;
    renderDetail(d);
  } catch (e) {
    $("#drawer-body").innerHTML = `<p>Couldn't load this booking: ${esc(e.message)}</p>`;
  }
}

const BAND_COLOUR = { LOW: "#0f7b4f", MEDIUM: "#b54708", HIGH: "#b42318" };

function renderDetail(d) {
  const b = d.booking, s = d.shipper, r = d.risk, q = d.quote;
  const colour = BAND_COLOUR[r.band];
  const reasons = r.reasons.length
    ? `<ul class="reasons">${r.reasons.map((x) => `<li class="${x.kind}"><span class="pts">+${x.points}</span><span>${esc(x.text)}</span></li>`).join("")}</ul>`
    : `<p class="muted">No rule fired and nothing unusual about this booking.</p>`;
  const routing = r.band === "HIGH"
    ? (b.status === "in_review" ? "Sent to the manual review queue. Not rejected." : `Reviewed: ${STATUS[b.status] || b.status}.`)
    : "Accepted automatically.";

  $("#drawer-body").innerHTML = `
    <div class="d-head">
      <div>
        <div class="d-awb">${esc(b.awb)}</div>
        <div class="d-route">${esc(b.origin)} → ${esc(b.destination)} · flight ${day(b.flight_date)} · ${esc(STATUS[b.status] || b.status)}</div>
      </div>
      <button class="close" id="close" aria-label="Close">×</button>
    </div>

    <div class="card">
      <h4>Risk score</h4>
      <div class="scorebox">
        <div class="dial" style="background:conic-gradient(${colour} ${r.score * 3.6}deg, #eef0f3 0)"><span style="color:${colour}">${r.score}</span></div>
        <div><span class="pill ${r.band}">${r.band}</span><div style="margin-top:6px">${esc(routing)}</div>
          <div class="muted" style="font-size:12px">LOW 0–39 · MEDIUM 40–69 · HIGH 70+. Rules carry the weight; the anomaly model only adds supporting points.</div></div>
      </div>
      <div style="margin-top:10px">${reasons}</div>
    </div>

    <div class="card">
      <h4>Booking</h4>
      <div class="facts">
        <div><span>Goods</span><b>${esc(b.commodity_text)}</b> <span>${esc(b.commodity_label)}</span></div>
        <div><span>Pieces</span><b>${num(b.pieces)}</b></div>
        <div><span>Weight</span><b>${num(b.weight_kg, b.weight_kg < 100 ? 1 : 0)} kg</b></div>
        <div><span>Volume</span><b>${b.volume_m3 < 0.1 ? b.volume_m3.toPrecision(2) : num(b.volume_m3, 2)} m³</b></div>
        <div><span>Density</span><b>${num(b.density_kg_m3)} kg/m³</b></div>
        <div><span>Declared value</span><b>${usd(b.declared_value_usd)}</b></div>
        <div><span>Payment</span><b>${esc(b.payment_label)}</b> ${b.payment_ref ? `<span>${esc(b.payment_ref)}</span>` : ""}</div>
        <div><span>Booked</span><b>${stamp(b.booked_at)}</b></div>
        <div><span>Lead time</span><b>${num(b.lead_time_hours, 0)} h before departure</b></div>
      </div>
    </div>

    <div class="card">
      <h4>Shipper · internal view</h4>
      <div class="facts">
        <div><span>Name</span><b>${esc(s.name)}</b></div>
        <div><span>Country</span><b>${esc(s.country)}</b></div>
        <div><span>KYC</span><b>${esc(s.kyc_status)}</b></div>
        <div><span>Account age at booking</span><b>${num(s.account_age_days_at_booking)} days</b></div>
        <div><span>Bookings</span><b>${num(s.bookings)}</b></div>
        <div><span>Contact</span><b>${esc(s.phone)}</b><span>${esc(s.email)}</span></div>
      </div>
      ${s.flagged ? `<div class="ring-note" style="margin-top:10px">Flagged internally: ${esc(s.flag_reason)}</div>` : ""}
    </div>

    <div class="card">
      <h4>Connections · shared phones, emails and payer accounts</h4>
      <div id="graph-summary" class="muted">Loading the fraud graph…</div>
      <div class="graph" id="graph"></div>
      <div class="legend"><span><i style="background:#d92d20"></i>flagged shipper</span><span><i style="background:#0e5e6f;box-shadow:0 0 0 2px #101828"></i>this shipper (thick outline)</span>
        <span><i style="background:#98a2b3"></i>other shipper</span><span><i style="background:#7a5af8;border-radius:2px"></i>phone</span>
        <span><i style="background:#2e90fa;border-radius:2px"></i>email</span><span><i style="background:#f79009;border-radius:2px"></i>payer account</span></div>
    </div>

    <div class="card" id="cover-card">${coverHtml(d)}</div>

    ${d.reviews.length ? `<div class="card"><h4>Review history</h4>${d.reviews.map((x) =>
      `<div class="decision"><b>${esc(x.decision)}</b><span>${esc(x.note || "")}</span><span class="muted">${esc(x.decided_by)} · ${stamp(x.decided_at)}</span></div>`).join("")}</div>` : ""}
  `;
  $("#close").addEventListener("click", closeDrawer);
  bindCover(d);
  drawGraph(s.id);
}

function coverHtml(d) {
  const q = d.quote, p = d.policy;
  if (p) {
    return `<h4>Cover · issued</h4>
      <div class="policy">
        <img src="${p.qr_png}" alt="QR code that opens the verification page for this cover">
        <div class="actions">
          <div><b>${esc(p.policy_no)}</b> · active</div>
          <div class="muted">Sum insured ${usd(p.sum_insured_usd, 2)} · premium ${usd(p.premium_usd, 2)}</div>
          <button class="btn primary small" id="scan-qr">Scan the QR (opens what a phone would)</button>
          <button class="btn small" id="send-cert">Send the certificate to the chat</button>
          <a class="link" href="${p.certificate_png}" target="_blank" rel="noopener">Open the certificate</a>
        </div>
      </div>
      <p class="disclaimer">QR links to ${esc(p.verify_url.replace(/\/verify\/policy\/.*/, "/verify/policy/…"))}. Illustrative cover for the demo, not a real policy.</p>`;
  }
  if (!q.eligible) {
    return `<h4>Cover</h4><div class="notice">${esc(q.reason || "Not eligible for cover.")}</div>`;
  }
  const rows = q.steps.map((st, i) => {
    const isTotal = i === q.steps.length - 1;
    const sign = ["Expense loading", "Profit margin"].includes(st.label) ? "+ " : st.label === "Carrier liability" ? "− " : "";
    return `<tr class="${isTotal ? "total" : ""}"><td>${sign}${esc(st.label)}</td><td class="f">${esc(st.formula)}</td><td class="num">${usd(st.value, 2)}</td></tr>`;
  }).join("");
  return `<h4>Cover · top-up quote (${esc(q.band)} band)</h4>
    <table class="quote">${rows}</table>
    <p class="disclaimer">${esc(q.disclaimer)} Rate on sum insured: ${(q.rate_on_sum_insured * 100).toFixed(3)}%.
      Carrier liability uses the Montreal Convention limit in config; verify it before any real use.</p>
    <button class="btn primary" id="issue">Issue cover for ${usd(q.premium_usd, 2)}</button>`;
}

function bindCover(d) {
  const issue = $("#issue");
  if (issue) {
    issue.addEventListener("click", async () => {
      issue.disabled = true;
      try {
        await api("/api/policies", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ booking_id: d.booking.id }) });
        toast("Cover issued. Scan the QR to check it.");
        const fresh = await api(`/api/bookings/${d.booking.id}`);
        $("#cover-card").innerHTML = coverHtml(fresh);
        bindCover(fresh);
        loadStats();
        loadBookings();
      } catch (e) { toast(e.message); issue.disabled = false; }
    });
  }
  const scan = $("#scan-qr");
  if (scan) scan.addEventListener("click", () => window.open(d.policy.verify_url, "_blank", "noopener"));
  const send = $("#send-cert");
  if (send) send.addEventListener("click", async () => {
    const blob = await (await fetch(d.policy.certificate_png)).blob();
    closeDrawer();
    showTab("verify");
    sendImage(blob, `${d.policy.policy_no}.png`);
  });
}

async function drawGraph(shipperId) {
  let g;
  try { g = await api(`/graph/shipper/${shipperId}?hops=2`); } catch (e) { $("#graph-summary").textContent = e.message; return; }
  const sm = g.summary;
  const summary = $("#graph-summary");
  if (!summary) return;
  if (sm.connected_shippers === 0) {
    summary.textContent = "No shared phone, email or payer account with any other shipper.";
    $("#graph").classList.add("empty");
    $(".legend").hidden = true;
    return;
  } else {
    summary.innerHTML = `${sm.connected_shippers} connected shipper${sm.connected_shippers === 1 ? "" : "s"} within 2 hops through ${sm.shared_identifiers} shared identifier${sm.shared_identifiers === 1 ? "" : "s"}` +
      (sm.flagged_shippers ? ` · <b style="color:#b42318">${sm.flagged_shippers} flagged</b>` : ` · none flagged (a shared agent number on its own is not a reason to flag anyone)`);
  }
  const box = $("#graph");
  if (!window.vis || !box) { box.innerHTML = `<p class="muted" style="padding:10px">Graph view unavailable.</p>`; return; }
  const ID_COLOUR = { phone: "#7a5af8", email: "#2e90fa", payment_ref: "#f79009" };
  const nodes = g.nodes.map((n) => {
    if (n.kind === "shipper") {
      const bg = n.flagged ? "#d92d20" : n.is_center ? "#0e5e6f" : "#98a2b3";
      return {
        id: n.id, label: n.label.length > 26 ? n.label.slice(0, 24) + "…" : n.label, shape: "dot", size: n.is_center ? 22 : 15,
        color: { background: bg, border: n.is_center ? "#101828" : bg, highlight: { background: bg, border: "#101828" } },
        borderWidth: n.is_center ? 4 : 1, font: { size: 15, color: "#101828", strokeWidth: 4, strokeColor: "#fbfcfd" },
        title: `${n.label} (${n.country})\nKYC: ${n.kyc_status}\nBookings: ${n.bookings}, highest score ${n.max_score ?? "–"}` + (n.flagged ? `\nFlagged: ${n.flag_reason}` : ""),
      };
    }
    return { id: n.id, label: n.label, shape: "diamond", size: 9, color: ID_COLOUR[n.kind], font: { size: 12, color: "#475467" },
      title: `${n.kind === "payment_ref" ? "Payer account" : n.kind} shared by ${n.shared_by} shippers` };
  });
  const edges = g.edges.map((e) => ({ from: e.from, to: e.to, color: { color: "#c3c9d2" }, width: 1.4 }));
  if (state.network) state.network.destroy();
  state.network = new vis.Network(box, { nodes, edges }, {
    physics: { solver: "forceAtlas2Based", forceAtlas2Based: { gravitationalConstant: -45, springLength: 70 }, stabilization: { iterations: 250 } },
    interaction: { hover: true, tooltipDelay: 120 },
  });
  state.network.once("stabilizationIterationsDone", () => {
    if (!state.network) return;
    state.network.setOptions({ physics: false });  // stop drifting once laid out
    state.network.fit({ animation: false, maxZoomLevel: 1.3 });
  });
  state.network.on("doubleClick", (p) => {
    const id = p.nodes && p.nodes[0];
    if (id && id.startsWith("shipper:")) toast("Shipper " + (g.nodes.find((n) => n.id === id) || {}).label);
  });
}

/* ---------------------------------------------------------------- review queue */

async function loadReview() {
  const [q, demo] = await Promise.all([api("/api/review-queue"), state.demo ? Promise.resolve(state.demo) : api("/api/demo")]);
  state.demo = demo;
  const e = demo.engine_check;
  $("#engine-check").innerHTML = `<b>Engine check on this demo data:</b> caught <b>${e.planted_caught} of ${e.planted_total}</b> planted risks
    (${e.planted_high} of them HIGH). False positives: <b>${e.clean_flagged_medium_plus} of ${num(e.clean_total)}</b> clean bookings
    (${(e.fp_rate_medium_plus * 100).toFixed(2)}%) scored MEDIUM or higher, ${e.clean_flagged_high} reached HIGH.`;
  $("#review-list").innerHTML = q.items.length ? q.items.map((b) => `
    <div class="rcard ${b.status}" data-id="${b.id}">
      <div class="rcard-head">
        <div><span class="mono"><b>${esc(b.awb)}</b></span> · ${esc(b.origin)} → ${esc(b.destination)} · ${dayShort(b.flight_date)}</div>
        ${scoreHtml(b.score, b.band)}
      </div>
      <div class="muted">${esc(b.commodity_text)} · ${num(b.weight_kg, b.weight_kg < 100 ? 1 : 0)} kg · ${usd(b.declared_value_usd)}${b.status === "on_hold" ? " · <b>on hold</b>" : ""}</div>
      <ul>${b.reasons.filter((x) => x.kind === "rule").map((x) => `<li>${esc(x.text)}</li>`).join("")}</ul>
      <button class="link open">Open booking and fraud graph</button>
      <textarea placeholder="Note for the file (required to reject)" maxlength="500"></textarea>
      <div class="row">
        <button class="btn small approve" data-d="approve">Approve</button>
        <button class="btn small hold" data-d="hold">Hold</button>
        <button class="btn small reject" data-d="reject">Reject</button>
      </div>
    </div>`).join("") : `<p class="muted">Nothing waiting for review.</p>`;
  $("#review-decisions").innerHTML = q.recent_decisions.length ? q.recent_decisions.map((x) =>
    `<div class="decision"><b>${esc(x.decision)}</b><span class="mono">${esc(x.awb)}</span><span>${esc(x.note || "")}</span><span class="muted">${stamp(x.decided_at)}</span></div>`).join("")
    : `<p class="muted">No decisions yet.</p>`;
}

async function decide(card, decision) {
  const id = card.dataset.id;
  const note = $("textarea", card).value.trim();
  try {
    await api(`/api/review/${id}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ decision, note }) });
    toast(`Booking ${decision === "approve" ? "approved" : decision === "hold" ? "put on hold" : "rejected"}.`);
    await Promise.all([loadReview(), loadStats()]);
  } catch (e) { toast(e.message); }
}

/* ---------------------------------------------------------------- chat simulator */

function nowTime() { return new Date().toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" }); }

function addMsg(who, text, opts = {}) {
  const el = document.createElement("div");
  el.className = `msg ${who} ${resClass(opts.result || "")}`;
  if (opts.img) { const im = document.createElement("img"); im.src = opts.img; im.alt = "photo sent"; el.appendChild(im); }
  if (text) el.appendChild(document.createTextNode(text));
  const t = document.createElement("span"); t.className = "time"; t.textContent = nowTime(); el.appendChild(t);
  $("#chat").appendChild(el);
  $("#chat").scrollTop = $("#chat").scrollHeight;
  return el;
}

async function chatSend(form) {
  const typing = addMsg("bot typing", "checking…");
  try {
    const r = await api("/api/chat", { method: "POST", body: form });
    typing.remove();
    addMsg("bot", r.text, { result: r.result });
  } catch (e) { typing.remove(); addMsg("bot", "Sorry, something went wrong: " + e.message); }
  loadStats(); loadLog();
}

function sendText(text) {
  if (!text.trim()) return;
  addMsg("me", text);
  const f = new FormData(); f.append("text", text);
  chatSend(f);
}

function sendImage(blob, name) {
  const url = URL.createObjectURL(blob);
  addMsg("me", "", { img: url });
  const f = new FormData(); f.append("image", blob, name || "photo.png");
  chatSend(f);
}

async function loadDocs() {
  if (!state.docs) state.docs = await api("/api/documents");
  const all = [...state.docs.fakes.map((d) => ({ ...d, label: "Fake" })), ...state.docs.samples.map((d) => ({ ...d, label: "Genuine" }))];
  $("#docs").innerHTML = all.map((d) => `
    <button class="doc" data-url="${esc(d.url)}" data-name="${esc(d.name)}" title="${esc(d.story)}">
      <img src="${esc(d.url)}" alt="${esc(d.name)}" loading="lazy">
      <div class="doc-name">${esc(d.label)} ${d.kind === "certificate" ? "certificate" : "air waybill"}</div>
      <div class="doc-story">${esc(d.story)}</div>
    </button>`).join("");
}

async function loadLog() {
  if ($("#tab-verify").hidden) return;
  const rows = await api("/api/verifications?limit=14");
  $("#log-rows").innerHTML = rows.map((r) => `<tr><td>${stamp(r.checked_at)}</td><td>${esc(r.channel)}</td>
    <td class="mono">${esc(r.input_value || "")}</td><td><span class="res ${resClass(r.result)}">${esc(r.result)}</span></td></tr>`).join("");
}

async function greet() {
  $("#chat").innerHTML = "";
  const f = new FormData(); f.append("text", "hi");
  const r = await api("/api/chat", { method: "POST", body: f });
  addMsg("bot", r.text);
}

/* ---------------------------------------------------------------- tabs, path, reset */

function showTab(name) {
  $$(".tab").forEach((t) => { const on = t.dataset.tab === name; t.classList.toggle("active", on); t.setAttribute("aria-selected", on); });
  $$(".panel").forEach((p) => { p.hidden = p.id !== "tab-" + name; });
  if (name === "review") loadReview();
  if (name === "verify") { loadDocs(); loadLog(); }
}

async function ensureDemo() {
  if (!state.demo) state.demo = await api("/api/demo");
  return state.demo;
}

async function runStep(step) {
  const demo = await ensureDemo();
  $("#path-panel").classList.add("compact");
  if (step === "1") {
    closeDrawer();
    showTab("verify");
    await loadDocs();
    $$(".doc").forEach((d) => d.classList.toggle("highlight", d.dataset.name === demo.demo_fake));
    const el = $(`.doc[data-name="${demo.demo_fake}"]`);
    if (el) el.scrollIntoView({ behavior: "smooth", block: "center" });
    toast("Click the highlighted document to send it into the chat.");
  } else if (step === "2") {
    showTab("bookings");
    openBooking(demo.hero_high_booking_id);
  } else if (step === "3") {
    showTab("bookings");
    openBooking(demo.hero_clean_booking_id);
  }
}

async function init() {
  // tabs
  $$(".tab").forEach((t) => t.addEventListener("click", () => showTab(t.dataset.tab)));

  // filters
  let searchTimer;
  $("#search").addEventListener("input", (e) => { clearTimeout(searchTimer); searchTimer = setTimeout(() => { state.q = e.target.value.trim(); loadBookings(); }, 250); });
  $$("#band-filter button").forEach((b) => b.addEventListener("click", () => {
    $$("#band-filter button").forEach((x) => x.classList.toggle("on", x === b));
    state.band = b.dataset.band; loadBookings();
  }));
  $("#sort").addEventListener("change", (e) => { state.sort = e.target.value; loadBookings(); });
  $("#more").addEventListener("click", () => loadBookings(false));
  $("#booking-rows").addEventListener("click", (e) => { const tr = e.target.closest("tr[data-id]"); if (tr) openBooking(Number(tr.dataset.id)); });
  $("#live").addEventListener("change", (e) => setLive(e.target.checked));

  // drawer
  $("#scrim").addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });

  // review
  $("#review-list").addEventListener("click", (e) => {
    const card = e.target.closest(".rcard"); if (!card) return;
    if (e.target.matches(".open")) openBooking(Number(card.dataset.id));
    if (e.target.dataset.d) decide(card, e.target.dataset.d);
  });

  // chat
  $("#composer").addEventListener("submit", (e) => { e.preventDefault(); const v = $("#msg").value; $("#msg").value = ""; sendText(v); });
  $("#file").addEventListener("change", (e) => { const f = e.target.files[0]; if (f) sendImage(f, f.name); e.target.value = ""; });
  $("#docs").addEventListener("click", async (e) => {
    const d = e.target.closest(".doc"); if (!d) return;
    d.classList.remove("highlight");
    const blob = await (await fetch(d.dataset.url)).blob();
    sendImage(blob, d.dataset.name);
  });
  const phone = $("#phone");
  ["dragenter", "dragover"].forEach((ev) => phone.addEventListener(ev, (e) => { e.preventDefault(); phone.classList.add("dragging"); }));
  ["dragleave", "drop"].forEach((ev) => phone.addEventListener(ev, (e) => { e.preventDefault(); phone.classList.remove("dragging"); }));
  phone.addEventListener("drop", (e) => { const f = e.dataTransfer.files[0]; if (f && f.type.startsWith("image/")) sendImage(f, f.name); });

  // presenter
  $("#btn-path").addEventListener("click", async () => {
    const panel = $("#path-panel"); panel.hidden = !panel.hidden; panel.classList.remove("compact");
    $("#btn-path").setAttribute("aria-expanded", String(!panel.hidden));
    const demo = await ensureDemo();
    $("#path-high").textContent = demo.hero_high_awb; $("#path-clean").textContent = demo.hero_clean_awb;
  });
  $$("#path-panel [data-step]").forEach((b) => b.addEventListener("click", () => runStep(b.dataset.step)));
  $("#path-close").addEventListener("click", () => { $("#path-panel").hidden = true; $("#btn-path").setAttribute("aria-expanded", "false"); });
  $("#path-expand").addEventListener("click", () => $("#path-panel").classList.remove("compact"));
  $("#btn-reset").addEventListener("click", async () => {
    if (!confirm("Reset the demo to its starting state? Issued cover, reviews, live bookings and chat checks will be cleared.")) return;
    setLive(false);
    await api("/api/admin/reset", { method: "POST" });
    state.demo = null; state.docs = null; state.stats = null;
    closeDrawer();
    await Promise.all([loadStats(), loadBookings(), greet()]);
    if (!$("#tab-review").hidden) loadReview();
    if (!$("#tab-verify").hidden) loadLog();
    toast("Demo reset.");
  });

  try {
    const demo = await ensureDemo();
    $("#airline").textContent = demo.airline;
    await Promise.all([loadStats(), loadBookings(), greet()]);
  } catch (e) {
    $("#booking-rows").innerHTML = `<tr><td colspan="8">Couldn't reach the demo database: ${esc(e.message)}. Run <code>make seed</code>, then reload.</td></tr>`;
  }
  setInterval(() => { loadStats().catch(() => {}); loadLog().catch(() => {}); }, 6000);
}

document.addEventListener("DOMContentLoaded", init);
