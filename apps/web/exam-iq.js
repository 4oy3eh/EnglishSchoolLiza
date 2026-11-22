/* Timed-sections runner (IQ family, Phase 17.5).
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
 *   intro (instructions + samples, "Begin", auto-start countdown to `intro_ends_at`)
 *   -> POST begin -> [study: the stimulus, big, until `study_ends_at`, then removed]
 *   -> answering: GET sections/{sid}/items, the whole subtest on ONE scrolling page,
 *      countdown to `deadline`, "Finish this part" (confirm) -> next intro.
 *   After the last section closes the runner submits the attempt.
 * The phase + its end come from the server (`server_now` gives the clock offset);
 * nothing pauses when the page is hidden (golden rule #3). Any 409 (section code in
 * `X-Section-Error`) is answered by re-reading GET /sections — the server's state
 * always wins; a reload resumes from it (study stimulus via GET .../study, items
 * via GET .../items, answers from /answers + the local dirty queue).
 * The study PICTURE has no asset id on the wire: `image_url` is an attempt-token,
 * window-gated route (the public /assets/ refuses study assets), fetched as a blob
 * (an <img src> cannot send X-Attempt-Token) and shown via an object URL that is
 * revoked — and the <img> removed — the moment the study window ends.
 *
 * Item views (answers still go through exam.js `save()` — debounced text, queued
 * PUTs): mark_toggle = a row with a big "X" toggle ("1" / "0"); drawing = a pen
 * canvas over the picture (pointer events: mouse, touch, pen; Undo / Clear), saved
 * as {"w","h","strokes":[[[x,y],...],...]} in PICTURE pixels (app/grading/drawing.py;
 * points thinned while drawing and simplified to stay well under the grader caps);
 * single_choice / gap_fill reuse exam.js's cards in a compact layout. Golden rule
 * #1: the payloads are keyless (no `correct`, no `target`) and nothing here asks
 * for a key.
 */
"use strict";

let iq = null; // the running timed attempt (see iqStart)

const IQ_POLL_MS = 15000; // background re-sync (expiry / another tab)
const IQ_FLUSH_BEFORE_MS = 1500; // push debounced text this long before a deadline
const IQ_END_RETRY_MS = 700; // past a phase end: re-ask the server at most this often

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
    studyUrl: null, // object URL of the study picture on screen (revoked after)
    loaded: {}, // sid -> ClientSection with items (answering)
    syncing: null,
    again: false,
    nextEndSync: 0,
    flushedKey: null,
    submitting: false,
    tick: null,
    poll: null,
  };
  $("#runner").classList.add("hidden");
  $("#navigator").classList.add("hidden");
  $("#iqRunner").classList.remove("hidden");
  $("#iqBody").addEventListener("keydown", iqKeydown);
  iq.tick = setInterval(iqTick, 250);
  iq.poll = setInterval(() => iqSync(), IQ_POLL_MS);
  return iqApply(sectionsState);
}

