/* Timed-sections runner (IQ family, Phase 17.5; step-by-step intro, timer
 * prominence and the drawing overlay: Phase 17b).
 *
 * Loaded BEFORE exam.js and shares its global scope (classic scripts): it uses
 * exam.js's `api`, `t`, `esc`, `save`, `localValue`, `itemHTML`, `wireItem`,
 * `drainSaves`, `showDone`, ... at call time only — nothing here runs at load.
 * Every top-level name is prefixed `iq` / `draw` so the two files never collide.
 *
 * When: exam.js `enterExam` asks `GET /exam/attempts/{id}/sections` for a test whose
 * served sections ALL have `time_limit_seconds`; `timed=true` hands the attempt to
 * `iqStart`. Every other test keeps the one-part-at-a-time runner untouched.
 *
 * Flow per subtest, strictly in order (server rules: app/delivery/sections.py):
 *   intro, shown STEP BY STEP (client-side, all inside the server's intro phase):
 *     1 Instructions -> 2 Example (worked samples + the booklet's practice problems,
 *     answered and checked here — never scored, never saved) -> 3 Ready ("N
 *     questions · M min S s · about X s per question", Start). Back between steps.
 *     The server intro cap covers all three screens: its countdown is shown small
 *     ("starts by itself in m:ss"); when it runs out the subtest starts by itself.
 *   -> POST begin -> [study: the stimulus, big, until `study_ends_at`, then removed]
 *   -> answering: GET sections/{sid}/items, the whole subtest on ONE scrolling page,
 *      the big sticky countdown (colour at <= 30 s / <= 10 s, one "30 seconds left"
 *      notice), "Finish this part" (confirm) -> next intro; if the time ran out, a
 *      "Time is up" interstitial first.
 *   After the last section closes the runner submits the attempt.
 * The phase + its end come from the server (`server_now` gives the clock offset);
 * nothing pauses when the page is hidden (golden rule #3). Any 409 (section code in
 * `X-Section-Error`) is answered by re-reading GET /sections — the server's state
 * always wins; a reload resumes from it (study stimulus via GET .../study, items
 * via GET .../items, answers from /answers + the local dirty queue).
 * The study PICTURE has no asset id on the wire: `image_url` is an attempt-token,
 * window-gated route (the public /assets/ refuses study assets), fetched as a blob
 * (an <img src> cannot send X-Attempt-Token) and shown via an object URL that is
 * revoked — and the <img> removed — the moment the study window ends. Study CELLS
 * (`image_urls`, one gated URL per cell) are fetched the same way and laid out as a
 * responsive grid (4 columns on a narrow phone, 5 up to 799 px, 10 from 800 px), so a
 * 20-symbol memory sheet stays legible on a phone.
 *
 * Item views (answers still go through exam.js `save()` — debounced text, queued
 * PUTs): mark_toggle = a row with a big "X" toggle ("1" / "0"); drawing = on the
 * page a NON-capturing thumbnail (picture + the strokes so far; the page scrolls
 * over it freely on a phone); a tap opens the full-screen drawing overlay (fits the
 * screen in either orientation, keeps the aspect, the countdown stays visible):
 * Undo / Clear / Confirm (saves, closes, the thumbnail updates) / Close (discards,
 * asks first when something changed). The section's end auto-confirms an open
 * overlay. Strokes are saved as {"w","h","strokes":[[[x,y],...],...]} in PICTURE
 * pixels (app/grading/drawing.py; points thinned while drawing and simplified to
 * stay well under the grader caps). Same overlay on desktop (one code path).
 * single_choice / gap_fill reuse exam.js's cards in a compact layout. Golden rule
 * #1: the payloads are keyless (no `correct`, no `target`) and nothing here asks
 * for a key. The intro's practice `solution`s are the booklet's PUBLIC answers to
 * unscored practice problems (contract `IntroPractice`).
 */
"use strict";

let iq = null; // the running timed attempt (see iqStart)

const IQ_POLL_MS = 15000; // background re-sync (expiry / another tab)
const IQ_FLUSH_BEFORE_MS = 1500; // push debounced text this long before a deadline
const IQ_END_RETRY_MS = 700; // past a phase end: re-ask the server at most this often
const IQ_WARN_AT_S = 30; // amber clock + the one-time "30 seconds left" notice
const IQ_CRIT_AT_S = 10; // red clock
const IQ_NOTICE_MS = 4000; // the notice hides itself after this

// A test that MAY run in timed-sections mode (the server decides: `timed`).
function iqTimedCandidate(test) {
  return (
    !!test &&
    Array.isArray(test.sections) &&
    test.sections.length > 0 &&
    test.sections.every((s) => s.time_limit_seconds != null)
  );
}

function iqStart(attemptId, test, sectionsState) {
  iq = {
    attemptId,
    test,
    byId: Object.fromEntries(test.sections.map((s) => [s.id, s])),
    order: test.sections.map((s) => s.id),
    offsetMs: 0,
    state: null,
    key: null, // "<sid>:<phase>" on screen
    endMs: null, // server-clock ms of the current phase's end
    phase: null,
    sid: null,
    study: {}, // sid -> ClientStudyStimulus (from begin; dropped after the window)
    studyUrls: [], // object URLs of the study picture / cells on screen (revoked after)
    loaded: {}, // sid -> ClientSection with items (answering)
    step: {}, // sid -> intro step index (client-side; mirrored in sessionStorage)
    practice: {}, // sid -> [{value, result, shown}] (never saved)
    warned: {}, // sid -> the "30 seconds left" notice was shown
    syncing: null,
    again: false,
    nextEndSync: 0,
    flushedKey: null,
    submitting: false,
    noticeTimer: null,
    tick: null,
    poll: null,
  };
  document.body.classList.add("iq-mode");
  $("#runner").classList.add("hidden");
  $("#navigator").classList.add("hidden");
  $("#iqRunner").classList.remove("hidden");
  $("#iqBody").addEventListener("keydown", iqKeydown);
  drawOverlayInit();
  const ok = $("#iqTimeUpOk");
  if (ok) ok.onclick = iqTimeUpClose;
  iq.tick = setInterval(iqTick, 250);
  iq.poll = setInterval(() => iqSync(), IQ_POLL_MS);
  return iqApply(sectionsState);
}

function iqStop() {
  if (!iq) return;
  iqDropStudy();
  drawClose();
  iqTimeUpClose();
  clearInterval(iq.tick);
  clearInterval(iq.poll);
  const box = $("#iqRunner");
  if (box) box.classList.add("hidden");
}

function iqServerNow() {
  return Date.now() + (iq ? iq.offsetMs : 0);
}

function iqClockText(ms) {
  const total = Math.max(0, Math.ceil(ms / 1000));
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
}

function iqLimitText(seconds) {
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return m && s ? `${m} min ${s} s` : m ? `${m} min` : `${s} s`;
}

