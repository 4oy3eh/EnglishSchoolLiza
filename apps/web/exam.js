/* Student exam runner (Phase 11, listening-UX rework).
 *
 * Drives one attempt in the browser against the same-origin /exam delivery API:
 * pick-your-name from the share link, start-or-resume the single attempt, render
 * one SECTION (= part) at a time with all its questions, save answers, run a
 * DISPLAY-ONLY countdown, and submit.
 *
 * Navigation unit is the section/part, not a single question. Sections are grouped
 * by `skill` into Reading / Writing / Listening blocks so the student always knows
 * which paper they are in. A question navigator shows answered/unanswered squares.
 *
 * Listening is one continuous recording: the audio element lives at the BLOCK
 * level (in #audioBar, built once), so moving between listening parts never stops
 * or restarts it. The optional sound-check (`preview_asset_id`) is freely
 * replayable; the main track is locked — single play, no pause/seek/restart.
 *
 * Golden rules on the client:
 *   #1  Never request or expect an answer key. The runner only ever consumes the
 *       keyless `ClientTest` projection; there is no `correct` anywhere here.
 *   #3  The timer is display-only. The server's /state is the single source of
 *       truth for time remaining and for expiry.
 *
 * Integrity (capture, never judge — golden rule #6): request fullscreen where
 * supported (skipped on iOS), block copy/cut/paste/contextmenu and text drops, and
 * wire the Phase-6 recorder so visibility/blur/answer/audio events reach the ingest
 * sink. A blocked paste/drop is recorded as `paste_attempt` with the LENGTH of the
 * attempted text only; open_writing textareas get typing-pattern stats
 * (`rec.trackWriting`) — counts and timings, never content or key identities. The
 * last writing window is flushed and the telemetry queue drained before submit.
 *
 * Submit goes through a review panel (#review): per part answered / unanswered,
 * the save queue's unsaved / rejected answers, time left, "Go to question" jumps;
 * its Submit keeps the old confirms and drains the save queue first. The deadline
 * auto-submit never waits for it. Display-only warnings at 10 and 2 minutes left
 * are shown + announced (aria-live); the server stays authoritative (rule #3).
 */
"use strict";

const ACTIVE_KEY = "exam:active"; // {testId, attemptId, rosterEntryId, name}
const $ = (sel) => document.querySelector(sel);

const params = new URLSearchParams(location.search);
const linkTestId = params.get("test");

let rec = null; // telemetry recorder
let units = []; // [{blockIdx, skill, section}] in served order (one per section)
let blocks = []; // [{skill, label, unitIdxs:[...], audio: AudioAssetStimulus|null}]
let itemNo = {}; // item_id -> display number (parsed from id, fallback running)
let idx = 0; // current unit (section) index
let curAttempt = null; // attempt id, for handlers that fire outside renderCurrent
const answers = {}; // item_id -> last value the SERVER confirmed (2xx on PUT)
const pending = {}; // item_id -> latest entered value not yet confirmed ("dirty")
const rejected = {}; // item_id -> value the server refused (400/404/422): never retried
const colourPaintUrls = {}; // colour_task item_id -> paint-only dataURL (re-entry redraw)
const PENDING_KEY = "exam:pending:"; // + attemptId -> JSON of `pending` (survives reload)
const PENDING_MAX_VALUE = 64 * 1024; // values larger than this (colour PNGs) are not persisted
let saveRunning = false; // the save loop is awaiting a PUT
let saveTimer = null; // scheduled (possibly backed-off) save loop run
let saveFailures = 0; // consecutive failed PUTs (drives the backoff)
let saveHalted = false; // a 409 told us the attempt is over — stop retrying
let serverDeadline = null; // ms epoch, from /state (authoritative)
let audioProgress = 0; // furthest listening position (seconds), server-authoritative
let tickTimer = null;
let pollTimer = null;
let finished = false;
let reviewOpen = false; // the review-before-submit panel is showing
let submitting = false; // a manual submit is in progress (drain -> confirm -> POST)
let warnedBelow = Infinity; // smallest timer-warning threshold (s) already shown

// Display-only timer warnings (seconds left -> message). The server still decides
// expiry (rule #3); these only make sure the student notices the clock.
const TIME_WARNINGS = [
  { at: 120, crit: true, text: "Only 2 minutes left! The test is submitted automatically when time runs out." },
  { at: 600, crit: false, text: "10 minutes left. The timer does not stop — check your answers and submit." },
];

// Persistent listening audio (survives section re-renders / block re-entry).
let audioBuiltFor = null; // blockIdx whose #audioBar is currently mounted
let mainAudioEl = null;
let mainStarted = false;

