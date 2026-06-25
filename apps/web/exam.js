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
 * supported (skipped on iOS), block copy/cut/paste/contextmenu, and wire the
 * Phase-6 recorder so visibility/blur/answer/audio events reach the ingest sink.
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
const answers = {}; // item_id -> last value sent (for repopulating on nav)
const colourPaintUrls = {}; // colour_task item_id -> paint-only dataURL (re-entry redraw)
let serverDeadline = null; // ms epoch, from /state (authoritative)
let audioProgress = 0; // furthest listening position (seconds), server-authoritative
let tickTimer = null;
let pollTimer = null;
let finished = false;

// Persistent listening audio (survives section re-renders / block re-entry).
let audioBuiltFor = null; // blockIdx whose #audioBar is currently mounted
let mainAudioEl = null;
let mainStarted = false;

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

  $("#pick").classList.add("hidden");
  $("#examHeader").classList.remove("hidden");
  $("#runner").classList.remove("hidden");
  $("#navigator").classList.remove("hidden");
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
      const u = units[idx];
      const first = u && u.section.items[0];
      if (rec && first) rec.interaction(first.id, { blocked: ev });
    });
  }

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

// -------------------------------------------------------------------------- //
// Server-authoritative timer (display-only render; server decides expiry).
// -------------------------------------------------------------------------- //
function syncDeadline(state) {
  serverDeadline = state.deadline ? new Date(state.deadline).getTime() : null;
  if (state && typeof state.audio_progress_seconds === "number") {
    audioProgress = Math.max(audioProgress, state.audio_progress_seconds);
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
  $("#submitBtn").classList.toggle("hidden", !last);

  renderNavigator(attemptId);
}

// -------------------------------------------------------------------------- //
// Audio progress persistence (anti-abuse): the furthest point reached lives on
// the SERVER (see /audio-progress + /state). A refresh — even on Brave with
// cookies/storage cleared, or a different device — resumes from there and can
// never restart from zero or re-run the sound-check.
// -------------------------------------------------------------------------- //
function reportAudioProgress(sec) {
  const id = attemptId();
  const s = Math.floor(sec);
  if (!id || !Number.isFinite(s) || s <= audioProgress) return;
  audioProgress = s; // optimistic; the server still takes the max
  api(`/attempts/${encodeURIComponent(id)}/audio-progress`, {
    method: "POST",
    body: { seconds: s },
  }).catch(() => {});
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
    imgs.map((a) => `<img src="/assets/${esc(a)}" alt="" />`).join("") +
    `</div>`
  );
}