// "about X per question" for the Ready screen: one decimal below 3 s, whole
// seconds below 90 s, else minutes (rounded to 5 s).
function iqPerQuestionText(limitSeconds, n) {
  if (!(n > 0) || !(limitSeconds > 0)) return "";
  const x = limitSeconds / n;
  if (x < 3) return `${Math.round(x * 10) / 10} s`;
  if (x < 90) return `${Math.round(x)} s`;
  return iqLimitText(Math.round(x / 5) * 5);
}

// Practice check (public booklet answers, never scored): true / false, or null
// when nothing was answered yet.
function iqPracticeNorm(s) {
  return String(s == null ? "" : s)
    .trim()
    .replace(/\s+/g, " ")
    .toLowerCase();
}

function iqPracticeCorrect(p, value) {
  if (value == null || value === "") return null;
  if (p.kind === "text") return iqPracticeNorm(value) === iqPracticeNorm(p.solution);
  if (p.kind === "choice" || p.kind === "mark") return String(value) === String(p.solution);
  return null;
}

// Re-read the server's section state (serialized: one request at a time; a call
// made meanwhile re-runs once after it).
function iqSync() {
  if (!iq || finished) return Promise.resolve();
  if (iq.syncing) {
    iq.again = true;
    return iq.syncing;
  }
  iq.syncing = (async () => {
    try {
      do {
        iq.again = false;
        const ss = await api(`/attempts/${encodeURIComponent(iq.attemptId)}/sections`);
        await iqApply(ss);
      } while (iq && iq.again && !finished);
    } catch (e) {
      if (e.status === 404) {
        iqStop();
        showDone(false);
      }
      /* network hiccup: the countdown keeps running, the next tick / poll retries */
    } finally {
      if (iq) iq.syncing = null;
    }
  })();
  return iq.syncing;
}

function iqPhaseEnd(st) {
  if (!st) return null;
  const iso =
    st.phase === "intro" ? st.intro_ends_at : st.phase === "study" ? st.study_ends_at : st.deadline;
  return iso ? Date.parse(iso) : null;
}

function iqCurStatus() {
  if (!iq || !iq.state || !iq.state.sections) return null;
  return iq.state.sections.find((s) => s.section_id === iq.sid) || null;
}

async function iqApply(ss) {
  if (!iq || finished) return;
  iq.offsetMs = Date.parse(ss.server_now) - Date.now();
  iq.state = ss;
  if (ss.attempt_status === "submitted") return showDone(true, { already: true });
  if (ss.attempt_status === "expired") return showDone(false, { unsaved: Object.keys(pending) });
  if (!ss.timed) return; // cannot happen once started; keep the screen as is
  const cur = ss.sections.find((s) => s.section_id === ss.current_section_id);
  if (!cur) {
    drawOverlaySaveAndClose();
    return iqFinishAll();
  }
  const key = `${cur.section_id}:${cur.phase}`;
  const prev = iq.key && iq.key !== "done" ? { sid: iq.sid, phase: iq.phase } : null;
  iq.endMs = iqPhaseEnd(cur);
  iq.phase = cur.phase;
  iq.sid = cur.section_id;
  if (key !== iq.key) {
    iq.key = key;
    iq.nextEndSync = 0;
    console.info(`[exam] timed section ${cur.section_id} -> ${cur.phase}`);
    // The previous subtest ran out of time (not finished by the student): say so
    // before the next one's instructions.
    if (prev && prev.sid !== cur.section_id && prev.phase !== "intro") {
      const ps = ss.sections.find((s) => s.section_id === prev.sid);
      if (ps && ps.close_reason === "expired") iqTimeUpShow(prev.sid);
    }
    await iqRender(cur, key);
  }
  iqTick();
}

function iqTick() {
  if (!iq || finished || iq.endMs == null) return;
  const ms = iq.endMs - iqServerNow();
  const sec = Math.ceil(Math.max(0, ms) / 1000);
  const cur = iqCurStatus();
  const limit = cur ? cur.time_limit_seconds || 0 : 0;
  let txt;
  let cls = "timer";
  if (iq.phase === "intro") {
    // The subtest's own clock has not started: show its full length, calm.
    txt = iqClockText(limit * 1000);
    cls += " idle";
  } else {
    txt = iqClockText(ms);
    if (iq.phase === "answering") cls += sec <= IQ_CRIT_AT_S ? " crit" : sec <= IQ_WARN_AT_S ? " low" : "";
  }
  for (const el of [$("#timer"), $("#drawOvClock")]) {
    if (!el) continue;
    if (el.textContent !== txt) el.textContent = txt;
    if (el.className !== cls) el.className = cls;
  }
  const label = $("#iqPhase");
  if (label) {
    const want = t(iq.phase === "intro" ? "iqPhaseIntro" : iq.phase === "study" ? "iqPhaseStudy" : "iqPhaseAnswer");
    if (label.textContent !== want) label.textContent = want;
  }
  const auto = $("#iqAuto");
  if (auto && iq.phase === "intro") auto.textContent = t("iqAutoStart", iqClockText(ms));
  // One non-blocking "30 seconds left" per subtest (only when it had more).
  if (iq.phase === "answering" && ms > 0 && sec <= IQ_WARN_AT_S && limit > IQ_WARN_AT_S && !iq.warned[iq.sid]) {
    iq.warned[iq.sid] = true;
    iqNotice(t("iq30Left"));
  }
  // The study window is over by the server clock: the picture goes NOW, not after
  // the re-sync round trip.
  if (iq.phase === "study" && ms <= 0) iqDropStudy();
  // Push the last answers out before the server closes the section (an open
  // drawing overlay is confirmed — the server still decides what counts).
  if (iq.phase === "answering" && ms <= IQ_FLUSH_BEFORE_MS && iq.flushedKey !== iq.key) {
    iq.flushedKey = iq.key;
    drawOverlaySaveAndClose();
    flushTextSaves();
    scheduleSaveLoop(0);
  }
  if (ms <= 0 && Date.now() >= iq.nextEndSync && !iq.syncing) {
    iq.nextEndSync = Date.now() + IQ_END_RETRY_MS;
    iqSync();
  }
}

// A short, non-blocking notice (never takes focus or taps); also read out.
function iqNotice(text) {
  const box = $("#iqNotice");
  if (box) {
    box.textContent = "⏰ " + text;
    box.classList.remove("hidden");
    clearTimeout(iq.noticeTimer);
    iq.noticeTimer = setTimeout(() => box.classList.add("hidden"), IQ_NOTICE_MS);
  }
  const live = $("#timeWarnLive");
  if (live) live.textContent = text;
}

function iqSectionName(sid) {
  const sec = iq.byId[sid];
  const pos = iq.order.indexOf(sid) + 1;
  return (sec && sec.title) || t("partOf", pos, iq.order.length);
}

function iqTimeUpShow(sid) {
  const box = $("#iqTimeUp");
  if (!box) return;
  $("#iqTimeUpMsg").textContent = t("iqTimeUpMsg", iqSectionName(sid));
  box.classList.remove("hidden");
  const live = $("#timeWarnLive");
  if (live) live.textContent = t("iqTimeUpTitle");
  requestAnimationFrame(() => $("#iqTimeUpOk").focus());
}