// -------------------------------------------------------------------------- //
// API helper.
// -------------------------------------------------------------------------- //
async function api(path, { method = "GET", body } = {}) {
  // `ngrok-skip-browser-warning` makes ngrok's free tier skip its interstitial
  // page for these XHR calls (otherwise it returns that HTML instead of JSON and
  // the runner can't load the roster/test). Harmless on non-ngrok hosts.
  const headers = { "ngrok-skip-browser-warning": "true" };
  if (body) headers["Content-Type"] = "application/json";
  const resp = await fetch(`/exam${path}`, {
    method,
    headers,
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
    // `expired` (time ran out, no submit) is finished exactly like `submitted`.
    const done = r.status === "submitted" || r.status === "expired";
    const btn = document.createElement("button");
    btn.className = "name-btn" + (done ? " done" : "");
    btn.textContent = r.display_name + (done ? "  (done)" : "");
    btn.disabled = done;
    btn.onclick = () => begin(r);
    list.appendChild(btn);
  }
}

async function begin(rosterEntry) {
  // Guard against a teacher (or wrong person) starting an attempt by accident:
  // confirm the student identity before the timer begins.
  if (
    !confirm(
      `Ты точно УЧЕНИК — ${rosterEntry.display_name}?\n\n` +
        "Нажми OK, только если это ты и ты готов(а) начать тест.\n" +
        "Время пойдёт сразу. Учителю сюда не нужно."
    )
  ) {
    return;
  }
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
// Build the navigation model: sections grouped into skill blocks.
// -------------------------------------------------------------------------- //
function skillLabel(skill) {
  return { reading: "Reading", writing: "Writing", listening: "Listening" }[skill] || "Section";
}

function buildModel(test) {
  units = [];
  blocks = [];
  itemNo = {};
  let cur = null;
  let blockSeq = 0; // questions numbered 1..N within each skill block (Reading/Listening)
  for (const section of test.sections) {
    const skill = section.skill || "other";
    if (!cur || cur.skill !== skill) {
      cur = { skill, label: skillLabel(skill), unitIdxs: [], audio: null };
      blocks.push(cur);
      blockSeq = 0; // restart numbering for the new block
    }
    const blockIdx = blocks.length - 1;
    units.push({ blockIdx, skill, section });
    cur.unitIdxs.push(units.length - 1);
    if (section.stimulus && section.stimulus.kind === "audio_asset" && !cur.audio) {
      cur.audio = section.stimulus;
    }
    for (const item of section.items) {
      blockSeq += 1;
      itemNo[item.id] = String(blockSeq);
    }
  }
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

  buildModel(test);
  idx = 0;
  curAttempt = attemptId;
  audioBuiltFor = null;
  mainStarted = false;

  // Server is the source of truth: re-load previously saved answers so a refresh
  // (the timer keeps running) never loses the student's work.
  try {
    const saved = await api(`/attempts/${encodeURIComponent(attemptId)}/answers`);
    Object.assign(answers, saved);
    for (const [itemId, val] of Object.entries(saved)) {
      if (typeof val === "string" && val.startsWith("data:")) colourPaintUrls[itemId] = val;
    }
  } catch (e) {
    /* non-fatal — start with a blank sheet */
  }
  // Anything entered before a network drop + reload is still "dirty" locally:
  // resend it now (the server copy, if any, is older than what the student typed).
  loadPending(attemptId);

  $("#pick").classList.add("hidden");
  $("#examHeader").classList.remove("hidden");
  $("#runner").classList.remove("hidden");
  $("#navigator").classList.remove("hidden");
  $("#examTitle").textContent = test.title;

  startIntegrity(attemptId);
  if (state) syncDeadline(state);
  startTimers(attemptId);
  renderCurrent(attemptId);
  renderSaveStatus();
  scheduleSaveLoop(0);
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
  for (const ev of ["copy", "cut", "contextmenu"]) {
    document.addEventListener(ev, (e) => {
      e.preventDefault();
      const u = units[idx];
      const first = u && u.section.items[0];
      if (rec && first) rec.interaction(first.id, { blocked: ev });
    });
  }
  // Paste / text drop: blocked, and recorded as `paste_attempt` with only the
  // LENGTH of what the student tried to insert (never the text itself).
  document.addEventListener("paste", (e) => {
    e.preventDefault();
    let length = 0;
    try {
      length = ((e.clipboardData && e.clipboardData.getData("text")) || "").length;
    } catch {
      /* clipboard unreadable */
    }
    blockedInsert(e.target, "paste", length);
  });
  document.addEventListener("drop", (e) => {
    const t = e.target;
    if (!t || !(t.tagName === "TEXTAREA" || t.tagName === "INPUT")) return;
    e.preventDefault();
    let length = 0;
    try {
      length = ((e.dataTransfer && e.dataTransfer.getData("text")) || "").length;
    } catch {
      /* drag data unreadable */
    }
    blockedInsert(t, "drop", length);
  });

  // Request fullscreen on the first user gesture, where supported. iOS Safari
  // has no element fullscreen, so we skip it there and degrade gracefully.
  //
  // Dev convenience: skip auto-fullscreen on localhost so manual testing isn't
  // jarring (the integrity capture — visibility/blur events — still runs). Force
  // the real exam behaviour with `?fs=1`. In production this always engages.
  const forceFs = params.get("fs") === "1";
  const isLocalDev =
    location.hostname === "localhost" || location.hostname === "127.0.0.1";
  if (!isIOS() && (forceFs || !isLocalDev)) {
    const goFull = () => {
      const el = document.documentElement;
      if (el.requestFullscreen) el.requestFullscreen().catch(() => {});
      document.removeEventListener("click", goFull);
    };
    document.addEventListener("click", goFull, { once: true });
  }
}

// The item + field kind a blocked paste/drop was aimed at (falls back to the
// question in focus, then to the first question of the visible part).
function blockedInsert(target, kind, length) {
  if (!rec) return;
  let itemId = null;
  let field = null;
  if (target && typeof target.id === "string" && target.id.startsWith("ctl-")) {
    itemId = target.id.slice(4);
    field = target.tagName === "TEXTAREA" ? "open_writing" : "gap_fill";
  }
  if (!itemId) itemId = rec.currentItemId;
  if (!itemId) {
    const u = units[idx];
    itemId = u && u.section.items[0] ? u.section.items[0].id : null;
  }
  rec.pasteAttempt(itemId, kind, length, field);
}

// -------------------------------------------------------------------------- //
// Server-authoritative timer (display-only render; server decides expiry).
// -------------------------------------------------------------------------- //
function syncDeadline(state) {
  serverDeadline = state.deadline ? new Date(state.deadline).getTime() : null;
  if (state && typeof state.audio_progress_seconds === "number") {
    audioProgress = Math.max(audioProgress, state.audio_progress_seconds);
    audioProgressSent = Math.max(audioProgressSent, state.audio_progress_seconds);
  }
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
    if (reviewOpen) $("#reviewTime").textContent = "0:00";
    // Never waits for the review screen (or anything else): the deadline submits.
    autoSubmit();
    return;
  }
  const total = Math.floor(ms / 1000);
  const m = Math.floor(total / 60);
  const s = String(total % 60).padStart(2, "0");
  el.textContent = `${m}:${s}`;
  el.className = "timer" + (total <= 30 ? " crit" : total <= 120 ? " low" : "");
  if (reviewOpen) $("#reviewTime").textContent = `${m}:${s}`;
  maybeWarnTime(total);
}

// Show (and announce once) the most urgent warning whose threshold was crossed.
// A resume that lands below 10 min shows the matching one straight away.
function maybeWarnTime(totalSec) {
  const hit = TIME_WARNINGS.find((w) => totalSec <= w.at);
  if (!hit || hit.at >= warnedBelow) return;
  warnedBelow = hit.at;
  $("#timeWarnText").textContent = "⏰ " + hit.text;
  $("#timeWarn").className = "time-warn" + (hit.crit ? " crit" : "");
  $("#timeWarnLive").textContent = hit.text; // assertive live region -> read out
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
// Rendering one section (part) at a time.
// -------------------------------------------------------------------------- //
function renderCurrent(attemptId) {
  const u = units[idx];
  const block = blocks[u.blockIdx];

  $("#skillLabel").textContent = block.label;
  $("#skillLabel").className = "skill-pill " + u.skill;
  $("#partLabel").textContent = u.section.title || "";
  $("#progress").textContent = `Part ${idx + 1} of ${units.length}`;

  // Listening audio bar: mounted once per listening block, shown only inside it.
  if (block.skill === "listening" && block.audio) {
    ensureAudioBar(u.blockIdx, block.audio, u.section.id);
    $("#audioBar").classList.remove("hidden");
  } else {
    $("#audioBar").classList.add("hidden");
  }

  renderSectionBody(attemptId, u.section);

  $("#prevBtn").disabled = idx === 0;
  const last = idx === units.length - 1;
  $("#nextBtn").classList.toggle("hidden", last);
  $("#reviewBtn").classList.toggle("hidden", !last);

  renderNavigator(attemptId);
}

// -------------------------------------------------------------------------- //
// Audio progress persistence (anti-abuse): the furthest point reached lives on
// the SERVER (see /audio-progress + /state). A refresh — even on Brave with
// cookies/storage cleared, or a different device — resumes from there and can
// never restart from zero or re-run the sound-check.
// -------------------------------------------------------------------------- //
let audioProgressSent = 0; // furthest position the server confirmed
let audioPushInflight = false;
let audioRetryTimer = null;

function reportAudioProgress(sec) {
  const s = Math.floor(sec);
  if (!Number.isFinite(s) || s <= audioProgress) return;
  audioProgress = s; // local furthest point; pushed below (server takes the max)
  pushAudioProgress();
}

// Push the furthest position; a failed POST is retried (3 s) until the server has
// at least the local value, so a network blip never loses the resume point.
function pushAudioProgress() {
  const id = attemptId();
  if (!id || finished || audioPushInflight || audioProgressSent >= audioProgress) return;
  const s = audioProgress;
  audioPushInflight = true;
  clearTimeout(audioRetryTimer);
  api(`/attempts/${encodeURIComponent(id)}/audio-progress`, {
    method: "POST",
    body: { seconds: s },
  })
    .then(() => {
      audioPushInflight = false;
      audioProgressSent = Math.max(audioProgressSent, s);
      if (audioProgressSent < audioProgress) pushAudioProgress();
    })
    .catch((e) => {
      audioPushInflight = false;
      if (e.status === 409 || e.status === 404) {
        audioProgressSent = s; // attempt is over / gone — nothing more to report
        return;
      }
      console.warn("[exam] audio progress not saved, retrying in 3 s", e);
      audioRetryTimer = setTimeout(pushAudioProgress, 3000);
    });
}

// -------------------------------------------------------------------------- //
// Listening audio bar: one continuous recording for the whole block.
// -------------------------------------------------------------------------- //
function ensureAudioBar(blockIdx, audio, sectionId) {
  if (audioBuiltFor === blockIdx) return; // keep the running player untouched
  audioBuiltFor = blockIdx;

  const plays = audio.plays || 1;
  const locked = audio.locked !== false;
  const resumePos = audioProgress; // > 0 means the test track was already started
  const resuming = resumePos > 0;
  // On a refresh the calibration/sound-check is over and the track is mid-way:
  // no preview, no restart — only resume from the furthest point reached.
  const hasPreview = !!audio.preview_asset_id && !resuming;
  mainStarted = resuming;
  const playsText = plays === 1 ? "once" : `${plays} times`;

  const bar = $("#audioBar");
  bar.innerHTML = `
    <div class="audiobar">
      <div class="audio-warn">🎧 One continuous recording. The test track plays ${playsText} and
        ${locked ? "<b>cannot be paused, rewound, or restarted</b>" : "should be played carefully"} —
        moving between questions does <b>not</b> stop it.
        ${resuming
          ? "If your page reloaded, the test track continues from where you were — it cannot start over."
          : hasPreview
            ? "Play the sound-check below as many times as you like, then start the test."
            : "Make sure your sound works before you start."}</div>
      ${hasPreview
        ? `<div class="audio-row">
             <button id="previewBtn" type="button">▶ Sound check</button>
             <span class="muted">Replay freely to check your sound</span>
             <audio id="previewAudio" src="/assets/${esc(audio.preview_asset_id)}" preload="none"></audio>
           </div>`
        : ""}
      <div class="audio-row">
        <button id="mainBtn" class="primary" type="button" ${hasPreview ? "disabled" : ""}>▶ ${resuming ? "Resume listening" : "Start listening test"}</button>
        <span class="muted" id="mainStatus">${hasPreview ? "Listen to the sound check first" : resuming ? "Continues from where you were" : `${plays} play(s)`}</span>
        <audio id="mainAudio" src="/assets/${esc(audio.asset_id)}" preload="none"></audio>
      </div>
    </div>`;

  mainAudioEl = $("#mainAudio");
  if (rec) rec.attachAudio(mainAudioEl, sectionId);

  const mainBtn = $("#mainBtn");
  const mainStatus = $("#mainStatus");

  if (hasPreview) {
    const pv = $("#previewAudio");
    const pvBtn = $("#previewBtn");
    pvBtn.onclick = () => {
      pv.currentTime = 0;
      pv.play().catch(() => {});
    };
    pv.addEventListener("ended", () => {
      if (!mainStarted) {
        mainBtn.disabled = false;
        mainStatus.textContent = `${plays} play(s)`;
      }
    });
  }

  let used = 0;
  mainBtn.onclick = () => {
    if (used >= plays) return;
    used += 1;
    mainStarted = true;
    const pvBtn = $("#previewBtn");
    if (pvBtn) pvBtn.disabled = true; // sound-check closes once the test begins
    mainBtn.disabled = true;
    mainStatus.textContent = resuming ? "Continuing…" : "Playing…";
    const target = resuming ? resumePos : 0;
    const go = () => {
      // Resuming: jump forward to the furthest point already heard (never earlier).
      if (target > 0 && mainAudioEl.currentTime < target - 0.5) {
        try {
          mainAudioEl.currentTime = target;
        } catch (e) {
          /* not seekable yet — will retry on the next ready event */
        }
      }
      mainAudioEl.play().catch(() => {});
    };
    // preload="none" -> readyState 0; load metadata first so the seek can land,
    // then play from `target` (HTTP Range on /assets makes the forward seek work).
    if (target > 0 && mainAudioEl.readyState < 1) {
      mainAudioEl.addEventListener("loadedmetadata", go, { once: true });
      mainAudioEl.load();
    } else {
      go();
    }
  };

  // Report the furthest position to the server every ~5s (it stores the max).
  let lastBucket = Math.floor(resumePos / 5);
  mainAudioEl.addEventListener("timeupdate", () => {
    const t = mainAudioEl.currentTime;
    if (Math.floor(t / 5) > lastBucket) {
      lastBucket = Math.floor(t / 5);
      reportAudioProgress(t);
    }
  });
  mainAudioEl.addEventListener("ended", () => {
    reportAudioProgress(mainAudioEl.duration || mainAudioEl.currentTime);
    mainStatus.textContent = used >= plays ? "Finished" : `${plays - used} play(s) left`;
    if (used < plays) mainBtn.disabled = false;
  });
  // Locked: a pause attempt (or background throttling) resumes the single play.
  if (locked) {
    mainAudioEl.addEventListener("pause", () => {
      if (mainStarted && !mainAudioEl.ended) mainAudioEl.play().catch(() => {});
    });
  }
}

// -------------------------------------------------------------------------- //
// Section body: shared stimulus + every question in the section.
// -------------------------------------------------------------------------- //
function renderSectionBody(attemptId, section) {
  const body = $("#sectionBody");
  body.innerHTML = stimulusHTML(section) + section.items.map((it) => itemHTML(section, it)).join("");
  for (const item of section.items) wireItem(attemptId, section, item);
}

function contextImagesHTML(st) {
  // Optional pictures shown beside a passage/gapped/audio stimulus (YLE parts).
  const imgs = st.images || [];
  if (!imgs.length) return "";
  return (
    `<div class="stimulus ctx-images">` +
    imgs.map((a, i) => `<img src="/assets/${esc(a)}" alt="${esc(picAlt(st.__title, i, imgs.length))}" />`).join("") +
    `</div>`
  );
}

// Alt text for a stimulus picture that has none of its own: the part title.
function picAlt(title, i, n) {
  const base = title ? `Picture for ${title}` : "Picture for this part";
  return n > 1 ? `${base} (${i + 1} of ${n})` : base;
}

function stimulusHTML(section) {
  const st = { ...section.stimulus, __title: section.title || "" };
  if (st.kind === "passage_text" || st.kind === "gapped_text") {
    return `<div class="stimulus">${esc(st.text)}</div>` + contextImagesHTML(st);
  }
  if (st.kind === "image_set") {
    const n = st.asset_ids.length;
    return (
      `<div class="stimulus">` +
      st.asset_ids
        .map((a, i) => `<img class="opt-img" src="/assets/${esc(a)}" alt="${esc(picAlt(st.__title, i, n))}" />`)
        .join("") +
      `</div>`
    );
  }
  if (st.kind === "matching_pool") {
    // Optional passage (e.g. a gapped text) above the A-H list shown ONCE; each
    // question then just picks a letter (no need to re-read the list per gap).
    const passage = st.text ? `<div class="stimulus">${esc(st.text)}</div>` : "";
    const list =
      `<div class="pool-ref"><div class="pool-title">Choose from:</div>` +
      st.options
        .map((p) => `<div class="pool-item"><b>${esc(p.key)}</b>&nbsp; ${esc(p.text)}</div>`)
        .join("") +
      `</div>`;
    return passage + list;
  }
  // audio_asset: the player is the persistent #audioBar, but any context pictures
  // (e.g. a listening note form to fill in) still render inline here.
  return contextImagesHTML(st);
}

function itemHTML(section, item) {
  const prior = localValue(item.id);
  const num = itemNo[item.id] || "";
  // Every control is labelled by "Question N" + its prompt (screen readers).
  const qnId = `qn-${esc(item.id)}`;
  const promptId = `pr-${esc(item.id)}`;
  const labelledBy = `aria-labelledby="${qnId} ${promptId}"`;
  const promptImg = item.image
    ? `<img class="prompt-img" src="/assets/${esc(item.image)}" alt="${esc(item.prompt)}" />`
    : "";
  const head =
    `<div class="qnum" id="${qnId}">${num ? `<span class="sr-only">Question </span>${esc(num)}` : ""}</div>` +
    `<p class="prompt" id="${promptId}">${esc(item.prompt)}</p>` +
    promptImg;
  let control = "";

  if (item.item_type === "single_choice") {
    const allImg = item.options.every((o) => o.kind === "image");
    const opts = item.options
      .map((o, i) => {
        const checked = prior === i ? "checked" : "";
        if (o.kind === "image") {
          // No authored alt: fall back to the prompt + the option's position.
          const alt = o.alt || `${item.prompt} — picture ${i + 1}`;
          return (
            `<label class="opt-tile${checked ? " sel" : ""}">` +
            `<input type="radio" name="opt-${esc(item.id)}" value="${i}" ${checked}/>` +
            `<img src="/assets/${esc(o.asset_id)}" alt="${esc(alt)}" /></label>`
          );
        }
        return `<label class="opt"><input type="radio" name="opt-${esc(item.id)}" value="${i}" ${checked}/>${esc(o.text)}</label>`;
      })
      .join("");
    // Image options sit in a wrapping grid (one row / 2×4); text options stack.
    control = `<div role="radiogroup" ${labelledBy}${allImg ? ' class="opt-grid"' : ""}>${opts}</div>`;
  } else if (item.item_type === "gap_fill") {
    control = `<input type="text" id="ctl-${esc(item.id)}" ${labelledBy} autocomplete="off" value="${esc(prior || "")}" />`;
  } else if (item.item_type === "matching") {
    const pool = section.stimulus.kind === "matching_pool" ? section.stimulus.options : [];
    // A 2×4 grid of letter buttons (the sentences live in the pool list above).
    control =
      `<div class="match-grid" role="radiogroup" ${labelledBy}>` +
      pool
        .map(
          (p) =>
            `<label class="opt-letter${prior === p.key ? " sel" : ""}">` +
            `<input type="radio" name="opt-${esc(item.id)}" value="${esc(p.key)}" ${prior === p.key ? "checked" : ""}/>` +
            `<span>${esc(p.key)}</span></label>`
        )
        .join("") +
      `</div>`;
  } else if (item.item_type === "open_writing") {
    const bullets = (item.bullet_points || []).map((b) => `<li>${esc(b)}</li>`).join("");
    control =
      (bullets ? `<ul class="bullets">${bullets}</ul>` : "") +
      `<textarea id="ctl-${esc(item.id)}" ${labelledBy} aria-describedby="wc-${esc(item.id)}">${esc(prior || "")}</textarea>` +
      `<div class="wordcount" id="wc-${esc(item.id)}"></div>`;
  } else if (item.item_type === "colour_task") {
    const swatches = (item.palette || ["blue", "green", "red", "brown"])
      .map(
        (c) =>
          `<button type="button" class="swatch" data-colour="${esc(c)}" style="background:${esc(c)}" title="${esc(c)}"></button>`
      )
      .join("");
    control =
      `<div class="colour-tools">${swatches}` +
      `<button type="button" class="swatch eraser" data-colour="eraser" title="rubber">⌫</button></div>` +
      `<div class="colour-stage" id="stage-${esc(item.id)}">` +
      `<canvas class="colour-paint" id="paint-${esc(item.id)}"></canvas>` +
      `<img class="colour-line" id="line-${esc(item.id)}" src="/assets/${esc(item.asset_id)}" alt="${esc(`Picture to colour: ${item.prompt}`)}" />` +
      `</div>`;
  }

  return `<div class="qcard" id="q-${esc(item.id)}">${head}${control}</div>`;
}

function wireItem(attemptId, section, item) {
  const focusItem = () => {
    if (rec) rec.setItem(item.id);
  };

  if (item.item_type === "single_choice") {
    const radios = document.querySelectorAll(`input[name="opt-${cssId(item.id)}"]`);
    radios.forEach((el) => {
      el.addEventListener("focus", focusItem);
      el.addEventListener("change", () => {
        // Highlight the chosen picture tile (no-op for text options).
        radios.forEach((r) => {
          const tile = r.closest(".opt-tile");
          if (tile) tile.classList.toggle("sel", r.checked);
        });
        save(attemptId, item.id, Number(el.value));
      });
    });
  } else if (item.item_type === "gap_fill") {
    const el = byCtl(item.id);
    el.addEventListener("focus", focusItem);
    wireText(attemptId, item.id, el);
  } else if (item.item_type === "matching") {
    const radios = document.querySelectorAll(`input[name="opt-${cssId(item.id)}"]`);
    radios.forEach((el) => {
      el.addEventListener("focus", focusItem);
      el.addEventListener("change", () => {
        radios.forEach((r) => {
          const lab = r.closest(".opt-letter");
          if (lab) lab.classList.toggle("sel", r.checked);
        });
        save(attemptId, item.id, el.value);
      });
    });
  } else if (item.item_type === "open_writing") {
    const ta = byCtl(item.id);
    const wc = document.getElementById(`wc-${item.id}`);
    const tally = () => {
      const n = ta.value.trim() ? ta.value.trim().split(/\s+/).length : 0;
      wc.textContent = `${n} / ${item.word_min} words`;
      wc.classList.toggle("ok", n >= item.word_min);
    };
    tally();
    ta.addEventListener("focus", focusItem);
    ta.addEventListener("input", tally);
    wireText(attemptId, item.id, ta);
    // Typing-pattern stats (counts/timings only — never content).
    if (rec) rec.trackWriting(ta, item.id, "open_writing");
  } else if (item.item_type === "colour_task") {
    wireColour(attemptId, item);
  }
}

// Colouring task: paint the white areas under a transparent-background line-art
// PNG with one medium brush (radius relative to the IMAGE, so it's consistent on
// every screen). The saved answer is the composite picture for the teacher to mark.
function wireColour(attemptId, item) {
  const canvas = document.getElementById(`paint-${item.id}`);
  const lineImg = document.getElementById(`line-${item.id}`);
  const tools = document.querySelector(`#q-${cssId(item.id)} .colour-tools`);
  if (!canvas || !lineImg || !tools) return;
  const ctx = canvas.getContext("2d");
  let colour = (item.palette && item.palette[0]) || "blue";

  const swatches = tools.querySelectorAll(".swatch");
  const select = (btn) => {
    swatches.forEach((s) => s.classList.remove("sel"));
    btn.classList.add("sel");
    colour = btn.dataset.colour;
  };
  swatches.forEach((btn) => (btn.onclick = () => select(btn)));
  if (swatches[0]) select(swatches[0]);

  const setup = () => {
    canvas.width = lineImg.naturalWidth || 600;
    canvas.height = lineImg.naturalHeight || 400;
    const prior = colourPaintUrls[item.id];
    if (prior) {
      const im = new Image();
      im.onload = () => ctx.drawImage(im, 0, 0);
      im.src = prior;
    }
  };
  if (lineImg.complete && lineImg.naturalWidth) setup();
  else lineImg.addEventListener("load", setup);

  const at = (e) => {
    const r = canvas.getBoundingClientRect();
    return { x: ((e.clientX - r.left) / r.width) * canvas.width, y: ((e.clientY - r.top) / r.height) * canvas.height };
  };
  const stroke = (a, b) => {
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    ctx.lineWidth = canvas.width * 0.045; // one medium brush, relative to the image
    if (colour === "eraser") {
      ctx.globalCompositeOperation = "destination-out";
      ctx.strokeStyle = "rgba(0,0,0,1)";
    } else {
      ctx.globalCompositeOperation = "source-over";
      ctx.strokeStyle = colour;
    }
    ctx.beginPath();
    ctx.moveTo(a.x, a.y);
    ctx.lineTo(b.x, b.y);
    ctx.stroke();
  };

  let last = null;
  canvas.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    if (rec) rec.setItem(item.id);
    canvas.setPointerCapture(e.pointerId);
    last = at(e);
    stroke(last, last);
  });
  canvas.addEventListener("pointermove", (e) => {
    if (!last) return;
    e.preventDefault();
    const p = at(e);
    stroke(last, p);
    last = p;
  });
  const end = () => {
    if (!last) return;
    last = null;
    commitColour(attemptId, item.id, canvas, lineImg);
  };
  canvas.addEventListener("pointerup", end);
  canvas.addEventListener("pointercancel", end);
}

