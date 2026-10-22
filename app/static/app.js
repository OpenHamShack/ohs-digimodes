"use strict";
/* OpenHamShack Digital Modes: the page.

   Polls api/state once a second (app/main.py says why not a socket), keeps
   the decodes it has been given, and draws WSJT-X's two lists: everything
   heard (band activity) and what is near the RX frequency or to and from
   this station. A click on a decode replies to it; the server decides what
   that means (sequencer.py), so the page holds no QSO logic of its own.

   Every word comes from static/i18n/en.json through t(); every frequency
   through ShackI18n.fmt, so the decimal mark is the reader's. */

const $ = (id) => document.getElementById(id);
const t = (key, vars) => window.ShackI18n.t(key, vars);
const fmt = () => window.ShackI18n.fmt;

const state = { lastId: 0, decodes: [], s: null, freqs: [], lastSlot: null };
const MAX = 400;
const NEAR_HZ = 60;

async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const r = await fetch(path, opts);
  let data = {};
  try { data = await r.json(); } catch (e) { /* not JSON: an error page */ }
  if (!r.ok) throw new Error(data.error || t("msg.http", { code: r.status }));
  return data;
}

function say(text) { $("message").textContent = text || ""; }

async function act(path, body) {
  try { const out = await api(path, body); say(""); await poll(); return out; } catch (e) { say(e.message); return null; }
}

function mhz(hz) { return fmt().frequency(hz, { unit: "MHz", decimals: 3 }); }

function row(d) {
  const tr = document.createElement("tr");
  tr.dataset.id = d.id;
  if (d.toMe) tr.className = "me"; else if (d.cq) tr.className = "cq";
  for (const v of [d.utc, String(d.snr), fmt().number(d.dt, { decimals: 1 }), String(d.freq), d.message]) {
    const td = document.createElement("td");
    td.textContent = v;
    tr.appendChild(td);
  }
  return tr;
}

function separator() {
  const tr = document.createElement("tr");
  tr.className = "sep";
  tr.appendChild(document.createElement("td")).colSpan = 5;
  return tr;
}

function drawLists() {
  const s = state.s;
  const rxHz = s.operating.rxHz;
  const my = (s.station.callsign || "").toUpperCase();
  const dx = s.sequencer.contact ? s.sequencer.contact.call : "";
  const band = $("band"), rx = $("rx");
  band.textContent = ""; rx.textContent = "";
  let slot = null;
  for (const d of state.decodes) {
    if (slot !== null && d.slot !== slot) band.appendChild(separator());
    slot = d.slot;
    band.appendChild(row(d));
    const words = d.message.split(/\s+/);
    const near = Math.abs(d.freq - rxHz) <= NEAR_HZ;
    const ours = (my && words.includes(my)) || (dx && words.includes(dx));
    if (near || ours) rx.appendChild(row(d));
  }
  for (const pane of [band, rx]) pane.parentElement.parentElement.scrollTop = 1e9;
  $("rxlabel").textContent = t("pane.rxAt", { hz: rxHz });
}