function iqTimeUpClose() {
  const box = $("#iqTimeUp");
  if (!box || box.classList.contains("hidden")) return;
  box.classList.add("hidden");
  const next = $("#iqNextBtn") || $("#iqBeginBtn");
  if (next) next.focus({ preventScroll: true });
}

// --------------------------------------------------------------------------- //
// Section actions (409 -> resync from the server).
// --------------------------------------------------------------------------- //
async function iqCall(path, opts) {
  try {
    return await api(`/attempts/${encodeURIComponent(iq.attemptId)}${path}`, opts);
  } catch (e) {
    if (e.status === 409) {
      const code = e.headers && e.headers.get ? e.headers.get("X-Section-Error") : null;
      if (code) {
        console.warn(`[exam] section action refused (${code}) — resyncing`);
        iqSync();
      } else {
        await resolveFinished(iq.attemptId); // the attempt itself is over
      }
      return null;
    }
    if (e.status !== 401) {
      const err = $("#iqErr");
      if (err) err.textContent = t("iqActionErr");
    }
    return null;
  }
}

// Remove the study stimulus from the page and free the picture's object URL.
function iqDropStudy() {
  if (!iq) return;
  const box = document.getElementById("iqStudy");
  if (box) box.remove();
  for (const u of iq.studyUrls) URL.revokeObjectURL(u);
  iq.studyUrls = [];
}

// The study picture as an object URL: GET image_url with the attempt token
// (no-store). null on refusal — 409 `study_not_open` re-syncs, 401 = session lost.
async function iqStudyImage(url) {
  const tok = sessionToken();
  let resp;
  try {
    resp = await fetch(url, {
      cache: "no-store",
      headers: {
        "ngrok-skip-browser-warning": "true",
        ...(tok ? { [TOKEN_HEADER]: tok } : {}),
      },
    });
  } catch (_) {
    return null;
  }
  if (!resp.ok) {
    if (resp.status === 401) sessionLost();
    else if (resp.status === 409) iqSync();
    console.warn(`[exam] study picture refused (${resp.status})`);
    return null;
  }
  return URL.createObjectURL(await resp.blob());
}

async function iqBegin(sid, btn) {
  if (btn) btn.disabled = true;
  const r = await iqCall(`/sections/${encodeURIComponent(sid)}/begin`, { method: "POST" });
  if (r && r.study) iq.study[sid] = r.study;
  if (!r && btn) btn.disabled = false;
  await iqSync();
}

async function iqFinishSection(sid, btn) {
  if (!confirm(t("iqFinishConfirm"))) return;
  if (btn) btn.disabled = true;
  await drainSaves(3000);
  if (finished) return;
  await iqCall(`/sections/${encodeURIComponent(sid)}/finish`, { method: "POST" });
  await iqSync();
}

async function iqFinishAll() {
  if (iq.submitting || finished) return;
  iq.submitting = true;
  iq.endMs = null;
  iq.key = "done";
  iqDropStudy();
  $("#iqHead").classList.add("hidden");
  $("#iqBody").innerHTML = `<p class="iq-wait">${esc(t("iqAllDone"))}</p>`;
  $("#timer").textContent = "--:--";
  $("#timer").className = "timer idle";
  const id = iq.attemptId;
  const unsaved = await drainSaves(5000);
  if (finished) return;
  await drainTelemetry(3000);
  if (finished) return;
  try {
    const state = await api(`/attempts/${encodeURIComponent(id)}/submit`, { method: "POST" });
    showDone(state.status === "submitted", { unsaved, rejected: rejectedIds() });
  } catch (e) {
    if (e.status === 409 && (await resolveFinished(id))) return;
    iq.submitting = false; // transient: the next poll tries again
  }
}

// --------------------------------------------------------------------------- //
// Rendering by phase.
// --------------------------------------------------------------------------- //
function iqHeader(sec) {
  const pos = iq.order.indexOf(sec.id) + 1;
  $("#iqHead").classList.remove("hidden");
  $("#iqPart").textContent = t("partOf", pos, iq.order.length);
  $("#iqTitle").textContent = sec.title || "";
}

function iqImages(list, cls = "iq-pic") {
  return (list || [])
    .map((a) => `<img class="${cls}" src="/assets/${esc(a)}" alt="" draggable="false" />`)
    .join("");
}

function iqSamplesHTML(intro) {
  return (intro.samples || [])
    .map(
      (s) =>
        `<div class="iq-sample">${s.text ? `<div class="iq-text">${esc(s.text)}</div>` : ""}${iqImages(s.images)}</div>`
    )
    .join("");
}

// The instructions + samples, folded, above the questions while answering.
function iqIntroHTML(sec) {
  const intro = sec.intro || {};
  const samples = iqSamplesHTML(intro);
  const text = intro.text ? `<div class="iq-text">${esc(intro.text)}</div>` : "";
  const body =
    text + iqImages(intro.images) + (samples ? `<h3 class="iq-h">${esc(t("iqExamples"))}</h3>${samples}` : "");
  if (!body) return "";
  return `<details class="iq-instr"><summary>${esc(t("iqInstructions"))}</summary>${body}</details>`;
}

async function iqRender(st, key) {
  const sec = iq.byId[st.section_id];
  if (!sec) return;
  iqHeader(sec);
  const body = $("#iqBody");
  window.scrollTo({ top: 0 });
  iqDropStudy(); // a new phase / section: no study picture survives it
  drawOverlaySaveAndClose(); // the previous phase's drawing overlay is confirmed
  if (st.phase !== "intro") iqTimeUpClose();
  if (st.phase === "intro") {
    iqRenderIntro(sec, st);
    return;
  }
  if (st.phase === "study") {
    body.innerHTML = `<p class="iq-wait">${esc(t("iqLoading"))}</p>`;
    let stim = iq.study[sec.id];
    if (!stim) stim = await iqCall(`/sections/${encodeURIComponent(sec.id)}/study`);
    if (!iq || iq.key !== key) return; // moved on meanwhile
    if (!stim) return; // 409: iqCall resyncs
    // The picture and / or the cells (each its own gated URL), fetched together.
    const [picUrl, ...cellUrls] = await Promise.all([
      stim.image_url ? iqStudyImage(stim.image_url) : Promise.resolve(null),
      ...(stim.image_urls || []).map((u) => iqStudyImage(u)),
    ]);
    const got = [picUrl, ...cellUrls].filter(Boolean);
    if (!iq || iq.key !== key) {
      got.forEach((u) => URL.revokeObjectURL(u)); // the window ended meanwhile
      return;
    }
    iq.studyUrls = got;
    const cells = cellUrls.length
      ? `<div class="iq-study-grid" id="iqStudyGrid">` +
        cellUrls
          .map((u) => `<div class="iq-study-cell">${u ? `<img src="${esc(u)}" alt="" draggable="false" />` : ""}</div>`)
          .join("") +
        `</div>`
      : "";
    body.innerHTML =
      `<div class="iq-study" id="iqStudy">` +
      `<h3 class="iq-h">${esc(t("iqStudyTitle"))}</h3>` +
      `<p class="muted">${esc(t("iqStudyHint"))}</p>` +
      (stim.text ? `<div class="iq-study-text">${esc(stim.text)}</div>` : "") +
      (picUrl ? `<img class="iq-study-img" src="${esc(picUrl)}" alt="" draggable="false" />` : "") +
      cells +
      `</div>`;
    return;
  }
  // answering
  delete iq.study[sec.id]; // the stimulus is gone for good
  body.innerHTML = `<p class="iq-wait">${esc(t("iqLoading"))}</p>`;
  let full = iq.loaded[sec.id];
  if (!full) {
    full = await iqCall(`/sections/${encodeURIComponent(sec.id)}/items`);
    if (full) iq.loaded[sec.id] = full;
  }
  if (!iq || iq.key !== key) return;
  if (!full) return;
  iqRenderItems(full);
}