function commitColour(attemptId, itemId, canvas, lineImg) {
  // Keep the paint-only layer for in-session redraw; send the flattened picture.
  colourPaintUrls[itemId] = canvas.toDataURL("image/png");
  const out = document.createElement("canvas");
  out.width = canvas.width;
  out.height = canvas.height;
  const octx = out.getContext("2d");
  octx.fillStyle = "#fff";
  octx.fillRect(0, 0, out.width, out.height);
  octx.drawImage(canvas, 0, 0);
  octx.drawImage(lineImg, 0, 0, out.width, out.height);
  save(attemptId, itemId, out.toDataURL("image/png"));
}

function byCtl(itemId) {
  return document.getElementById(`ctl-${itemId}`);
}

// CSS.escape for attribute selectors (item ids are simple, but be safe).
function cssId(s) {
  return window.CSS && CSS.escape ? CSS.escape(s) : s;
}

// Debounce text saves so we persist on pause, not on every keystroke. Every
// wired control registers a flusher so navigation / submit / the deadline can
// force the pending debounce out immediately (`flushTextSaves`).
const textFlushers = new Set();
function wireText(attemptId, itemId, el) {
  let t = null;
  let touched = false; // only a control the student typed in has anything to flush
  const flush = () => {
    clearTimeout(t);
    t = null;
    if (!el.isConnected) {
      textFlushers.delete(flush); // control was re-rendered away
      return;
    }
    if (touched) save(attemptId, itemId, el.value);
  };
  textFlushers.add(flush);
  el.addEventListener("input", () => {
    touched = true;
    clearTimeout(t);
    t = setTimeout(flush, 600);
  });
  el.addEventListener("blur", flush);
}