function drawControls() {
  const s = state.s;
  const op = s.operating;
  if (document.activeElement !== $("rxHz")) $("rxHz").value = op.rxHz;
  if (document.activeElement !== $("txHz")) $("txHz").value = op.txHz;
  $("mode").value = op.mode;

  const src = $("source");
  const kinds = { radio: "src.radio", soundcard: "src.soundcard", wav: "src.wav", none: "src.none" };
  const states = { connecting: "srcState.connecting", stopped: "srcState.stopped", ended: "srcState.ended",
                   none: "srcState.none", receiving: "srcState.receiving", error: "srcState.error" };
  src.textContent = s.source.state === "error"
    ? t("src.error", { why: s.source.error })
    : t("src.withState", { source: t(kinds[s.source.kind] || "src.none"), state: t(states[s.source.state] || "srcState.stopped") });
  src.className = "pill " + (s.source.state === "receiving" ? "ok" : s.source.state === "error" ? "bad" : "");
  if (!s.decoder.ok) { src.textContent = t("src.noDecoder", { why: s.decoder.error }); src.className = "pill bad"; }

  const seq = s.sequencer;
  const sending = s.tx.sending;
  const tx = $("txstate");
  if (sending) { tx.textContent = t("tx.sending", { message: sending.message }); tx.className = "pill onair"; }
  else if (seq.enabled) { tx.textContent = t("tx.armed"); tx.className = "pill ok"; }
  else { const stops = { watchdog: "tx.stopped.watchdog", noreply: "tx.stopped.noreply" };
    tx.textContent = stops[seq.stopped] ? t(stops[seq.stopped]) : t("tx.idle"); tx.className = "pill"; }

  const c = seq.contact;
  $("contact").textContent = c ? t("tx.contact", { call: c.call, grid: c.grid || "-", sent: c.rstSent || "-", rcvd: c.rstRcvd || "-" }) : "";

  const list = $("txlist");
  list.textContent = "";
  for (const n of ["1", "2", "3", "4", "5", "6"]) {
    const text = seq.messages[n];
    if (!text) continue;
    const li = document.createElement("li");
    li.textContent = t("tx.slot", { n, text });
    if (String(seq.step) === n) li.className = "next";
    list.appendChild(li);
  }
  $("enable").classList.toggle("on", seq.enabled);
  $("enable").textContent = seq.enabled ? t("ctl.enabled") : t("ctl.enable");

  const why = s.tx.refusal || s.tx.error;
  const refusal = $("refusal");
  refusal.hidden = !why;
  refusal.textContent = why ? t("tx.refusal", { why }) : "";
  for (const id of ["cq", "enable"]) $(id).disabled = Boolean(s.tx.refusal) || state.level !== "transmit";
  for (const id of ["halt", "txToRx", "logBtn", "dismissBtn"]) $(id).disabled = state.level === "view";

  const p = s.pending;
  const pending = $("pending");
  if (p && pending.hidden) {
    $("pCall").value = p.call; $("pGrid").value = p.grid; $("pSent").value = p.rstSent;
    $("pRcvd").value = p.rstRcvd; $("pComment").value = "";
  }
  pending.hidden = !p;

  // The slot clock: the second within the slot, and which half (even/odd).
  const date = new Date(s.now * 1000);
  $("utc").textContent = date.toISOString().slice(11, 19);
  const bar = $("slotbar");
  bar.style.width = Math.min(100, (100 * s.slotSecond) / s.period) + "%";
  bar.classList.toggle("odd", s.slotParity === 1);
}

function drawDials() {
  const sel = $("dial");
  const op = state.s.operating;
  const rows = state.freqs.filter((f) => f.mode === op.mode);
  const key = rows.map((f) => f.dialHz).join(",") + op.dialHz;
  if (sel.dataset.key === key) return;
  sel.dataset.key = key;
  sel.textContent = "";
  for (const f of rows) {
    const o = document.createElement("option");
    o.value = f.dialHz;
    o.textContent = f.tx ? t("dial.row", { band: f.band, mhz: mhz(f.dialHz) })
                         : t("dial.rowRx", { band: f.band, mhz: mhz(f.dialHz) });
    if (!f.tx) o.title = f.why;
    sel.appendChild(o);
  }
  if (!rows.some((f) => f.dialHz === op.dialHz)) {
    const o = document.createElement("option");
    o.value = op.dialHz;
    o.textContent = t("dial.other", { mhz: mhz(op.dialHz) });
    sel.appendChild(o);
  }
  sel.value = op.dialHz;
}

async function drawLog() {
  try {
    const { qsos } = await api("api/log");
    const body = $("qsos");
    body.textContent = "";
    for (const q of qsos.slice(0, 20)) {
      const tr = document.createElement("tr");
      const mode = q.submode || q.mode || "";
      for (const v of [(q.start || "").replace("T", " ").slice(0, 16), q.call, q.band || "", mode,
                       q.rst_sent || "", q.rst_rcvd || "", q.gridsquare || ""]) {
        const td = document.createElement("td"); td.textContent = v; tr.appendChild(td);
      }
      body.appendChild(tr);
    }
  } catch (e) { /* the log is a nicety; the message line has the poll's errors */ }
}

async function poll() {
  let s;
  try { s = await api("api/state?since=" + state.lastId); } catch (e) { say(e.message); return; }
  if (s.lastId < state.lastId) { state.decodes = []; }   // the server restarted
  state.decodes.push(...s.decodes);
  if (state.decodes.length > MAX) state.decodes.splice(0, state.decodes.length - MAX);
  state.lastId = s.lastId;
  const modeChanged = !state.s || state.s.operating.mode !== s.operating.mode;
  state.s = s;
  if (modeChanged || !state.freqs.length) {
    try { state.freqs = (await api("api/frequencies")).frequencies; } catch (e) { state.freqs = []; }
  }
  drawDials();
  drawControls();
  if (s.decodes.length) drawLists();
  const slot = s.lastSlot && s.lastSlot.slot;
  if (slot !== state.lastSlot) { state.lastSlot = slot; drawLog(); drawLists(); }
}