// ---- the intro, step by step ---------------------------------------------- //
function iqSteps(sec) {
  const intro = sec.intro || {};
  const example = (intro.samples || []).length > 0 || (intro.practice || []).length > 0;
  return example ? ["instr", "example", "ready"] : ["instr", "ready"];
}

function iqStepKey(sid) {
  return `exam:iqstep:${iq.attemptId}:${sid}`;
}

function iqStepGet(sid) {
  if (iq.step[sid] == null) {
    let v = 0;
    try {
      v = Number(sessionStorage.getItem(iqStepKey(sid))) || 0;
    } catch {
      /* private mode: start at step 1 */
    }
    iq.step[sid] = v;
  }
  return iq.step[sid];
}

function iqStepSet(sid, n) {
  iq.step[sid] = n;
  try {
    sessionStorage.setItem(iqStepKey(sid), String(n));
  } catch {
    /* fine: kept in memory */
  }
}

function iqRenderIntro(sec, st) {
  const body = $("#iqBody");
  const steps = iqSteps(sec);
  const i = Math.max(0, Math.min(iqStepGet(sec.id), steps.length - 1));
  const step = steps[i];
  const label = { instr: t("iqStepInstr"), example: t("iqStepExample"), ready: t("iqStepReady") };
  const dots =
    `<ol class="iq-steps">` +
    steps
      .map(
        (s, k) =>
          `<li class="${k === i ? "cur" : k < i ? "done" : ""}"${k === i ? ' aria-current="step"' : ""}>` +
          `<span class="n">${k + 1}</span><span class="l">${esc(label[s])}</span></li>`
      )
      .join("") +
    `</ol>`;
  let main;
  if (step === "instr") main = iqInstrHTML(sec);
  else if (step === "example") main = iqExampleHTML(sec);
  else main = iqReadyHTML(sec, st);
  const back = i > 0 ? `<button type="button" id="iqBackBtn">${esc(t("back"))}</button>` : "";
  const next =
    step === "ready" ? "" : `<button type="button" class="primary" id="iqNextBtn">${esc(t("next"))}</button>`;
  body.innerHTML =
    dots +
    `<div class="iq-step" data-step="${step}" id="iqStep">${main}</div>` +
    `<p class="err" id="iqErr"></p>` +
    `<div class="nav iq-nav">${back}<span class="spacer"></span>${next}</div>` +
    `<p class="iq-auto muted" id="iqAuto"></p>`;
  const go = (n) => {
    iqStepSet(sec.id, n);
    iqRenderIntro(sec, st);
    window.scrollTo({ top: 0 });
    const h = $("#iqStep .iq-step-title");
    if (h) h.focus({ preventScroll: true });
  };
  const bb = $("#iqBackBtn");
  if (bb) bb.onclick = () => go(i - 1);
  const nb = $("#iqNextBtn");
  if (nb) nb.onclick = () => go(i + 1);
  if (step === "example") iqWirePractice(sec);
  const btn = $("#iqBeginBtn");
  if (btn) btn.onclick = () => iqBegin(sec.id, btn);
  iqTick();
}

function iqInstrHTML(sec) {
  const intro = sec.intro || {};
  return (
    `<h2 class="iq-step-title" tabindex="-1">${esc(t("iqStepInstr"))}</h2>` +
    (sec.title ? `<h3 class="iq-sec-title">${esc(sec.title)}</h3>` : "") +
    (intro.text ? `<div class="iq-text">${esc(intro.text)}</div>` : "") +
    iqImages(intro.images)
  );
}

function iqExampleHTML(sec) {
  const intro = sec.intro || {};
  const samples = iqSamplesHTML(intro);
  const practice = intro.practice || [];
  return (
    `<h2 class="iq-step-title" tabindex="-1">${esc(t("iqStepExample"))}</h2>` +
    (samples ? `<div class="iq-samples">${samples}</div>` : "") +
    (practice.length
      ? `<h3 class="iq-h iq-ptitle">${esc(t("iqPracticeTitle"))}</h3>` +
        `<p class="muted iq-small">${esc(t("iqPracticeHint"))}</p>` +
        `<div class="iq-practice-list">${practice.map((p, k) => iqPracticeHTML(sec, p, k)).join("")}</div>`
      : "")
  );
}

function iqReadyHTML(sec, st) {
  const n = sec.item_count || (sec.items || []).length;
  const limit = st.time_limit_seconds || sec.time_limit_seconds || 0;
  const per = iqPerQuestionText(limit, n);
  const chips = [t("iqQuestions", n), iqLimitText(limit)];
  if (per) chips.push(t("iqPerQuestion", per));
  const study = sec.study
    ? `<p class="iq-ready-study">${esc(t("iqReadyStudy", sec.study.study_seconds))}</p>`
    : "";
  return (
    `<div class="iq-ready">` +
    `<h2 class="iq-step-title" tabindex="-1">${esc(t("iqReadyTitle"))}</h2>` +
    (sec.title ? `<div class="iq-ready-sec">${esc(sec.title)}</div>` : "") +
    `<p class="iq-ready-big" id="iqReadySummary">` +
    chips.map((c) => `<span class="iq-chip">${esc(c)}</span>`).join(`<span class="sep">·</span>`) +
    `</p>` +
    study +
    `<p class="iq-ready-warn">⏱ ${esc(t("iqReadyClock"))}</p>` +
    `<button type="button" class="primary iq-start" id="iqBeginBtn">${esc(t("iqBegin"))}</button>` +
    `</div>`
  );
}

// ---- practice problems (never scored, never saved) ------------------------ //
function iqPState(sid, k, p) {
  if (!iq.practice[sid]) iq.practice[sid] = [];
  if (!iq.practice[sid][k]) {
    iq.practice[sid][k] = { value: p && p.kind === "mark" ? "0" : "", result: null, shown: false };
  }
  return iq.practice[sid][k];
}

function iqPracticeAnswerText(p) {
  if (p.kind === "choice") {
    const o = (p.options || []).find((x) => x.key === p.solution);
    return `(${p.solution})${o && o.text ? " " + o.text : ""}`;
  }
  if (p.kind === "mark") return t(p.solution === "1" ? "iqMarkYes" : "iqMarkNo");
  return p.solution;
}

