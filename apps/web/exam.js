/* Student exam runner (Phase 11).
 *
 * Drives one attempt in the browser against the same-origin /exam delivery API:
 * pick-your-name from the share link, start-or-resume the single attempt, serve
 * one item at a time, save answers, run a DISPLAY-ONLY countdown, and submit.
 *
 * Golden rules on the client:
 *   #1  Never request or expect an answer key. The runner only ever consumes the
 *       keyless `ClientTest` projection; there is no `correct` anywhere here.
 *   #3  The timer is display-only. The server's /state is the single source of
 *       truth for time remaining and for expiry; when it says expired (or the
 *       local countdown reaches zero), the attempt finalizes — leaving the tab
 *       never pauses it (that's enforced server-side; we only render it).
 *
 * Integrity (capture, never judge — golden rule #6): request fullscreen where
 * supported (skipped on iOS), block copy/cut/paste/contextmenu, and wire the
 * Phase-6 recorder so visibility/blur/answer/audio events reach the ingest sink.
 */
"use strict";

const ACTIVE_KEY = "exam:active"; // {testId, attemptId, rosterEntryId, name}
const $ = (sel) => document.querySelector(sel);

const params = new URLSearchParams(location.search);
const linkTestId = params.get("test");

let rec = null; // telemetry recorder
let flat = []; // [{section, item}] in served order
let idx = 0;
const answers = {}; // item_id -> last value sent (for repopulating on nav)
let serverDeadline = null; // ms epoch, from /state (authoritative)
let tickTimer = null;
let pollTimer = null;
let finished = false;