function iqStop() {
  if (!iq) return;
  iqDropStudy();
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

async function iqApply(ss) {
  if (!iq || finished) return;
  iq.offsetMs = Date.parse(ss.server_now) - Date.now();
  iq.state = ss;
  if (ss.attempt_status === "submitted") return showDone(true, { already: true });
  if (ss.attempt_status === "expired") return showDone(false, { unsaved: Object.keys(pending) });
  if (!ss.timed) return; // cannot happen once started; keep the screen as is
  const cur = ss.sections.find((s) => s.section_id === ss.current_section_id);
  if (!cur) return iqFinishAll();
  const key = `${cur.section_id}:${cur.phase}`;
  iq.endMs = iqPhaseEnd(cur);
  iq.phase = cur.phase;
  iq.sid = cur.section_id;
  if (key !== iq.key) {
    iq.key = key;
    iq.nextEndSync = 0;
    console.info(`[exam] timed section ${cur.section_id} -> ${cur.phase}`);
    await iqRender(cur, key);
  }
  iqTick();
}

function iqTick() {
  if (!iq || finished || iq.endMs == null) return;
  const ms = iq.endMs - iqServerNow();
  const txt = iqClockText(ms);
  const sec = Math.ceil(Math.max(0, ms) / 1000);
  const cls = "timer" + (iq.phase === "answering" ? (sec <= 10 ? " crit" : sec <= 30 ? " low" : "") : "");
  for (const el of [$("#timer"), $("#iqClock")]) {
    if (!el) continue;
    el.textContent = txt;
    el.className = cls + (el.id === "iqClock" ? " iq-clock" : "");
  }
  const auto = $("#iqAuto");
  if (auto && iq.phase === "intro") auto.textContent = t("iqAutoStart", txt);
  // The study window is over by the server clock: the picture goes NOW, not after
  // the re-sync round trip.
  if (iq.phase === "study" && ms <= 0) iqDropStudy();
  // Push the last typed answers out before the server closes the section.
  if (iq.phase === "answering" && ms <= IQ_FLUSH_BEFORE_MS && iq.flushedKey !== iq.key) {
    iq.flushedKey = iq.key;
    flushTextSaves();
    scheduleSaveLoop(0);
  }
  if (ms <= 0 && Date.now() >= iq.nextEndSync && !iq.syncing) {
    iq.nextEndSync = Date.now() + IQ_END_RETRY_MS;
    iqSync();
  }
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
  if (iq.studyUrl) {
    URL.revokeObjectURL(iq.studyUrl);
    iq.studyUrl = null;
  }
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

function iqIntroHTML(sec, { brief = false } = {}) {
  const intro = sec.intro || {};
  const samples = (intro.samples || [])
    .map(
      (s) =>
        `<div class="iq-sample">${s.text ? `<div class="iq-text">${esc(s.text)}</div>` : ""}${iqImages(s.images)}</div>`
    )
    .join("");
  const text = intro.text ? `<div class="iq-text">${esc(intro.text)}</div>` : "";
  const body =
    text + iqImages(intro.images) + (samples ? `<h3 class="iq-h">${esc(t("iqExamples"))}</h3>${samples}` : "");
  if (!body) return "";
  return brief
    ? `<details class="iq-instr"><summary>${esc(t("iqInstructions"))}</summary>${body}</details>`
    : `<div class="iq-intro">${body}</div>`;
}

function iqLimitText(seconds) {
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return m && s ? `${m} min ${s} s` : m ? `${m} min` : `${s} s`;
}

async function iqRender(st, key) {
  const sec = iq.byId[st.section_id];
  if (!sec) return;
  iqHeader(sec);
  const body = $("#iqBody");
  window.scrollTo({ top: 0 });
  iqDropStudy(); // a new phase / section: no study picture survives it
  if (st.phase === "intro") {
    const study = sec.study ? `<p class="muted">${esc(t("iqStudyNote", sec.study.study_seconds))}</p>` : "";
    body.innerHTML =
      iqIntroHTML(sec) +
      `<p class="iq-limit">${esc(t("iqTimeLimit", iqLimitText(st.time_limit_seconds)))}</p>` +
      study +
      `<p class="muted" id="iqAuto"></p>` +
      `<p class="err" id="iqErr"></p>` +
      `<div class="nav"><span class="spacer"></span>` +
      `<button type="button" class="primary" id="iqBeginBtn">${esc(t("iqBegin"))}</button></div>`;
    const btn = $("#iqBeginBtn");
    btn.onclick = () => iqBegin(sec.id, btn);
    btn.focus();
    return;
  }
  if (st.phase === "study") {
    body.innerHTML = `<p class="iq-wait">${esc(t("iqLoading"))}</p>`;
    let stim = iq.study[sec.id];
    if (!stim) stim = await iqCall(`/sections/${encodeURIComponent(sec.id)}/study`);
    if (!iq || iq.key !== key) return; // moved on meanwhile
    if (!stim) return; // 409: iqCall resyncs
    const picUrl = stim.image_url ? await iqStudyImage(stim.image_url) : null;
    if (!iq || iq.key !== key) {
      if (picUrl) URL.revokeObjectURL(picUrl); // the window ended meanwhile
      return;
    }
    iq.studyUrl = picUrl;
    body.innerHTML =
      `<div class="iq-study" id="iqStudy">` +
      `<h3 class="iq-h">${esc(t("iqStudyTitle"))}</h3>` +
      `<p class="muted">${esc(t("iqStudyHint"))}</p>` +
      (stim.text ? `<div class="iq-study-text">${esc(stim.text)}</div>` : "") +
      (picUrl ? `<img class="iq-study-img" src="${esc(picUrl)}" alt="" draggable="false" />` : "") +
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

function iqRenderItems(sec) {
  const body = $("#iqBody");
  sec.items.forEach((it, i) => {
    itemNo[it.id] = String(i + 1);
  });
  const cards = sec.items.map((it) => iqItemHTML(sec, it)).join("");
  body.innerHTML =
    iqIntroHTML(sec, { brief: true }) +
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
    const frame = sq
      ? `<div class="draw-square" style="left:${sq.x * 100}%;top:${sq.y * 100}%;width:${sq.w * 100}%;height:${sq.h * 100}%"></div>`
      : "";
    return (
      `<div class="qcard iq-card iq-draw" id="q-${id}">` +
      `<div class="qtop"><div class="qnum" id="qn-${id}">${num}</div></div>` +
      `<p class="prompt" id="pr-${id}">${esc(item.prompt)}</p>` +
      `<div class="draw-tools">` +
      `<button type="button" data-act="undo">${esc(t("iqUndo"))}</button>` +
      `<button type="button" data-act="clear">${esc(t("iqClear"))}</button>` +
      `<span class="draw-note muted" role="status"></span></div>` +
      `<div class="draw-stage" id="dstage-${id}">` +
      `<img class="draw-pic" src="/assets/${esc(item.asset_id)}" alt="" draggable="false" />${frame}` +
      `<canvas class="draw-canvas iq-focus" id="dcv-${id}" tabindex="0" role="img" ` +
      `aria-label="${esc(t("iqDrawAria"))}"></canvas></div></div>`
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
// Drawing canvas (pen only).
// --------------------------------------------------------------------------- //
const DRAW_DEFAULT_W = 400; // picture size used until (or when) the picture loads
const DRAW_DEFAULT_H = 300;
const DRAW_MAX_STROKES = 250; // grader: 300
const DRAW_MAX_POINTS = 4000; // grader: 5 000
const DRAW_MAX_CHARS = 200000; // grader: 300 000

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

function drawWire(attemptId, item) {
  const stage = document.getElementById(`dstage-${item.id}`);
  const canvas = document.getElementById(`dcv-${item.id}`);
  const card = document.getElementById(`q-${item.id}`);
  if (!stage || !canvas || !card) return;
  const pic = stage.querySelector(".draw-pic");
  const note = card.querySelector(".draw-note");
  const ctx = canvas.getContext("2d");
  // Logical space = the picture's own pixels (the grader normalizes by w / h).
  let W = DRAW_DEFAULT_W;
  let H = DRAW_DEFAULT_H;
  let strokes = [];
  const prior = drawParse(localValue(item.id));
  if (prior) {
    W = prior.w;
    H = prior.h;
    strokes = prior.strokes.filter((s) => Array.isArray(s) && s.length).map((s) => s.map((p) => [p[0], p[1]]));
  }
  stage.style.aspectRatio = `${W} / ${H}`;

  const redraw = () => {
    const dpr = window.devicePixelRatio || 1;
    const cw = Math.max(1, Math.round(stage.clientWidth * dpr));
    const ch = Math.max(1, Math.round(stage.clientHeight * dpr));
    if (canvas.width !== cw || canvas.height !== ch) {
      canvas.width = cw;
      canvas.height = ch;
    }
    ctx.clearRect(0, 0, cw, ch);
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    ctx.strokeStyle = "#111";
    ctx.lineWidth = Math.max(1.5 * dpr, cw * 0.006);
    const sx = cw / W;
    const sy = ch / H;
    for (const st of strokes) {
      ctx.beginPath();
      ctx.moveTo(st[0][0] * sx, st[0][1] * sy);
      if (st.length === 1) ctx.lineTo(st[0][0] * sx + 0.1, st[0][1] * sy);
      for (const p of st) ctx.lineTo(p[0] * sx, p[1] * sy);
      ctx.stroke();
    }
  };

  // The picture's real size, once known: rescale what is already drawn into it.
  const adopt = () => {
    const nw = pic.naturalWidth;
    const nh = pic.naturalHeight;
    if (!nw || !nh || (nw === W && nh === H)) return redraw();
    const fx = nw / W;
    const fy = nh / H;
    strokes = strokes.map((st) => st.map((p) => [p[0] * fx, p[1] * fy]));
    W = nw;
    H = nh;
    stage.style.aspectRatio = `${W} / ${H}`;
    redraw();
  };
  if (pic.complete && pic.naturalWidth) adopt();
  else pic.addEventListener("load", adopt);
  pic.addEventListener("error", redraw);
  if (window.ResizeObserver) new ResizeObserver(redraw).observe(stage);
  else window.addEventListener("resize", redraw);
  requestAnimationFrame(redraw);

  const commit = () => save(attemptId, item.id, drawSerialize(W, H, strokes));
  const minStep = () => Math.max(1, W / 300); // thin the points while drawing
  const at = (e) => {
    const r = canvas.getBoundingClientRect();
    const x = ((e.clientX - r.left) / r.width) * W;
    const y = ((e.clientY - r.top) / r.height) * H;
    return [Math.max(0, Math.min(W, x)), Math.max(0, Math.min(H, y))];
  };
  const points = () => strokes.reduce((a, s) => a + s.length, 0);

  let cur = null;
  let pid = null;
  canvas.addEventListener("pointerdown", (e) => {
    if (e.button !== undefined && e.button !== 0) return;
    e.preventDefault();
    if (cur) return; // one pen at a time (a second finger is ignored)
    if (rec) rec.setItem(item.id);
    if (strokes.length >= DRAW_MAX_STROKES) {
      note.textContent = t("iqTooMany");
      return;
    }
    note.textContent = "";
    try {
      canvas.setPointerCapture(e.pointerId);
    } catch {
      /* synthetic events have no capture */
    }
    pid = e.pointerId;
    cur = [at(e)];
    strokes.push(cur);
    redraw();
  });
  canvas.addEventListener("pointermove", (e) => {
    if (!cur || e.pointerId !== pid) return;
    e.preventDefault();
    const evs = e.getCoalescedEvents ? e.getCoalescedEvents() : [e];
    let moved = false;
    for (const ev of evs.length ? evs : [e]) {
      const p = at(ev);
      if (drawDist(p, cur[cur.length - 1]) < minStep()) continue;
      if (points() >= DRAW_MAX_POINTS) break;
      cur.push(p);
      moved = true;
    }
    if (moved) redraw();
  });
  const end = (e) => {
    if (!cur || (e && e.pointerId !== pid)) return;
    cur = null;
    pid = null;
    commit();
  };
  canvas.addEventListener("pointerup", end);
  canvas.addEventListener("pointercancel", end);
  // Older iOS: make sure a touch on the canvas never scrolls the page.
  canvas.addEventListener("touchstart", (e) => e.preventDefault(), { passive: false });
  canvas.addEventListener("touchmove", (e) => e.preventDefault(), { passive: false });

  card.querySelector('[data-act="undo"]').onclick = () => {
    if (!strokes.length) return;
    strokes.pop();
    note.textContent = "";
    redraw();
    commit();
  };
  card.querySelector('[data-act="clear"]').onclick = () => {
    if (!strokes.length) return;
    strokes = [];
    note.textContent = "";
    redraw();
    commit();
  };
}