function iqFeedbackHTML(p, ps) {
  if (p.kind === "drawing") {
    if (!ps.shown) return "";
    return (
      iqImages(p.solution_images, "iq-pic iq-pans") +
      (p.explanation ? `<div class="iq-text">${esc(p.explanation)}</div>` : "")
    );
  }
  if (ps.result === "empty") return `<span class="muted">${esc(t("iqAnswerFirst"))}</span>`;
  if (ps.result == null) return "";
  const head =
    ps.result === "ok"
      ? `<b>${esc(t("iqRight"))}</b>`
      : `<b>${esc(t("iqWrong", iqPracticeAnswerText(p)))}</b>`;
  return head + (p.explanation ? `<div class="iq-text">${esc(p.explanation)}</div>` : "");
}

function iqPracticeHTML(sec, p, k) {
  const ps = iqPState(sec.id, k, p);
  const check = `<button type="button" class="iq-check" data-k="${k}">${esc(t("iqCheck"))}</button>`;
  const prompt =
    p.prompt && p.kind !== "mark" ? `<div class="iq-pprompt" id="ipp-${k}">${esc(p.prompt)}</div>` : "";
  let ctl = "";
  let imgs = p.kind === "drawing" ? "" : iqImages(p.images, "iq-pic iq-ppic");
  if (p.kind === "text") {
    const numeric = /^\d+$/.test(String(p.solution || "").trim());
    ctl =
      `<div class="iq-prow"><input type="text" class="iq-pin" data-k="${k}" autocomplete="off" ` +
      `autocapitalize="off" spellcheck="false" aria-labelledby="ipn-${k} ipp-${k}"` +
      `${numeric ? ' inputmode="numeric"' : ""} value="${esc(ps.value || "")}" />${check}</div>`;
  } else if (p.kind === "choice") {
    ctl =
      `<div class="iq-popts" role="group" aria-labelledby="ipn-${k}">` +
      (p.options || [])
        .map(
          (o) =>
            `<button type="button" class="iq-popt" data-k="${k}" data-key="${esc(o.key)}" ` +
            `aria-pressed="${ps.value === o.key}">(${esc(o.key)})${o.text ? " " + esc(o.text) : ""}</button>`
        )
        .join("") +
      `</div><div class="iq-prow">${check}</div>`;
  } else if (p.kind === "mark") {
    ctl =
      `<div class="iq-prow iq-pmark"><span class="iq-pprompt mono" id="ipp-${k}">${esc(p.prompt || "")}</span>` +
      `<button type="button" class="iq-toggle iq-ptoggle" data-k="${k}" aria-pressed="${ps.value === "1"}" ` +
      `aria-label="${esc(t("iqMarkAria", k + 1))}">X</button>${check}</div>`;
  } else {
    // drawing: a "try it" picture (the overlay, never saved) + "show answer".
    const src = (p.images || [])[0];
    ctl =
      `<p class="muted iq-small">${esc(t("iqTryIt"))}</p>` +
      drawThumbHTML(`p-${k}`, src, null, t("iqDrawOpenPractice", k + 1)) +
      `<div class="iq-prow"><button type="button" class="iq-show" data-k="${k}" aria-expanded="${ps.shown}">` +
      `${esc(t("iqShowAnswer"))}</button></div>`;
  }
  const fbCls = ps.result === "ok" ? " ok" : ps.result === "bad" ? " bad" : "";
  return (
    `<div class="iq-practice" data-k="${k}" id="ipr-${k}">` +
    `<span class="iq-pnum" id="ipn-${k}">${esc(t("iqPracticeN", k + 1))}</span>` +
    `<div class="iq-pbody">${prompt}${imgs}${ctl}` +
    `<div class="iq-fb${fbCls}" id="ipfb-${k}" role="status" aria-live="polite">${iqFeedbackHTML(p, ps)}</div>` +
    `</div></div>`
  );
}

function iqWirePractice(sec) {
  const list = (sec.intro && sec.intro.practice) || [];
  const box = $("#iqStep");
  if (!box) return;
  const refresh = (k) => {
    const p = list[k];
    const ps = iqPState(sec.id, k, p);
    const fb = document.getElementById(`ipfb-${k}`);
    if (!fb) return;
    fb.className = "iq-fb" + (ps.result === "ok" ? " ok" : ps.result === "bad" ? " bad" : "");
    fb.innerHTML = iqFeedbackHTML(p, ps);
  };
  const doCheck = (k) => {
    const p = list[k];
    const ps = iqPState(sec.id, k, p);
    const r = iqPracticeCorrect(p, ps.value);
    ps.result = r == null ? "empty" : r ? "ok" : "bad";
    console.info(`[exam] practice ${sec.id}#${k + 1} checked: ${ps.result}`);
    refresh(k);
  };
  for (const input of box.querySelectorAll(".iq-pin")) {
    const k = Number(input.dataset.k);
    input.addEventListener("input", () => {
      const ps = iqPState(sec.id, k, list[k]);
      ps.value = input.value;
      if (ps.result) {
        ps.result = null;
        refresh(k);
      }
    });
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        doCheck(k);
      }
    });
  }
  for (const b of box.querySelectorAll(".iq-popt")) {
    b.onclick = () => {
      const k = Number(b.dataset.k);
      const ps = iqPState(sec.id, k, list[k]);
      ps.value = b.dataset.key;
      ps.result = null;
      for (const o of box.querySelectorAll(`.iq-popt[data-k="${k}"]`)) {
        o.setAttribute("aria-pressed", String(o === b));
      }
      refresh(k);
    };
  }
  for (const b of box.querySelectorAll(".iq-ptoggle")) {
    b.onclick = () => {
      const k = Number(b.dataset.k);
      const ps = iqPState(sec.id, k, list[k]);
      ps.value = ps.value === "1" ? "0" : "1";
      ps.result = null;
      b.setAttribute("aria-pressed", String(ps.value === "1"));
      refresh(k);
    };
  }
  for (const b of box.querySelectorAll(".iq-check")) b.onclick = () => doCheck(Number(b.dataset.k));
  for (const b of box.querySelectorAll(".iq-show")) {
    b.onclick = () => {
      const k = Number(b.dataset.k);
      const ps = iqPState(sec.id, k, list[k]);
      ps.shown = !ps.shown;
      b.setAttribute("aria-expanded", String(ps.shown));
      refresh(k);
    };
  }
  list.forEach((p, k) => {
    if (p.kind !== "drawing") return;
    const key = `practice:${sec.id}:${k}`;
    if (!draw.store[key]) draw.store[key] = { W: DRAW_DEFAULT_W, H: DRAW_DEFAULT_H, strokes: [] };
    drawThumbWire(`p-${k}`, key, {
      num: t("iqPracticeN", k + 1),
      prompt: p.prompt || "",
      square: null,
      onConfirm: null, // practice: never saved
    });
  });
}