// -------------------------------------------------------------------------- //
// API helper.
// -------------------------------------------------------------------------- //
async function api(path, { method = "GET", body } = {}) {
  const resp = await fetch(`/exam${path}`, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!resp.ok) {
    const err = new Error(`${method} ${path} -> ${resp.status}`);
    err.status = resp.status;
    throw err;
  }
  return resp.status === 204 ? null : resp.json();
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function getActive() {
  try {
    return JSON.parse(localStorage.getItem(ACTIVE_KEY) || "null");
  } catch {
    return null;
  }
}

// -------------------------------------------------------------------------- //
// Pick-your-name landing.
// -------------------------------------------------------------------------- //
async function showPicker() {
  if (!linkTestId) {
    $("#pickEmpty").textContent = "This link is missing its test id.";
    return;
  }
  let roster;
  try {
    roster = await api(`/tests/${encodeURIComponent(linkTestId)}/roster`);
  } catch (e) {
    $("#pickErr").textContent = "Could not load the class list. Try again.";
    return;
  }
  const list = $("#nameList");
  list.innerHTML = "";
  $("#pickEmpty").classList.toggle("hidden", roster.length > 0);
  for (const r of roster) {
    const btn = document.createElement("button");
    btn.className = "name-btn" + (r.status === "submitted" ? " done" : "");
    btn.textContent = r.display_name + (r.status === "submitted" ? "  (done)" : "");
    btn.disabled = r.status === "submitted";
    btn.onclick = () => begin(r);
    list.appendChild(btn);
  }
}

async function begin(rosterEntry) {
  $("#pickErr").textContent = "";
  let started;
  try {
    started = await api(`/roster/${encodeURIComponent(rosterEntry.roster_entry_id)}/start`, {
      method: "POST",
    });
  } catch (e) {
    $("#pickErr").textContent = "Could not start the test. Try again.";
    return;
  }
  localStorage.setItem(
    ACTIVE_KEY,
    JSON.stringify({
      testId: started.test_id,
      attemptId: started.attempt_id,
      rosterEntryId: rosterEntry.roster_entry_id,
      name: rosterEntry.display_name,
    })
  );
  await enterExam(started.attempt_id, started.state);
}

// -------------------------------------------------------------------------- //
// Enter the exam (fresh start or refresh-resume).
// -------------------------------------------------------------------------- //
async function enterExam(attemptId, state) {
  if (state && (state.status === "submitted" || state.expired)) {
    return showDone(state.status === "submitted");
  }

  let test;
  try {
    test = await api(`/attempts/${encodeURIComponent(attemptId)}/test`);
  } catch (e) {
    if (e.status === 404) clearActiveAndRestart();
    return;
  }

  flat = [];
  for (const section of test.sections) {
    for (const item of section.items) flat.push({ section, item });
  }
  idx = 0;

  $("#pick").classList.add("hidden");
  $("#examHeader").classList.remove("hidden");
  $("#runner").classList.remove("hidden");
  $("#examTitle").textContent = test.title;

  startIntegrity(attemptId);
  if (state) syncDeadline(state);
  startTimers(attemptId);
  renderCurrent(attemptId);
}

function clearActiveAndRestart() {
  localStorage.removeItem(ACTIVE_KEY);
  location.search = linkTestId ? `?test=${encodeURIComponent(linkTestId)}` : "";
}

// -------------------------------------------------------------------------- //
// Integrity: fullscreen (skip iOS), block clipboard/context menu, telemetry.
// -------------------------------------------------------------------------- //
function isIOS() {
  return (
    /iP(hone|od|ad)/.test(navigator.platform) ||
    (navigator.userAgent.includes("Mac") && "ontouchend" in document) // iPadOS
  );
}

function startIntegrity(attemptId) {
  rec = window.ExamRecorder.start({ attemptId });

  // Discourage copy-paste assistance; capture the attempt as an interaction.
  for (const ev of ["copy", "cut", "paste", "contextmenu"]) {
    document.addEventListener(ev, (e) => {
      e.preventDefault();
      const item = flat[idx];
      if (rec && item) rec.interaction(item.item.id, { blocked: ev });
    });
  }

  // Request fullscreen on the first user gesture, where supported. iOS Safari
  // has no element fullscreen, so we skip it there and degrade gracefully.
  if (!isIOS()) {
    const goFull = () => {
      const el = document.documentElement;
      if (el.requestFullscreen) el.requestFullscreen().catch(() => {});
      document.removeEventListener("click", goFull);
    };
    document.addEventListener("click", goFull, { once: true });
  }
}

// -------------------------------------------------------------------------- //
// Server-authoritative timer (display-only render; server decides expiry).
// -------------------------------------------------------------------------- //
function syncDeadline(state) {
  serverDeadline = state.deadline ? new Date(state.deadline).getTime() : null;
}

function startTimers(attemptId) {
  renderTimer();
  tickTimer = setInterval(renderTimer, 1000);
  // Reconcile with the server periodically; it is the source of truth (#3).
  pollTimer = setInterval(() => pollState(attemptId), 15000);
}

function stopTimers() {
  clearInterval(tickTimer);
  clearInterval(pollTimer);
}

function remainingMs() {
  if (serverDeadline == null) return null;
  return serverDeadline - Date.now();
}

function renderTimer() {
  const ms = remainingMs();
  const el = $("#timer");
  if (ms == null) {
    el.textContent = "--:--";
    return;
  }
  if (ms <= 0) {
    el.textContent = "0:00";
    el.className = "timer crit";
    autoSubmit();
    return;
  }
  const total = Math.floor(ms / 1000);
  const m = Math.floor(total / 60);
  const s = String(total % 60).padStart(2, "0");
  el.textContent = `${m}:${s}`;
  el.className = "timer" + (total <= 30 ? " crit" : total <= 120 ? " low" : "");
}

async function pollState(attemptId) {
  try {
    const state = await api(`/attempts/${encodeURIComponent(attemptId)}/state`);
    syncDeadline(state);
    if (state.expired || state.status === "submitted") {
      stopTimers();
      showDone(state.status === "submitted");
    }
  } catch (e) {
    // A dangling attempt (404) won't recover — stop and finish. Transient
    // network errors are tolerated: keep the local countdown running.
    if (e.status === 404) {
      stopTimers();
      showDone(false);
    }
  }
}

// -------------------------------------------------------------------------- //
// Rendering one item at a time.
// -------------------------------------------------------------------------- //
function renderCurrent(attemptId) {
  const { section, item } = flat[idx];
  if (rec) rec.setItem(item.id);

  $("#progress").textContent = `Question ${idx + 1} of ${flat.length}`;
  $("#stimulus").innerHTML = "";
  renderStimulus(section);
  $("#question").innerHTML = "";
  renderItem(attemptId, item);

  $("#prevBtn").disabled = idx === 0;
  const last = idx === flat.length - 1;
  $("#nextBtn").classList.toggle("hidden", last);
  $("#submitBtn").classList.toggle("hidden", !last);
}

function renderStimulus(section) {
  const st = section.stimulus;
  const box = $("#stimulus");
  if (st.kind === "passage_text" || st.kind === "gapped_text") {
    box.innerHTML = `<div class="stimulus">${esc(st.text)}</div>`;
  } else if (st.kind === "image_set") {
    box.innerHTML =
      `<div class="stimulus">` +
      st.asset_ids.map((a) => `<img class="opt-img" src="/assets/${esc(a)}" alt="" />`).join("") +
      `</div>`;
  } else if (st.kind === "audio_asset") {
    renderAudio(section.id, st);
  }
  // matching_pool is rendered inline with the matching item's select.
}

function renderAudio(sectionId, st) {
  const plays = st.plays || 2;
  const box = $("#stimulus");
  box.innerHTML = `<div class="stimulus">
      <audio id="audio" src="/assets/${esc(st.asset_id)}" preload="none"></audio>
      <div class="nav"><button id="playBtn">▶ Play</button>
      <span class="muted" id="playsLeft">${plays} play(s) left</span></div>
    </div>`;
  const audio = $("#audio");
  const btn = $("#playBtn");
  let used = 0;
  if (rec) rec.attachAudio(audio, sectionId);
  btn.onclick = () => {
    if (used >= plays) return;
    used += 1;
    audio.play().catch(() => {});
    $("#playsLeft").textContent = `${plays - used} play(s) left`;
    if (used >= plays) btn.disabled = true;
  };
}

function renderItem(attemptId, item) {
  const q = $("#question");
  const prior = answers[item.id];
  const prompt = `<p class="prompt">${esc(item.prompt)}</p>`;

  if (item.item_type === "single_choice") {
    q.innerHTML =
      prompt +
      item.options
        .map((o, i) => {
          const checked = prior === i ? "checked" : "";
          const label =
            o.kind === "image"
              ? `<img class="opt-img" src="/assets/${esc(o.asset_id)}" alt="${esc(o.alt || "")}" />`
              : esc(o.text);
          return `<label class="opt"><input type="radio" name="opt" value="${i}" ${checked}/>${label}</label>`;
        })
        .join("");
    q.querySelectorAll("input[name=opt]").forEach((el) =>
      el.addEventListener("change", () => save(attemptId, item.id, Number(el.value)))
    );
  } else if (item.item_type === "gap_fill") {
    q.innerHTML =
      prompt + `<input type="text" id="gap" autocomplete="off" value="${esc(prior || "")}" />`;
    wireText(attemptId, item.id, $("#gap"));
  } else if (item.item_type === "matching") {
    const pool = currentSectionPool();
    const opts = pool
      .map((p) => `<option value="${esc(p.key)}" ${prior === p.key ? "selected" : ""}>${esc(p.key)} — ${esc(p.text)}</option>`)
      .join("");
    q.innerHTML = prompt + `<select id="match"><option value="">— choose —</option>${opts}</select>`;
    $("#match").addEventListener("change", (e) => {
      if (e.target.value) save(attemptId, item.id, e.target.value);
    });
  } else if (item.item_type === "open_writing") {
    const bullets = (item.bullet_points || [])
      .map((b) => `<li>${esc(b)}</li>`)
      .join("");
    q.innerHTML =
      prompt +
      (bullets ? `<ul class="bullets">${bullets}</ul>` : "") +
      `<textarea id="writing">${esc(prior || "")}</textarea>` +
      `<div class="wordcount" id="wc"></div>`;
    const ta = $("#writing");
    const wc = $("#wc");
    const tally = () => {
      const n = ta.value.trim() ? ta.value.trim().split(/\s+/).length : 0;
      wc.textContent = `${n} / ${item.word_min} words`;
      wc.classList.toggle("ok", n >= item.word_min);
    };
    tally();
    ta.addEventListener("input", tally);
    wireText(attemptId, item.id, ta);
  }
}

function currentSectionPool() {
  const st = flat[idx].section.stimulus;
  return st.kind === "matching_pool" ? st.options : [];
}

// Debounce text saves so we persist on pause, not on every keystroke.
function wireText(attemptId, itemId, el) {
  let t = null;
  el.addEventListener("input", () => {
    clearTimeout(t);
    t = setTimeout(() => save(attemptId, itemId, el.value), 600);
  });
  el.addEventListener("blur", () => {
    clearTimeout(t);
    save(attemptId, itemId, el.value);
  });
}

// -------------------------------------------------------------------------- //
// Save (displayed -> server maps to canonical) + telemetry answer_change.
// -------------------------------------------------------------------------- //
async function save(attemptId, itemId, value) {
  if (finished) return;
  if (answers[itemId] === value) return;
  answers[itemId] = value;
  if (rec) rec.answerChange(itemId, value);
  $("#saveErr").textContent = "";
  try {
    await api(`/attempts/${encodeURIComponent(attemptId)}/answers/${encodeURIComponent(itemId)}`, {
      method: "PUT",
      body: { response: value },
    });
  } catch (e) {
    if (e.status === 409) {
      // Past the deadline / already finalized — the server has the last word.
      return pollState(attemptId);
    }
    $("#saveErr").textContent = "Could not save your last answer — check your connection.";
  }
}

// -------------------------------------------------------------------------- //
// Navigation + submit.
// -------------------------------------------------------------------------- //
function attemptId() {
  const a = getActive();
  return a ? a.attemptId : null;
}

function go(delta) {
  idx = Math.max(0, Math.min(flat.length - 1, idx + delta));
  renderCurrent(attemptId());
}

async function submitTest() {
  const id = attemptId();
  if (!id || finished) return;
  $("#submitBtn").disabled = true;
  try {
    const state = await api(`/attempts/${encodeURIComponent(id)}/submit`, { method: "POST" });
    showDone(state.status === "submitted");
  } catch (e) {
    if (e.status === 409) {
      // Already submitted or expired — treat as finished.
      showDone(false);
    } else {
      $("#saveErr").textContent = "Submit failed — try again.";
      $("#submitBtn").disabled = false;
    }
  }
}

let autoSubmitted = false;
async function autoSubmit() {
  if (autoSubmitted || finished) return;
  autoSubmitted = true;
  stopTimers();
  const id = attemptId();
  if (!id) return showDone(false);
  try {
    await api(`/attempts/${encodeURIComponent(id)}/submit`, { method: "POST" });
  } catch {
    /* expired submits are rejected server-side — that's expected */
  }
  showDone(false);
}

function showDone(submitted) {
  finished = true;
  stopTimers();
  localStorage.removeItem(ACTIVE_KEY);
  if (rec) rec.stop();
  $("#pick").classList.add("hidden");
  $("#runner").classList.add("hidden");
  $("#examHeader").classList.add("hidden");
  $("#done").classList.remove("hidden");
  if (!submitted) {
    $("#doneIcon").textContent = "⏱";
    $("#doneTitle").textContent = "Time's up";
    $("#doneMsg").textContent =
      "The exam time ended and your answers so far have been recorded.";
  }
}

// -------------------------------------------------------------------------- //
// Boot: resume an active attempt, else show the picker.
// -------------------------------------------------------------------------- //
$("#prevBtn").onclick = () => go(-1);
$("#nextBtn").onclick = () => go(1);
$("#submitBtn").onclick = submitTest;

async function boot() {
  const active = getActive();
  if (active && (!linkTestId || active.testId === linkTestId)) {
    try {
      const state = await api(`/attempts/${encodeURIComponent(active.attemptId)}/state`);
      return enterExam(active.attemptId, state);
    } catch (e) {
      localStorage.removeItem(ACTIVE_KEY); // dangling — fall back to the picker
    }
  }
  showPicker();
}

boot();