function flushTextSaves() {
  for (const f of Array.from(textFlushers)) f();
  if (rec) rec.flushWriting("save");
}

// Before a submit: push the last writing window and wait (bounded) until the
// telemetry queue is acknowledged, so the one-shot finalize sees it.
async function drainTelemetry(maxMs) {
  if (!rec) return;
  rec.flushWriting("submit");
  try {
    await rec.drain(maxMs);
  } catch {
    /* best effort */
  }
}

// -------------------------------------------------------------------------- //
// Question navigator: filled square = answered, click to jump.
// -------------------------------------------------------------------------- //
// What the student currently has entered: the dirty value if one is waiting to
// be saved, else a value the server rejected (still shown in the control so the
// student can change it), else the server-confirmed one.
function localValue(itemId) {
  if (itemId in pending) return pending[itemId];
  if (itemId in rejected) return rejected[itemId];
  return answers[itemId];
}

// A rejected answer is NOT answered: the navigator square stays empty for it.
function isAnswered(itemId) {
  if (itemId in rejected && !(itemId in pending)) return false;
  const v = localValue(itemId);
  return v !== undefined && v !== null && String(v).trim() !== "";
}

function rejectedIds() {
  return Object.keys(rejected).filter((i) => !(i in pending));
}

function renderNavigator(attemptId) {
  const nav = $("#navigator");
  let html = `<div class="navtitle">Your answers</div>`;
  for (const block of blocks) {
    html += `<div class="navblock"><div class="navskill">${esc(block.label)}</div><div class="navgrid">`;
    for (const ui of block.unitIdxs) {
      const sec = units[ui].section;
      for (const item of sec.items) {
        const cls =
          "navsq" + (isAnswered(item.id) ? " done" : "") + (ui === idx ? " cur" : "");
        html += `<button class="${cls}" data-unit="${ui}" data-item="${esc(item.id)}" title="${esc(item.prompt).slice(0, 70)}" aria-label="${esc(navLabel(item.id))}">${esc(itemNo[item.id] || "")}</button>`;
      }
    }
    html += `</div></div>`;
  }
  html +=
    `<div class="navlegend"><span><span class="legend-box done"></span>answered</span>` +
    `<span><span class="legend-box"></span>not answered</span></div>` +
    `<div class="navreview"><button type="button" id="navReviewBtn">Review &amp; submit</button></div>`;
  nav.innerHTML = html;
  nav.querySelectorAll(".navsq").forEach((b) => {
    b.onclick = () => jumpTo(Number(b.dataset.unit), b.dataset.item);
  });
  $("#navReviewBtn").onclick = showReview;
}