// ---- answering ------------------------------------------------------------ //
function iqRenderItems(sec) {
  const body = $("#iqBody");
  sec.items.forEach((it, i) => {
    itemNo[it.id] = String(i + 1);
  });
  const cards = sec.items.map((it) => iqItemHTML(sec, it)).join("");
  body.innerHTML =
    iqIntroHTML(sec) +
    stimulusHTML(sec) +
    `<div class="iq-items">${cards}</div>` +
    `<p class="err" id="iqErr"></p>` +
    `<div class="nav"><span class="spacer"></span>` +
    `<button type="button" class="primary" id="iqFinishBtn">${esc(t("iqFinish"))}</button></div>`;
  for (const it of sec.items) iqWireItem(sec, it);
  const fb = $("#iqFinishBtn");
  fb.onclick = () => iqFinishSection(sec.id, fb);
  const first = body.querySelector(".iq-items .iq-focus");
  if (first) first.focus({ preventScroll: true });
}

// --------------------------------------------------------------------------- //
// Items.
// --------------------------------------------------------------------------- //
function iqItemHTML(sec, item) {
  const id = esc(item.id);
  const num = esc(itemNo[item.id] || "");
  if (item.item_type === "mark_toggle") {
    const on = localValue(item.id) === "1";
    const img = item.image
      ? `<img class="iq-row-img" src="/assets/${esc(item.image)}" alt="" draggable="false" />`
      : "";
    return (
      `<div class="qcard iq-card iq-row${on ? " marked" : ""}" id="q-${id}">` +
      `<span class="qnum" id="qn-${id}">${num}</span>` +
      `<span class="iq-row-prompt" id="pr-${id}">${esc(item.prompt)}</span>${img}` +
      `<button type="button" class="iq-toggle iq-focus" id="tg-${id}" aria-pressed="${on}" ` +
      `aria-label="${esc(t("iqMarkAria", itemNo[item.id] || ""))}">X</button></div>`
    );
  }
  if (item.item_type === "drawing") {
    const sq = item.mode === "lines" && item.square ? item.square : null;
    return (
      `<div class="qcard iq-card iq-draw" id="q-${id}">` +
      `<div class="qtop"><div class="qnum" id="qn-${id}">${num}</div></div>` +
      `<p class="prompt" id="pr-${id}">${esc(item.prompt)}</p>` +
      drawThumbHTML(item.id, item.asset_id, sq, t("iqDrawOpenAria", itemNo[item.id] || ""), true) +
      `</div>`
    );
  }
  // single_choice (text or picture options), gap_fill (+ optional picture), ...:
  // exam.js's own card, compacted by the .iq-items styles.
  return itemHTML(sec, item).replace('class="qcard"', 'class="qcard iq-card"');
}

function iqWireItem(sec, item) {
  if (item.item_type === "mark_toggle") {
    const btn = document.getElementById(`tg-${item.id}`);
    btn.addEventListener("focus", () => rec && rec.setItem(item.id));
    btn.addEventListener("click", () => {
      const on = btn.getAttribute("aria-pressed") !== "true";
      btn.setAttribute("aria-pressed", String(on));
      btn.closest(".iq-row").classList.toggle("marked", on);
      save(iq.attemptId, item.id, on ? "1" : "0");
    });
    return;
  }
  if (item.item_type === "drawing") {
    drawWire(iq.attemptId, item);
    return;
  }
  wireItem(iq.attemptId, sec, item);
  const card = document.getElementById(`q-${item.id}`);
  const ctl = card && card.querySelector("input[type=text], input[type=radio]:checked, input[type=radio], textarea");
  if (ctl) ctl.classList.add("iq-focus");
}

// Enter in a text box (or on a radio) -> the next question; ↑/↓ between toggle rows.
function iqKeydown(e) {
  const el = e.target;
  if (!el || !el.closest) return;
  const card = el.closest(".iq-card");
  if (!card) return;
  const isText = el.tagName === "INPUT" && el.type === "text";
  const isRadio = el.tagName === "INPUT" && el.type === "radio";
  const isToggle = el.classList.contains("iq-toggle");
  let dir = 0;
  if (e.key === "Enter" && (isText || isRadio)) dir = 1;
  else if (isToggle && e.key === "ArrowDown") dir = 1;
  else if (isToggle && e.key === "ArrowUp") dir = -1;
  if (!dir) return;
  e.preventDefault();
  if (isText) el.blur(); // the blur flushes the debounced save
  const cards = Array.from(document.querySelectorAll("#iqBody .iq-card"));
  const next = cards[cards.indexOf(card) + dir];
  if (!next) {
    const fb = $("#iqFinishBtn");
    if (fb && dir > 0) fb.focus();
    return;
  }
  const target =
    next.querySelector("input[type=radio]:checked") || next.querySelector(".iq-focus, input, button");
  if (target) {
    target.focus();
    next.scrollIntoView({ block: "nearest", behavior: scrollBehavior() });
  }
}

// --------------------------------------------------------------------------- //
// Drawing: page thumbnail (never captures touch) + full-screen overlay.
// --------------------------------------------------------------------------- //
const DRAW_DEFAULT_W = 400; // picture size used until (or when) the picture loads
const DRAW_DEFAULT_H = 300;
const DRAW_MAX_STROKES = 250; // grader: 300
const DRAW_MAX_POINTS = 4000; // grader: 5 000
const DRAW_MAX_CHARS = 200000; // grader: 300 000

// key -> {W, H, strokes} (confirmed strokes, picture pixels); `paint` -> thumbnail painter
const draw = { store: {}, paint: {} };

function drawParse(value) {
  if (typeof value !== "string" || !value.trim()) return null;
  try {
    const d = JSON.parse(value);
    if (!d || !Array.isArray(d.strokes) || !(d.w > 0) || !(d.h > 0)) return null;
    return d;
  } catch {
    return null;
  }
}

function drawDist(a, b) {
  return Math.hypot(a[0] - b[0], a[1] - b[1]);
}

function drawSegDist(p, a, b) {
  const dx = b[0] - a[0];
  const dy = b[1] - a[1];
  const den = dx * dx + dy * dy;
  if (!den) return drawDist(p, a);
  const k = Math.max(0, Math.min(1, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / den));
  return Math.hypot(p[0] - (a[0] + k * dx), p[1] - (a[1] + k * dy));
}

// Ramer-Douglas-Peucker (iterative), keeps the end points.
function drawRdp(pts, eps) {
  if (pts.length < 3) return pts.slice();
  const keep = new Array(pts.length).fill(false);
  keep[0] = keep[pts.length - 1] = true;
  const stack = [[0, pts.length - 1]];
  while (stack.length) {
    const [lo, hi] = stack.pop();
    let best = -1;
    let idx = -1;
    for (let i = lo + 1; i < hi; i++) {
      const d = drawSegDist(pts[i], pts[lo], pts[hi]);
      if (d > best) {
        best = d;
        idx = i;
      }
    }
    if (best > eps && idx > 0) {
      keep[idx] = true;
      stack.push([lo, idx], [idx, hi]);
    }
  }
  return pts.filter((_, i) => keep[i]);
}