function stimulusHTML(section) {
  const st = section.stimulus;
  if (st.kind === "passage_text" || st.kind === "gapped_text") {
    return `<div class="stimulus">${esc(st.text)}</div>` + contextImagesHTML(st);
  }
  if (st.kind === "image_set") {
    return (
      `<div class="stimulus">` +
      st.asset_ids.map((a) => `<img class="opt-img" src="/assets/${esc(a)}" alt="" />`).join("") +
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
  const prior = answers[item.id];
  const promptImg = item.image
    ? `<img class="prompt-img" src="/assets/${esc(item.image)}" alt="" />`
    : "";
  const head =
    `<div class="qnum">${esc(itemNo[item.id] || "")}</div><p class="prompt">${esc(item.prompt)}</p>` +
    promptImg;
  let control = "";

  if (item.item_type === "single_choice") {
    const allImg = item.options.every((o) => o.kind === "image");
    const opts = item.options
      .map((o, i) => {
        const checked = prior === i ? "checked" : "";
        if (o.kind === "image") {
          return (
            `<label class="opt-tile${checked ? " sel" : ""}">` +
            `<input type="radio" name="opt-${esc(item.id)}" value="${i}" ${checked}/>` +
            `<img src="/assets/${esc(o.asset_id)}" alt="${esc(o.alt || "")}" /></label>`
          );
        }
        return `<label class="opt"><input type="radio" name="opt-${esc(item.id)}" value="${i}" ${checked}/>${esc(o.text)}</label>`;
      })
      .join("");
    // Image options sit in a wrapping grid (one row / 2×4); text options stack.
    control = allImg ? `<div class="opt-grid">${opts}</div>` : opts;
  } else if (item.item_type === "gap_fill") {
    control = `<input type="text" id="ctl-${esc(item.id)}" autocomplete="off" value="${esc(prior || "")}" />`;
  } else if (item.item_type === "matching") {
    const pool = section.stimulus.kind === "matching_pool" ? section.stimulus.options : [];
    // A 2×4 grid of letter buttons (the sentences live in the pool list above).
    control =
      `<div class="match-grid">` +
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
      `<textarea id="ctl-${esc(item.id)}">${esc(prior || "")}</textarea>` +
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
      `<img class="colour-line" id="line-${esc(item.id)}" src="/assets/${esc(item.asset_id)}" alt="picture to colour" />` +
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
// Question navigator: filled square = answered, click to jump.
// -------------------------------------------------------------------------- //
function isAnswered(itemId) {
  const v = answers[itemId];
  return v !== undefined && v !== null && String(v).trim() !== "";
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
        html += `<button class="${cls}" data-unit="${ui}" data-item="${esc(item.id)}" title="${esc(item.prompt).slice(0, 70)}">${esc(itemNo[item.id] || "")}</button>`;
      }
    }
    html += `</div></div>`;
  }
  html +=
    `<div class="navlegend"><span><span class="legend-box done"></span>answered</span>` +
    `<span><span class="legend-box"></span>not answered</span></div>`;
  nav.innerHTML = html;
  nav.querySelectorAll(".navsq").forEach((b) => {
    b.onclick = () => {
      const unit = Number(b.dataset.unit);
      const itemId = b.dataset.item;
      if (unit !== idx) {
        idx = unit;
        renderCurrent(attemptId);
      }
      const el = document.getElementById("q-" + itemId);
      if (el) {
        el.scrollIntoView({ behavior: "smooth", block: "center" });
        el.classList.add("flash");
        setTimeout(() => el.classList.remove("flash"), 1200);
      }
    };
  });
}

// -------------------------------------------------------------------------- //
// Save (displayed -> server maps to canonical) + telemetry answer_change.
// -------------------------------------------------------------------------- //
async function save(attemptId, itemId, value) {
  if (finished) return;
  if (answers[itemId] === value) return;
  answers[itemId] = value;
  // Don't flood telemetry with a colouring data-URL; log a compact marker instead.
  const telemetryValue =
    typeof value === "string" && value.startsWith("data:") ? `[image ${value.length}b]` : value;
  if (rec) rec.answerChange(itemId, telemetryValue);
  refreshNavSquare(itemId);
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

function refreshNavSquare(itemId) {
  const sq = document.querySelector(`.navsq[data-item="${cssId(itemId)}"]`);
  if (sq) sq.classList.toggle("done", isAnswered(itemId));
}

// -------------------------------------------------------------------------- //
// Navigation + submit.
// -------------------------------------------------------------------------- //
function attemptId() {
  const a = getActive();
  return a ? a.attemptId : curAttempt;
}

function go(delta) {
  idx = Math.max(0, Math.min(units.length - 1, idx + delta));
  renderCurrent(attemptId());
  window.scrollTo({ top: 0, behavior: "smooth" });
}

async function submitTest() {
  const id = attemptId();
  if (!id || finished) return;
  const unanswered = countUnanswered();
  if (unanswered > 0 && !confirm(`You have ${unanswered} unanswered question(s). Submit anyway?`)) {
    return;
  }
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
  if (mainAudioEl) {
    try {
      mainStarted = false;
      mainAudioEl.pause();
    } catch {
      /* ignore */
    }
  }
  $("#pick").classList.add("hidden");
  $("#runner").classList.add("hidden");
  $("#navigator").classList.add("hidden");
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