function navLabel(itemId) {
  const state = itemId in rejected && !(itemId in pending)
    ? "answer rejected"
    : isAnswered(itemId)
      ? "answered"
      : "not answered";
  return `Question ${itemNo[itemId] || ""}, ${state}`;
}

// Show the part holding `itemId` and bring the question into view + focus.
function jumpTo(unit, itemId, { focus = false } = {}) {
  if (reviewOpen) hideReview();
  if (unit !== idx || !document.getElementById("q-" + itemId)) {
    flushTextSaves();
    idx = unit;
    renderCurrent(attemptId());
  }
  const el = document.getElementById("q-" + itemId);
  if (el) {
    el.scrollIntoView({ behavior: "smooth", block: "center" });
    el.classList.add("flash");
    setTimeout(() => el.classList.remove("flash"), 1200);
    if (focus) {
      const ctl = el.querySelector("input:not([type=hidden]), textarea, button, canvas");
      if (ctl) ctl.focus({ preventScroll: true });
    }
  }
}

// -------------------------------------------------------------------------- //
// Review before the final submit: per part answered / unanswered, the save
// queue's state, time left, and jump links. Purely a view over the navigator
// data — the deadline never waits for it (autoSubmit runs from the timer).
// -------------------------------------------------------------------------- //
function showReview() {
  if (finished) return;
  flushTextSaves(); // a debounced keystroke must count as answered here
  scheduleSaveLoop(0);
  reviewOpen = true;
  $("#runner").classList.add("hidden");
  $("#navigator").classList.add("hidden");
  $("#review").classList.remove("hidden");
  $("#reviewErr").textContent = "";
  renderReview();
  renderTimer();
  window.scrollTo({ top: 0 });
  $("#reviewTitle").focus({ preventScroll: true });
}