// The saved JSON: coordinates rounded to 0.1 px; simplified harder until it fits
// the caps (points / chars) — the grader resamples anyway.
function drawSerialize(w, h, strokes) {
  const r1 = (x) => Math.round(x * 10) / 10;
  let eps = 0;
  for (let round = 0; round < 12; round++) {
    const out = strokes
      .map((st) => (eps ? drawRdp(st, eps) : st).map((p) => [r1(p[0]), r1(p[1])]))
      .filter((st) => st.length);
    const n = out.reduce((a, st) => a + st.length, 0);
    const json = JSON.stringify({ w: r1(w), h: r1(h), strokes: out });
    if (n <= DRAW_MAX_POINTS && json.length <= DRAW_MAX_CHARS) return json;
    eps = eps ? eps * 2 : Math.max(0.5, w / 800);
  }
  return JSON.stringify({ w: r1(w), h: r1(h), strokes: [] }); // unreachable in practice
}

function drawCopy(strokes) {
  return strokes.map((st) => st.map((p) => [p[0], p[1]]));
}

// Paint strokes (picture pixels W x H) onto a canvas covering a box of cssW x cssH.
function drawPaint(canvas, cssW, cssH, W, H, strokes) {
  const dpr = window.devicePixelRatio || 1;
  const cw = Math.max(1, Math.round(cssW * dpr));
  const ch = Math.max(1, Math.round(cssH * dpr));
  if (canvas.width !== cw || canvas.height !== ch) {
    canvas.width = cw;
    canvas.height = ch;
  }
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, cw, ch);
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  ctx.strokeStyle = "#111";
  ctx.lineWidth = Math.max(1.5 * dpr, cw * 0.006);
  const sx = cw / W;
  const sy = ch / H;
  for (const st of strokes) {
    if (!st.length) continue;
    ctx.beginPath();
    ctx.moveTo(st[0][0] * sx, st[0][1] * sy);
    if (st.length === 1) ctx.lineTo(st[0][0] * sx + 0.1, st[0][1] * sy);
    for (const p of st) ctx.lineTo(p[0] * sx, p[1] * sy);
    ctx.stroke();
  }
}

function drawFrameHTML(sq) {
  return sq
    ? `<div class="draw-square" style="left:${sq.x * 100}%;top:${sq.y * 100}%;width:${sq.w * 100}%;height:${sq.h * 100}%"></div>`
    : "";
}

// The page view of a drawing: a button holding the picture + the strokes so far.
// It never captures touch, so the page scrolls over it on a phone.
function drawThumbHTML(domId, assetId, sq, aria, focus = false) {
  const id = esc(domId);
  return (
    `<button type="button" class="draw-thumb${focus ? " iq-focus" : ""}" id="dthumb-${id}" ` +
    `aria-label="${esc(aria)}" data-square="${sq ? esc(JSON.stringify(sq)) : ""}">` +
    `<span class="draw-stage" id="dstage-${id}">` +
    `<img class="draw-pic" src="/assets/${esc(assetId || "")}" alt="" draggable="false" />${drawFrameHTML(sq)}` +
    `<canvas class="draw-ink" id="dcv-${id}" aria-hidden="true"></canvas></span>` +
    `<span class="draw-open">✎ ${esc(t("iqTapToDraw"))}</span></button>`
  );
}

// Wire a thumbnail: keep its aspect = the picture's, paint the confirmed strokes,
// a tap opens the overlay on a copy of them.
function drawThumbWire(domId, key, opts) {
  const btn = document.getElementById(`dthumb-${domId}`);
  const stage = document.getElementById(`dstage-${domId}`);
  const canvas = document.getElementById(`dcv-${domId}`);
  if (!btn || !stage || !canvas) return;
  const pic = stage.querySelector(".draw-pic");
  const setAspect = () => {
    const s = draw.store[key];
    stage.style.aspectRatio = `${s.W} / ${s.H}`;
    stage.style.setProperty("--ar", String(s.W / s.H));
  };
  const paint = () => {
    const s = draw.store[key];
    if (!s || !stage.isConnected) return;
    drawPaint(canvas, stage.clientWidth, stage.clientHeight, s.W, s.H, s.strokes);
  };
  // The picture's real size, once known: rescale what is already drawn into it.
  const adopt = () => {
    const s = draw.store[key];
    const nw = pic.naturalWidth;
    const nh = pic.naturalHeight;
    if (nw && nh && (nw !== s.W || nh !== s.H)) {
      const fx = nw / s.W;
      const fy = nh / s.H;
      s.strokes = s.strokes.map((st) => st.map((p) => [p[0] * fx, p[1] * fy]));
      s.W = nw;
      s.H = nh;
      setAspect();
    }
    paint();
  };
  setAspect();
  if (pic.complete && pic.naturalWidth) adopt();
  else pic.addEventListener("load", adopt);
  pic.addEventListener("error", paint);
  if (window.ResizeObserver) new ResizeObserver(paint).observe(stage);
  else window.addEventListener("resize", paint);
  requestAnimationFrame(paint);
  draw.paint[key] = paint;
  btn.addEventListener("click", () => {
    if (pic.naturalWidth && !(pic.naturalWidth === draw.store[key].W)) adopt();
    drawOpen({
      key,
      src: pic.getAttribute("src"),
      square: opts.square,
      num: opts.num,
      prompt: opts.prompt,
      itemId: opts.itemId || null,
      onConfirm: opts.onConfirm,
      returnFocus: btn,
    });
  });
}

function drawWire(attemptId, item) {
  const key = item.id;
  const prior = drawParse(localValue(item.id));
  if (prior) {
    draw.store[key] = {
      W: prior.w,
      H: prior.h,
      strokes: prior.strokes.filter((s) => Array.isArray(s) && s.length).map((s) => s.map((p) => [p[0], p[1]])),
    };
  } else if (!draw.store[key]) {
    draw.store[key] = { W: DRAW_DEFAULT_W, H: DRAW_DEFAULT_H, strokes: [] };
  }
  const sq = item.mode === "lines" && item.square ? item.square : null;
  drawThumbWire(item.id, key, {
    num: itemNo[item.id] || "",
    prompt: item.prompt,
    square: sq,
    itemId: item.id,
    onConfirm: (s) => save(attemptId, item.id, drawSerialize(s.W, s.H, s.strokes)),
  });
}

// ---- the overlay (one for the page; exam.html #drawOverlay) --------------- //
const dov = {
  wired: false,
  open: false,
  key: null,
  W: DRAW_DEFAULT_W,
  H: DRAW_DEFAULT_H,
  strokes: [],
  cur: null,
  pid: null,
  dirty: false,
  onConfirm: null,
  returnFocus: null,
};

function drawOvFit() {
  if (!dov.open) return;
  const wrap = $("#drawOvWrap");
  const stage = $("#drawOvStage");
  const cs = getComputedStyle(wrap);
  const aw = wrap.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight);
  const ah = wrap.clientHeight - parseFloat(cs.paddingTop) - parseFloat(cs.paddingBottom);
  const ar = dov.W / dov.H;
  let w = Math.max(40, aw);
  let h = w / ar;
  if (h > ah) {
    h = Math.max(40, ah);
    w = h * ar;
  }
  stage.style.width = `${Math.floor(w)}px`;
  stage.style.height = `${Math.floor(h)}px`;
  drawOvRedraw();
}