function openSettings() {
  const s = state.s;
  const form = $("settingsForm");
  for (const r of form.elements.source) r.checked = r.value === s.operating.source;
  $("radioUrl").value = s.operating.radioUrl || "";
  $("scRow").hidden = s.installation.form !== "desktop";
  const st = s.station;
  $("callsign").value = st.callsign; $("locator").value = st.locator; $("country").value = st.country;
  $("licence").value = (st.licence && st.licence.class) || "";
  for (const id of ["callsign", "locator", "country", "licence"]) $(id).disabled = !st.editable;
  $("stationNote").textContent = st.editable ? t("set.stationLocal") : t("set.stationController");
  const desktop = s.installation.form === "desktop";
  $("txSwitch").textContent = s.installation.allowTransmit ? t("set.txOn") : desktop ? "" : t("set.txOff");
  $("txAllowRow").hidden = !desktop || s.installation.allowTransmit;
  $("allowTx").checked = Boolean(s.operating.allowTransmit);
  if (s.installation.form === "desktop") {
    api("api/devices").then(({ devices }) => {
      const sel = $("soundDevice");
      sel.textContent = "";
      const def = document.createElement("option"); def.value = ""; def.textContent = t("set.defaultInput");
      sel.appendChild(def);
      for (const d of devices) {
        const o = document.createElement("option"); o.value = String(d.id); o.textContent = d.name; sel.appendChild(o);
      }
      sel.value = s.operating.soundDevice || "";
    }).catch(() => {});
  }
  $("settings").showModal();
}

async function saveSettings(ev) {
  ev.preventDefault();
  const form = $("settingsForm");
  const source = [...form.elements.source].find((r) => r.checked);
  const cfg = { source: source ? source.value : "none", radioUrl: $("radioUrl").value.trim(),
                soundDevice: $("soundDevice").value };
  if (state.s.installation.form === "desktop") cfg.allowTransmit = $("allowTx").checked;
  await act("api/config", cfg);
  if (state.s.station.editable) {
    const lic = $("licence").value.trim();
    await act("api/profile", { callsign: $("callsign").value.trim(), locator: $("locator").value.trim(),
                               country: $("country").value.trim().toUpperCase(),
                               licence: lic ? { class: lic } : {} });
  }
  $("settings").close();
}

function wire() {
  $("mode").addEventListener("change", (e) => act("api/config", { mode: e.target.value }));
  $("dial").addEventListener("change", (e) => act("api/config", { dialHz: Number(e.target.value) }));
  $("rxHz").addEventListener("change", (e) => act("api/config", { rxHz: Number(e.target.value) }));
  $("txHz").addEventListener("change", (e) => act("api/config", { txHz: Number(e.target.value) }));
  $("txToRx").addEventListener("click", () => act("api/config", { txHz: state.s.operating.rxHz }));
  $("cq").addEventListener("click", () => act("api/cq", {}));
  $("enable").addEventListener("click", () => act("api/tx", { on: !state.s.sequencer.enabled }));
  $("halt").addEventListener("click", () => act("api/halt", {}));
  $("logBtn").addEventListener("click", () => act("api/log", {
    call: $("pCall").value, gridsquare: $("pGrid").value, rst_sent: $("pSent").value,
    rst_rcvd: $("pRcvd").value, comment: $("pComment").value }).then((r) => {
      if (r) { say(t("log.done", { call: r.fields.call })); drawLog(); } }));
  $("dismissBtn").addEventListener("click", () => act("api/log/dismiss", {}));
  $("settingsBtn").addEventListener("click", openSettings);
  $("saveSettings").addEventListener("click", saveSettings);
  for (const id of ["band", "rx"]) {
    $(id).addEventListener("click", (e) => {
      const tr = e.target.closest("tr[data-id]");
      if (tr) act("api/reply", { id: Number(tr.dataset.id) });
    });
  }
}

window.ShackI18n.ready.then(async () => {
  try { state.level = (await api("api/me")).level; } catch (e) { state.level = "view"; }
  wire();
  await poll();
  setInterval(poll, 1000);
});