function hideReview() {
  reviewOpen = false;
  $("#review").classList.add("hidden");
  if (finished) return;
  $("#runner").classList.remove("hidden");
  $("#navigator").classList.remove("hidden");
}

// The parts list is rebuilt only when its markup changed: renderSaveStatus calls
// renderReview on every save tick, and replacing innerHTML each time would drop
// the keyboard focus of a student tabbing through the panel. When it does change,
// focus is restored to the same control (matched by its data-item + class).
let reviewPartsHtml = null;

function setReviewParts(html) {
  const box = $("#reviewParts");
  if (html === reviewPartsHtml && box.childElementCount) return false;
  const active = document.activeElement;
  let restore = null;
  if (active && box.contains(active) && active.dataset && active.dataset.item) {
    restore = { item: active.dataset.item, cls: active.classList.contains("review-go") ? "review-go" : "review-q" };
  }
  box.innerHTML = html;
  reviewPartsHtml = html;
  if (restore) {
    const again = [...box.querySelectorAll(`.${restore.cls}`)].find((b) => b.dataset.item === restore.item);
    if (again) again.focus({ preventScroll: true });
  }
  return true;
}

function renderReview() {
  if (!reviewOpen) return;
  const unsavedIds = Object.keys(pending);
  const rejectedList = rejectedIds();
  let total = 0;
  let todo = 0;
  let html = "";
  units.forEach((u, ui) => {
    const items = u.section.items;
    const missing = items.filter((it) => !isAnswered(it.id));
    total += items.length;
    todo += missing.length;
    const block = blocks[u.blockIdx];
    const title = `${block.label} · ${u.section.title || `Part ${ui + 1}`}`;
    const squares = items
      .map((it) => {
        const bad = it.id in rejected && !(it.id in pending);
        const cls = bad ? "bad" : isAnswered(it.id) ? "done" : "todo";
        const saving = it.id in pending ? " (saving…)" : "";
        return (
          `<button type="button" class="review-q ${cls}" data-unit="${ui}" data-item="${esc(it.id)}" ` +
          `aria-label="Go to question ${esc(itemNo[it.id] || "")} — ${esc(navLabel(it.id).split(", ")[1])}${saving}">` +
          `${esc(itemNo[it.id] || "")}</button>`
        );
      })
      .join("");
    const links = missing.length
      ? `<div class="review-todo">Not answered: ` +
        missing
          .map(
            (it) =>
              `<button type="button" class="review-go" data-unit="${ui}" data-item="${esc(it.id)}">Go to question ${esc(itemNo[it.id] || "")}</button>`
          )
          .join("") +
        `</div>`
      : "";
    html +=
      `<div class="review-part" data-unit="${ui}">` +
      `<div class="review-part-head"><span class="title">${esc(title)}</span>` +
      `<span class="count${missing.length ? " warn" : ""}">${items.length - missing.length} of ${items.length} answered</span></div>` +
      `<div class="navgrid">${squares}</div>${links}</div>`;
  });
  const rebuilt = setReviewParts(html);
  const count = $("#reviewCount");
  count.textContent = todo
    ? `${total - todo} of ${total} answered · ${todo} not answered`
    : `All ${total} questions answered`;
  count.className = "review-count" + (todo ? " warn" : "");
  count.dataset.unanswered = String(todo);

  const save = $("#reviewSave");
  const lines = [];
  if (unsavedIds.length) {
    lines.push(
      `⚠ ${unsavedIds.length} answer(s) not saved yet (question ${questionNumbers(unsavedIds)}) — ` +
        (saveFailures ? "check your connection, retrying…" : "saving…")
    );
  }
  if (rejectedList.length) {
    lines.push(
      `⚠ ${rejectedList.length} answer(s) rejected by the server (question ${questionNumbers(rejectedList)}) — go back and change them.`
    );
  }
  save.textContent = lines.length ? lines.join(" ") : "✓ All your answers are saved.";
  save.className = "review-save " + (lines.length ? "warn" : "ok");

  if (!rebuilt) return; // same buttons, handlers still attached
  $("#reviewParts")
    .querySelectorAll("[data-item]")
    .forEach((b) => {
      b.onclick = () => jumpTo(Number(b.dataset.unit), b.dataset.item, { focus: true });
    });
}