function drawOvRedraw() {
  const stage = $("#drawOvStage");
  drawPaint($("#drawOvCanvas"), stage.clientWidth, stage.clientHeight, dov.W, dov.H, dov.strokes);
}

function drawOpen(o) {
  drawOverlayInit();
  const s = draw.store[o.key];
  dov.open = true;
  dov.key = o.key;
  dov.W = s.W;
  dov.H = s.H;
  dov.strokes = drawCopy(s.strokes);
  dov.cur = null;
  dov.pid = null;
  dov.dirty = false;
  dov.onConfirm = o.onConfirm;
  dov.returnFocus = o.returnFocus;
  const pic = $("#drawOvPic");
  pic.src = o.src;
  const sq = o.square;
  const frame = $("#drawOvSquare");
  frame.classList.toggle("hidden", !sq);
  if (sq) {
    frame.style.left = `${sq.x * 100}%`;
    frame.style.top = `${sq.y * 100}%`;
    frame.style.width = `${sq.w * 100}%`;
    frame.style.height = `${sq.h * 100}%`;
  }
  $("#drawOvNum").textContent = o.num ? String(o.num) : "";
  $("#drawOvNum").classList.toggle("hidden", !o.num);
  $("#drawOvPrompt").textContent = o.prompt || "";
  $("#drawOvNote").textContent = "";
  $("#drawOverlay").classList.remove("hidden");
  document.documentElement.classList.add("draw-ov-open");
  if (o.itemId && rec) rec.setItem(o.itemId);
  console.info(`[exam] drawing overlay open ${o.key}`);
  iqTick();
  drawOvFit();
  requestAnimationFrame(drawOvFit);
  $("#drawOvCanvas").focus({ preventScroll: true });
}

function drawClose() {
  if (!dov.open) return;
  dov.open = false;
  dov.cur = null;
  $("#drawOverlay").classList.add("hidden");
  document.documentElement.classList.remove("draw-ov-open");
  const back = dov.returnFocus;
  dov.returnFocus = null;
  if (back && back.isConnected) back.focus({ preventScroll: true });
}

function drawOvConfirm() {
  if (!dov.open) return;
  const key = dov.key;
  const s = draw.store[key];
  s.strokes = drawCopy(dov.strokes);
  s.W = dov.W;
  s.H = dov.H;
  const changed = dov.dirty;
  const cb = dov.onConfirm;
  drawClose();
  if (changed && cb) cb(s);
  if (draw.paint[key]) draw.paint[key]();
  console.info(`[exam] drawing overlay confirm ${key} strokes=${s.strokes.length}`);
}

function drawOvCancel() {
  if (!dov.open) return;
  if (dov.dirty && !confirm(t("iqDiscard"))) return;
  drawClose();
}

// The section's time is (nearly) up: keep what is drawn (the server decides).
function drawOverlaySaveAndClose() {
  if (dov.open) drawOvConfirm();
}

function drawOverlayInit() {
  if (dov.wired) return;
  const canvas = $("#drawOvCanvas");
  if (!canvas) return;
  dov.wired = true;
  const minStep = () => Math.max(1, dov.W / 300); // thin the points while drawing
  const at = (e) => {
    const r = canvas.getBoundingClientRect();
    const x = ((e.clientX - r.left) / r.width) * dov.W;
    const y = ((e.clientY - r.top) / r.height) * dov.H;
    return [Math.max(0, Math.min(dov.W, x)), Math.max(0, Math.min(dov.H, y))];
  };
  const points = () => dov.strokes.reduce((a, s) => a + s.length, 0);
  const note = $("#drawOvNote");
  canvas.addEventListener("pointerdown", (e) => {
    if (!dov.open) return;
    if (e.button !== undefined && e.button !== 0) return;
    e.preventDefault();
    if (dov.cur) return; // one pen at a time (a second finger is ignored)
    if (dov.strokes.length >= DRAW_MAX_STROKES) {
      note.textContent = t("iqTooMany");
      return;
    }
    note.textContent = "";
    try {
      canvas.setPointerCapture(e.pointerId);
    } catch {
      /* synthetic events have no capture */
    }
    dov.pid = e.pointerId;
    dov.cur = [at(e)];
    dov.strokes.push(dov.cur);
    dov.dirty = true;
    drawOvRedraw();
  });
  canvas.addEventListener("pointermove", (e) => {
    if (!dov.cur || e.pointerId !== dov.pid) return;
    e.preventDefault();
    const evs = e.getCoalescedEvents ? e.getCoalescedEvents() : [e];
    let moved = false;
    for (const ev of evs.length ? evs : [e]) {
      const p = at(ev);
      if (drawDist(p, dov.cur[dov.cur.length - 1]) < minStep()) continue;
      if (points() >= DRAW_MAX_POINTS) break;
      dov.cur.push(p);
      moved = true;
    }
    if (moved) drawOvRedraw();
  });
  const end = (e) => {
    if (!dov.cur || (e && e.pointerId !== dov.pid)) return;
    dov.cur = null;
    dov.pid = null;
  };
  canvas.addEventListener("pointerup", end);
  canvas.addEventListener("pointercancel", end);
  // Older iOS: a touch on the drawing area never scrolls / zooms the page.
  for (const el of [canvas, $("#drawOvWrap")]) {
    el.addEventListener("touchstart", (e) => e.preventDefault(), { passive: false });
    el.addEventListener("touchmove", (e) => e.preventDefault(), { passive: false });
  }
  $("#drawOvUndo").onclick = () => {
    if (!dov.strokes.length) return;
    dov.strokes.pop();
    dov.dirty = true;
    note.textContent = "";
    drawOvRedraw();
  };
  $("#drawOvClear").onclick = () => {
    if (!dov.strokes.length) return;
    dov.strokes = [];
    dov.dirty = true;
    note.textContent = "";
    drawOvRedraw();
  };
  $("#drawOvOk").onclick = drawOvConfirm;
  $("#drawOvCancel").onclick = drawOvCancel;
  $("#drawOverlay").addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      e.preventDefault();
      drawOvCancel();
    }
  });
  const pic = $("#drawOvPic");
  pic.addEventListener("load", () => {
    // A picture not measured on the page yet: adopt its size when nothing is drawn.
    if (!dov.open || !pic.naturalWidth) return;
    if (!dov.strokes.length && (pic.naturalWidth !== dov.W || pic.naturalHeight !== dov.H)) {
      dov.W = pic.naturalWidth;
      dov.H = pic.naturalHeight;
    }
    drawOvFit();
  });
  // Rotation / resize: refit — the strokes live in picture pixels, nothing is lost.
  window.addEventListener("resize", drawOvFit);
  window.addEventListener("orientationchange", () => setTimeout(drawOvFit, 50));
  if (window.ResizeObserver) new ResizeObserver(drawOvFit).observe($("#drawOvWrap"));
}