// -------------------------------------------------------------------------- //
// Save (displayed -> server maps to canonical) + telemetry answer_change.
// -------------------------------------------------------------------------- //
// Answers are queued per item (`pending` = latest entered value) and a single
// save loop PUTs them, retrying with exponential backoff on network errors / 5xx.
// `answers[itemId]` only becomes the value once the server said 2xx; a value that
// changed while its PUT was in flight is sent again. The dirty map is mirrored to
// localStorage so a reload after a network drop resends it (`loadPending`).
function save(attemptId, itemId, value) {
  if (finished) return;
  const cur = localValue(itemId);
  if (cur === value) return; // nothing new entered (incl. re-entering a rejected value)
  if (cur === undefined && value === "") return; // never answered and still blank
  delete rejected[itemId]; // a new value gets a fresh chance
  pending[itemId] = value;
  persistPending(attemptId);
  // Don't flood telemetry with a colouring data-URL; log a compact marker instead.
  const telemetryValue =
    typeof value === "string" && value.startsWith("data:") ? `[image ${value.length}b]` : value;
  if (rec) rec.answerChange(itemId, telemetryValue);
  refreshNavSquare(itemId);
  renderSaveStatus();
  scheduleSaveLoop(0);
}

function pendingKey(attemptId) {
  return PENDING_KEY + attemptId;
}

// Mirror the dirty map to localStorage. Values above PENDING_MAX_VALUE (in practice
// only colour-task PNG data-URLs, ~100-300 KB) are skipped to stay well inside the
// ~5 MB storage quota: they are still retried in memory for as long as the page
// lives, but a reload after a network drop will NOT resend them.
function persistPending(attemptId) {
  try {
    const keys = Object.keys(pending);
    if (!keys.length) {
      localStorage.removeItem(pendingKey(attemptId));
      return;
    }
    const slim = {};
    for (const k of keys) {
      const v = pending[k];
      if (typeof v === "string" && v.length > PENDING_MAX_VALUE) continue; // too big to mirror
      slim[k] = v;
    }
    localStorage.setItem(pendingKey(attemptId), JSON.stringify(slim));
  } catch (e) {
    /* storage unavailable / quota — the in-memory queue still retries */
  }
}

function loadPending(attemptId) {
  let stored = null;
  try {
    stored = JSON.parse(localStorage.getItem(pendingKey(attemptId)) || "null");
  } catch (e) {
    stored = null;
  }
  if (!stored || typeof stored !== "object") return;
  let n = 0;
  for (const [itemId, value] of Object.entries(stored)) {
    if (answers[itemId] === value) continue; // the server already has it
    pending[itemId] = value;
    if (typeof value === "string" && value.startsWith("data:")) colourPaintUrls[itemId] = value;
    n += 1;
  }
  if (n) console.warn(`[exam] ${n} unsaved answer(s) restored from the previous load — resending`);
  persistPending(attemptId);
}

// Run the loop after `delayMs` unless a run is already scheduled or in flight.
// `delayMs === 0` (a new answer / submit) overrides a pending backoff wait.
function scheduleSaveLoop(delayMs) {
  if (saveRunning || finished || saveHalted) return;
  if (saveTimer !== null) {
    if (delayMs > 0) return; // keep the earlier/backed-off schedule
    clearTimeout(saveTimer);
  }
  saveTimer = setTimeout(runSaveLoop, delayMs);
}

async function runSaveLoop() {
  saveTimer = null;
  if (saveRunning || finished || saveHalted) return;
  const id = attemptId();
  if (!id) return;
  saveRunning = true;
  try {
    while (!finished && !saveHalted && Object.keys(pending).length) {
      const snapshot = Object.entries(pending); // latest value per item at this moment
      let networkFailed = false;
      for (const [itemId, value] of snapshot) {
        if (pending[itemId] !== value) continue; // changed again meanwhile; next pass sends it
        try {
          await api(
            `/attempts/${encodeURIComponent(id)}/answers/${encodeURIComponent(itemId)}`,
            { method: "PUT", body: { response: value } }
          );
          answers[itemId] = value; // persisted — only now
          if (pending[itemId] === value) delete pending[itemId]; // else a newer value waits
          persistPending(id);
          saveFailures = 0;
          renderSaveStatus();
        } catch (e) {
          if (e.status === 409) {
            // Past the deadline / already finalized — the server has the last word.
            saveHalted = true;
            renderSaveStatus();
            if (await resolveFinished(id)) return;
            networkFailed = true; // state says still running: treat as transient
            break;
          }
          if (e.status === 404 || e.status === 400 || e.status === 422) {
            // The server will never accept this one (unknown item / bad shape);
            // retrying can't help — park it in `rejected` so the rest of the queue
            // flows, and surface it (indicator, navigator, submit/done screens).
            console.warn(`[exam] answer for ${itemId} rejected (${e.status}); not retrying`);
            if (pending[itemId] === value) {
              delete pending[itemId];
              rejected[itemId] = value;
              refreshNavSquare(itemId);
            }
            persistPending(id);
            renderSaveStatus(); // show the rejection now, not after the whole pass
            continue;
          }
          networkFailed = true;
          break;
        }
      }
      if (networkFailed) {
        saveFailures += 1;
        const delay = Math.min(30000, 1000 * 2 ** (saveFailures - 1));
        console.warn(
          `[exam] save failed (${saveFailures}x); ${Object.keys(pending).length} answer(s) unsaved, retrying in ${delay} ms`
        );
        renderSaveStatus();
        saveRunning = false;
        scheduleSaveLoop(delay);
        return;
      }
    }
  } finally {
    saveRunning = false;
    renderSaveStatus();
  }
}

// Header indicator: "Saved" / "Saving…" / "⚠ N answers not saved — retrying",
// plus "⚠ N answer(s) rejected by the server" while any rejected item remains.
function renderSaveStatus() {
  const el = $("#saveStatus");
  if (!el) return;
  const n = Object.keys(pending).length;
  const r = rejectedIds().length;
  const parts = [];
  let cls = "ok";
  if (n === 0) {
    parts.push("Saved");
  } else if (saveFailures === 0 && !saveHalted) {
    parts.push("Saving…");
    cls = "busy";
  } else {
    parts.push(`⚠ ${n} answer${n === 1 ? "" : "s"} not saved — retrying`);
    cls = "warn";
  }
  if (r > 0) {
    parts.push(`⚠ ${r} answer${r === 1 ? "" : "s"} rejected by the server`);
    cls = "warn";
  }
  el.textContent = parts.join(" · ");
  el.className = "save-status " + cls;
  renderReview(); // the review panel mirrors the save queue live
}

// Flush debounced text, kick the loop, and wait (bounded) for the queue to drain.
// Returns the item ids still unsaved when the wait ran out.
async function drainSaves(maxMs) {
  flushTextSaves();
  scheduleSaveLoop(0);
  const until = Date.now() + maxMs;
  while (Object.keys(pending).length && Date.now() < until && !saveHalted) {
    await new Promise((r) => setTimeout(r, 100));
  }
  return Object.keys(pending);
}

function questionNumbers(itemIds) {
  return itemIds.map((i) => itemNo[i] || i).join(", ");
}

// Human sentence(s) for the submit confirm / done screen: which answers never
// reached the server (still retrying) and which ones the server refused.
function unsavedSummary(unsaved, rejectedList) {
  const lines = [];
  if (unsaved.length) {
    lines.push(
      `${unsaved.length} answer(s) could not be saved (question ${questionNumbers(unsaved)}).`
    );
  }
  if (rejectedList.length) {
    lines.push(
      `${rejectedList.length} answer(s) were rejected by the server ` +
        `(question ${questionNumbers(rejectedList)}).`
    );
  }
  return lines.join("\n");
}

// A 409 on save/submit means the server already closed the attempt: ask it which
// way (submitted vs. expired) so the done screen says the right thing.
// Returns false if the attempt is in fact still running.
async function resolveFinished(id) {
  let state = null;
  try {
    state = await api(`/attempts/${encodeURIComponent(id)}/state`);
  } catch (e) {
    /* unreachable / gone — fall back to the generic time's-up screen */
  }
  if (state && state.status === "in_progress" && !state.expired) {
    saveHalted = false;
    return false;
  }
  showDone(!!state && state.status === "submitted", { already: true });
  return true;
}

function refreshNavSquare(itemId) {
  const sq = document.querySelector(`.navsq[data-item="${cssId(itemId)}"]`);
  if (sq) {
    sq.classList.toggle("done", isAnswered(itemId));
    sq.setAttribute("aria-label", navLabel(itemId));
  }
}

// -------------------------------------------------------------------------- //
// Navigation + submit.
// -------------------------------------------------------------------------- //
function attemptId() {
  const a = getActive();
  return a ? a.attemptId : curAttempt;
}

function go(delta) {
  flushTextSaves(); // don't leave a debounced keystroke behind on the old part
  idx = Math.max(0, Math.min(units.length - 1, idx + delta));
  renderCurrent(attemptId());
  window.scrollTo({ top: 0, behavior: "smooth" });
}

// The final submit, from the review panel: same confirms as before (unanswered,
// then — after draining the save queue — unsaved / rejected answers).
async function submitTest() {
  const id = attemptId();
  if (!id || finished || submitting) return;
  const unanswered = countUnanswered();
  if (unanswered > 0 && !confirm(`You have ${unanswered} unanswered question(s). Submit anyway?`)) {
    return;
  }
  if (finished || autoSubmitted) return; // the deadline won while the dialog was open
  submitting = true;
  $("#submitBtn").disabled = true;
  $("#reviewErr").textContent = "";
  try {
    // Make sure everything entered has actually reached the server first.
    const unsaved = await drainSaves(5000);
    if (finished || autoSubmitted) return; // closed meanwhile (409 / deadline)
    const rejectedList = rejectedIds();
    if (
      (unsaved.length || rejectedList.length) &&
      !confirm(
        unsavedSummary(unsaved, rejectedList) +
          "\n\n" +
          (unsaved.length
            ? "Check your connection and press Cancel to keep trying, or OK to submit without them."
            : "Press Cancel to change those answers, or OK to submit without them.")
      )
    ) {
      scheduleSaveLoop(0);
      return;
    }
    if (finished || autoSubmitted) return;
    await drainTelemetry(3000);
    if (finished || autoSubmitted) return;
    try {
      const state = await api(`/attempts/${encodeURIComponent(id)}/submit`, { method: "POST" });
      showDone(state.status === "submitted", { unsaved, rejected: rejectedList });
    } catch (e) {
      if (e.status === 409) {
        // Already submitted or expired — the server decides which.
        await resolveFinished(id);
      } else {
        $("#reviewErr").textContent = "Submit failed — try again.";
      }
    }
  } finally {
    submitting = false;
    if (!finished) $("#submitBtn").disabled = false;
  }
}

function countUnanswered() {
  let n = 0;
  for (const u of units) for (const item of u.section.items) if (!isAnswered(item.id)) n += 1;
  return n;
}

let autoSubmitted = false;
async function autoSubmit() {
  if (autoSubmitted || finished) return;
  autoSubmitted = true;
  stopTimers();
  const id = attemptId();
  if (!id) return showDone(false);
  // Push the last ≤600 ms of typing and wait (bounded) for the queue; the server
  // may already reject these as late — that is its call (rule #3), we just try.
  const unsaved = await drainSaves(5000);
  if (finished) return;
  await drainTelemetry(2000);
  if (finished) return;
  try {
    await api(`/attempts/${encodeURIComponent(id)}/submit`, { method: "POST" });
  } catch (e) {
    if (e.status === 409 && (await resolveFinished(id))) return;
    /* expired submits are rejected server-side — that's expected */
  }
  showDone(false, { unsaved, rejected: rejectedIds() });
}

function showDone(submitted, opts) {
  opts = opts || {};
  finished = true;
  stopTimers();
  clearTimeout(saveTimer);
  saveTimer = null;
  localStorage.removeItem(ACTIVE_KEY);
  try {
    localStorage.removeItem(pendingKey(attemptId() || curAttempt));
  } catch (e) {
    /* ignore */
  }
  if (rec) rec.stop();
  if (mainAudioEl) {
    try {
      mainStarted = false;
      mainAudioEl.pause();
    } catch {
      /* ignore */
    }
  }
  reviewOpen = false;
  $("#pick").classList.add("hidden");
  $("#runner").classList.add("hidden");
  $("#navigator").classList.add("hidden");
  $("#review").classList.add("hidden");
  $("#timeWarn").classList.add("hidden");
  $("#examHeader").classList.add("hidden");
  $("#done").classList.remove("hidden");
  if (!submitted) {
    $("#doneIcon").textContent = "⏱";
    $("#doneTitle").textContent = "Time's up";
    $("#doneMsg").textContent =
      "The exam time ended and your answers so far have been recorded.";
  } else if (opts.already) {
    $("#doneTitle").textContent = "Already submitted";
    $("#doneMsg").textContent =
      "This test was submitted earlier. Your answers have been recorded. You can close this tab.";
  }
  const unsaved = opts.unsaved || [];
  const rejectedList = opts.rejected || [];
  if (unsaved.length || rejectedList.length) {
    $("#doneUnsaved").textContent =
      "⚠ " + unsavedSummary(unsaved, rejectedList).replace("\n", " ") + " Please tell your teacher.";
    $("#doneUnsaved").classList.remove("hidden");
  }
}

// -------------------------------------------------------------------------- //
// Boot: resume an active attempt, else show the picker.
// -------------------------------------------------------------------------- //
$("#prevBtn").onclick = () => go(-1);
$("#nextBtn").onclick = () => go(1);
$("#reviewBtn").onclick = showReview;
$("#reviewBackBtn").onclick = hideReview;
$("#submitBtn").onclick = submitTest;
$("#timeWarnClose").onclick = () => $("#timeWarn").classList.add("hidden");

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
