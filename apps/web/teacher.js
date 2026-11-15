/* Teacher dashboard client (Phase 10).
 *
 * Talks to the same-origin /admin API. The teacher signs in once with the shared
 * password (POST /admin/login), and every call carries the returned bearer token.
 * It surfaces the four Phase-10 jobs: review-queue approval (the draft->published
 * human gate), bank management, live roster, and results ranked suspicious-first
 * with a raw-replay detail view next to the advisory verdict (golden rule #6).
 */
"use strict";

const TOKEN_KEY = "teacher_token";
let token = sessionStorage.getItem(TOKEN_KEY);
let currentTestId = null;
let currentAttemptId = null;

const $ = (sel) => document.querySelector(sel);

async function api(path, { method = "GET", body } = {}) {
  const resp = await fetch(`/admin${path}`, {
    method,
    headers: {
      "ngrok-skip-browser-warning": "true", // skip ngrok free interstitial on XHR
      ...(body ? { "Content-Type": "application/json" } : {}),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (resp.status === 401) {
    signOut();
    throw new Error("Session expired — sign in again.");
  }
  if (!resp.ok) throw new Error(`${method} ${path} -> ${resp.status}`);
  return resp.status === 204 ? null : resp.json();
}

// -- auth -------------------------------------------------------------------- //
async function signIn() {
  const password = $("#password").value;
  $("#loginErr").textContent = "";
  try {
    const { token: t } = await api("/login", { method: "POST", body: { password } });
    token = t;
    sessionStorage.setItem(TOKEN_KEY, token);
    showApp();
  } catch (e) {
    $("#loginErr").textContent = "Sign-in failed. Check the password.";
  }
}

function signOut() {
  token = null;
  sessionStorage.removeItem(TOKEN_KEY);
  $("#app").classList.add("hidden");
  $("#login").classList.remove("hidden");
}

function showApp() {
  $("#login").classList.add("hidden");
  $("#app").classList.remove("hidden");
  refresh();
}

// -- bank + review ----------------------------------------------------------- //
async function refresh() {
  await Promise.all([loadReview(), loadTests()]);
  if (currentTestId) await loadRoster(currentTestId);
}

async function loadReview() {
  const drafts = await api("/review");
  const body = $("#reviewTable tbody");
  body.innerHTML = "";
  $("#reviewEmpty").classList.toggle("hidden", drafts.length > 0);
  for (const d of drafts) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${esc(d.title)}</td><td>${d.level}</td>
      <td>${d.section_count}</td><td>${d.item_count}</td>
      <td><button class="primary">Approve &amp; publish</button></td>`;
    tr.querySelector("button").onclick = () => approve(d.test_id);
    body.appendChild(tr);
  }
}

async function approve(testId) {
  await api(`/review/${testId}/approve`, { method: "POST" });
  await refresh();
}

async function loadTests() {
  const tests = await api("/tests");
  const body = $("#testsTable tbody");
  body.innerHTML = "";
  for (const t of tests) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${esc(t.title)}</td><td>${t.level}</td>
      <td><span class="pill ${t.status}">${t.status}</span></td>
      <td class="row">
        <button data-act="view">View test</button>
        <button data-act="open">Roster &amp; results</button>
      </td>`;
    tr.querySelector('[data-act="view"]').onclick = () => viewTest(t.id);
    tr.querySelector('[data-act="open"]').onclick = () => openTest(t);
    body.appendChild(tr);
  }
}

// -- test review (vet the composition, with the answer key) ------------------ //
async function viewTest(testId) {
  const t = await api(`/tests/${testId}`);
  $("#testViewTitle").textContent = `${t.title} (${t.level}, ${t.status}, ${t.duration_minutes} min)`;
  $("#testViewSection").classList.remove("hidden");
  $("#testViewBody").innerHTML = t.sections.map(renderSectionReview).join("");
  $("#testViewSection").scrollIntoView({ behavior: "smooth" });
}

function renderSectionReview(sec) {
  return `<div class="tv-sec">
    <h3>${esc(sec.title || sec.id)} <span class="tag">${esc(sec.skill || "—")}</span></h3>
    ${renderStimulusReview(sec.stimulus)}
    ${sec.items.map(renderItemReview).join("")}
  </div>`;
}

function renderStimulusReview(st) {
  if (!st) return "";
  if (st.kind === "passage_text" || st.kind === "gapped_text") {
    return `<div class="tv-stim">${esc(st.text)}</div>`;
  }
  if (st.kind === "image_set") {
    return `<div>${st.asset_ids.map((a) => `<img class="tv-img" src="/assets/${esc(a)}" alt="" />`).join("")}</div>`;
  }
  if (st.kind === "audio_asset") {
    const preview = st.preview_asset_id
      ? `<div class="hist">Sound-check: <audio controls preload="none" src="/assets/${esc(st.preview_asset_id)}"></audio></div>`
      : "";
    return `<div class="tv-stim">🎧 plays=${st.plays}, locked=${st.locked}
      <div>Main: <audio controls preload="none" src="/assets/${esc(st.asset_id)}"></audio></div>${preview}</div>`;
  }
  if (st.kind === "matching_pool") {
    return `<div class="tv-stim">Pool: ${st.options.map((o) => `<b>${esc(o.key)}</b> ${esc(o.text)}`).join(" · ")}</div>`;
  }
  return "";
}

function renderItemReview(it) {
  let key = "";
  let body = "";
  if (it.item_type === "single_choice") {
    body = it.options
      .map((o) => {
        const isKey = o.key === it.correct;
        const label = o.kind === "image" ? `[image ${esc(o.asset_id)}]` : esc(o.text);
        return `<div class="tv-opt ${isKey ? "ok" : ""}">${isKey ? "✓ " : ""}<b>${esc(o.key)}</b> — ${label}</div>`;
      })
      .join("");
  } else if (it.item_type === "gap_fill") {
    const acc = [...(it.accepted || []), ...(it.accepted_variants || [])];
    key = `<span class="ok qkey">accepted: ${acc.map(esc).join(" / ")}</span>`;
  } else if (it.item_type === "matching") {
    key = `<span class="ok qkey">correct: ${esc(it.correct)}</span>`;
  } else if (it.item_type === "open_writing") {
    key = `<span class="qkey muted">writing · min ${it.word_min} words · ${esc(it.grade_mode || "llm")}</span>`;
    if (it.bullet_points && it.bullet_points.length) {
      body = `<ul>${it.bullet_points.map((b) => `<li>${esc(b)}</li>`).join("")}</ul>`;
    }
  }
  return `<div style="margin:8px 0"><div><span class="tag">${esc(it.id)}</span>${esc(it.prompt)} ${key}</div>${body}</div>`;
}

// -- roster + results -------------------------------------------------------- //
async function openTest(test) {
  currentTestId = test.id;
  $("#rosterTitle").textContent = test.title;
  $("#rosterSection").classList.remove("hidden");
  $("#detailSection").classList.add("hidden");
  await loadRoster(test.id);
}

// Roster entry status -> label (`expired`: the server deadline ended the attempt
// without a submit — finished, like `submitted`).
const ROSTER_STATUS_LABELS = {
  not_started: "not started",
  in_progress: "in progress",
  submitted: "submitted",
  expired: "expired (time up)",
};

function rosterStatusLabel(status) {
  return ROSTER_STATUS_LABELS[status] || status;
}

async function loadRoster(testId) {
  const [roster, results] = await Promise.all([
    api(`/tests/${testId}/roster`),
    api(`/tests/${testId}/results`),
  ]);

  const rbody = $("#rosterTable tbody");
  rbody.innerHTML = "";
  for (const r of roster) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${esc(r.display_name)}</td><td>${esc(rosterStatusLabel(r.status))}</td>
      <td class="muted">${r.attempt_id || "—"}</td>
      <td class="row">
        ${r.attempt_id ? '<button data-act="reopen">Re-open (keep progress)</button>' : ""}
        ${r.attempt_id ? '<button data-act="reset" class="danger">Reset</button>' : ""}
        <button data-act="remove" class="danger">Remove</button>
      </td>`;
    const reopenBtn = tr.querySelector('[data-act="reopen"]');
    if (reopenBtn) reopenBtn.onclick = () => reopenAttempt(r);
    const resetBtn = tr.querySelector('[data-act="reset"]');
    if (resetBtn) resetBtn.onclick = () => resetAttempt(r);
    tr.querySelector('[data-act="remove"]').onclick = () => removeStudent(r);
    rbody.appendChild(tr);
  }

  const body = $("#resultsTable tbody");
  body.innerHTML = "";
  for (const o of results) {
    const hi = o.suspicion_score >= 0.5 ? "hi" : "";
    const finished = o.attempt_status === "submitted" || o.attempt_status === "expired";
    // Offer an in-row re-grade when the finalize failed or never landed.
    const canRegrade = o.report_status === "failed" || (o.report_status === "missing" && finished);
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${esc(o.display_name || o.attempt_id)}</td>
      <td>${o.score} / ${o.max_score}</td>
      <td>${gradingStatus(o.report_status, o.attempt_status, o.report_error)}
          ${canRegrade ? '<button data-act="regrade" class="small">Переоценить</button>' : ""}</td>
      <td>${o.needs_review ? "⚠︎" : ""}</td>
      <td class="susp ${hi}">${o.suspicion_score.toFixed(2)}</td>
      <td class="susp">${o.confidence.toFixed(2)}</td>
      <td>${o.event_count}</td>
      <td><button data-act="detail">Detail</button></td>`;
    tr.querySelector('[data-act="detail"]').onclick = () => loadDetail(o.attempt_id);
    const rg = tr.querySelector('[data-act="regrade"]');
    if (rg) rg.onclick = () => regradeRow(rg, o.attempt_id);
    body.appendChild(tr);
  }
  const missing = results.filter(
    (o) => o.report_status === "failed" || o.report_status === "missing"
  ).length;
  const fm = $("#finalizeMissingBtn");
  if (fm) {
    fm.classList.toggle("hidden", missing === 0);
    fm.textContent = missing ? `Дооценить все (${missing})` : "Дооценить все";
  }
}

// The finalize state chip for the per-attempt grade + skill report (computed once
// on submit). "pending" = the background grade is still expected (⏳); "complete" =
// ✓; "failed" = the finalize errored — the cause is in the tooltip; "missing" = a
// finished attempt whose finalize was lost (restart) and needs a re-grade. A
// student still working shows "—" — there is nothing to grade yet.
function gradingStatus(status, attemptStatus, error) {
  const finished = !attemptStatus || attemptStatus === "submitted" || attemptStatus === "expired";
  if (status === "complete") return '<span class="chip ok">✓ готово</span>';
  if (status === "failed") {
    return `<span class="chip bad" title="${esc(error || "ошибка оценивания")}">⚠ ошибка</span>`;
  }
  if (!finished) return '<span class="chip muted">—</span>';
  if (status === "missing") {
    return '<span class="chip warn" title="Оценка не была записана — переоцените">⚠ нет оценки</span>';
  }
  return '<span class="chip muted">⏳ оценивается…</span>';
}

// Re-grade straight from the results row (failed / missing report).
async function regradeRow(btn, attemptId) {
  btn.disabled = true;
  btn.textContent = "Оцениваю…";
  try {
    await api(`/results/${attemptId}/regrade`, { method: "POST" });
  } catch (e) {
    alert("Не удалось переоценить — попробуйте ещё раз.");
  }
  if (currentTestId) await loadRoster(currentTestId);
}

// Grade every finished attempt of this test whose report is missing or failed.
async function finalizeMissing() {
  const btn = $("#finalizeMissingBtn");
  if (!currentTestId || !btn) return;
  btn.disabled = true;
  const label = btn.textContent;
  btn.textContent = "Оцениваю…";
  try {
    await api(`/tests/${currentTestId}/finalize-missing`, { method: "POST" });
  } catch (e) {
    alert("Не удалось дооценить — попробуйте ещё раз.");
  }
  btn.disabled = false;
  btn.textContent = label;
  await loadRoster(currentTestId);
}

async function loadDetail(attemptId) {
  currentAttemptId = attemptId;
  const d = await api(`/results/${attemptId}`);
  renderDetail(d);
  $("#detailSection").scrollIntoView({ behavior: "smooth" });
}

function renderDetail(d) {
  $("#detailSection").classList.remove("hidden");
  $("#detailBody").innerHTML = `
    <p><strong>${esc(d.display_name || d.attempt_id)}</strong>
       ${gradingStatus(d.report_status, undefined, d.report_error)}
       <button id="regradeBtn" data-attempt="${esc(d.attempt_id)}">Переоценить</button></p>
    <p>Score: <strong>${d.grading.score} / ${d.grading.max_score}</strong>
       ${d.grading.needs_review ? '<span class="pill draft">needs review</span>' : ""}</p>
    ${renderSkills(d.skills || [], d.summary)}
    <p>Advisory verdict — suspicion <strong>${d.verdict.suspicion_score.toFixed(2)}</strong>,
       confidence ${d.verdict.confidence.toFixed(2)}
       ${d.verdict.model_id ? `<span class="muted">(${esc(d.verdict.model_id)})</span>` : '<span class="muted">(no analyst)</span>'}</p>
    <p class="muted">${esc(d.verdict.summary)}</p>
    ${d.verdict.flags.length ? `<p>Flags: ${d.verdict.flags.map(esc).join(", ")}</p>` : ""}
    ${renderWriting(d.responses || [])}
    ${renderDevices(d.devices || [], d.profile)}
    ${renderBreakdown(d.responses || [])}
    ${renderTimeline(d.timeline || [])}
    <details style="margin-top:14px">
      <summary class="muted">${d.event_count} raw events — machine-readable (for scripts / analysers)</summary>
      <pre>${esc(JSON.stringify(d.events, null, 2))}</pre>
    </details>`;
  wireGrading();
  const rb = $("#regradeBtn");
  if (rb) rb.onclick = () => regrade(rb.dataset.attempt);
}

// Per-skill pedagogical feedback (strengths/weaknesses), produced once by Gemini at
// submit and read from the persisted report. The teacher's academic-performance view,
// separate from the advisory cheating verdict (golden rules #2/#6).
function renderSkills(skills, summary) {
  if (!skills.length && !summary) return "";
  const cards = skills
    .map(
      (s) => `<div class="tv-sec">
        <h3>${esc(s.skill)} — <strong>${s.score} / ${s.max_score}</strong></h3>
        ${s.strengths ? `<div><b>Сильные стороны:</b> ${esc(s.strengths)}</div>` : ""}
        ${s.weaknesses ? `<div><b>Над чем работать:</b> ${esc(s.weaknesses)}</div>` : ""}
      </div>`
    )
    .join("");
  return `<h2 style="margin-top:16px">Анализ по навыкам</h2>
    ${summary ? `<p class="hist">${esc(summary)}</p>` : ""}${cards}`;
}

async function regrade(attemptId) {
  const btn = $("#regradeBtn");
  if (btn) { btn.disabled = true; btn.textContent = "Оцениваю…"; }
  try {
    const d = await api(`/results/${attemptId}/regrade`, { method: "POST" });
    renderDetail(d);
    if (currentTestId) await loadRoster(currentTestId);
  } catch (e) {
    alert("Не удалось переоценить — попробуйте ещё раз.");
    if (btn) { btn.disabled = false; btn.textContent = "Переоценить"; }
  }
}

// Each open_writing as its own read-and-grade card (Writing 1, Writing 2, …).
function renderWriting(responses) {
  const ws = responses.filter((r) => r.item_type === "open_writing");
  if (!ws.length) return "";
  const cards = ws
    .map((r, i) => {
      const opts = ['<option value="" disabled' + (r.manual ? "" : " selected") + ">—</option>"];
      for (let p = 0; p <= r.max_points; p++) {
        opts.push(`<option value="${p}" ${r.manual && r.awarded === p ? "selected" : ""}>${p}</option>`);
      }
      const status = r.manual
        ? `<span class="ok">✓ graded ${r.awarded} / ${r.max_points}</span>`
        : '<span class="bad">not graded yet</span>';
      return `<div class="tv-sec">
        <h3>Writing ${i + 1} — ${esc(r.section_title || "")}</h3>
        <div><b>Task:</b> ${esc(r.prompt)}</div>
        ${r.bullet_points.length ? `<ul>${r.bullet_points.map((b) => `<li>${esc(b)}</li>`).join("")}</ul>` : ""}
        ${r.word_min ? `<div class="hist">minimum ${r.word_min} words</div>` : ""}
        <div class="writing">${r.writing_text ? esc(r.writing_text) : "— no answer —"}</div>
        <div class="row" style="margin-top:8px">
          <label>Mark:
            <select data-grade-select data-item="${esc(r.item_id)}">${opts.join("")}</select> / ${r.max_points}
          </label>
          ${status}
        </div>
      </div>`;
    })
    .join("");
  return `<h2 style="margin-top:16px">Writing — read &amp; grade</h2>${cards}`;
}

async function gradeItem(itemId, awarded) {
  try {
    const d = await api(`/results/${currentAttemptId}/grades/${encodeURIComponent(itemId)}`, {
      method: "PUT",
      body: { awarded },
    });
    renderDetail(d);
    // The results table shows the merged score — keep it in step with the mark.
    if (currentTestId) await loadRoster(currentTestId);
  } catch (e) {
    alert("Could not save the mark — try again.");
  }
}

function wireGrading() {
  document.querySelectorAll("[data-grade]").forEach((b) => {
    b.onclick = () => gradeItem(b.dataset.item, Number(b.dataset.awarded));
  });
  document.querySelectorAll("[data-grade-select]").forEach((sel) => {
    sel.onchange = () => {
      if (sel.value !== "") gradeItem(sel.dataset.item, Number(sel.value));
    };
  });
}

function renderTimeline(timeline) {
  if (!timeline.length) return "";
  const rows = timeline
    .map((e) => {
      const t = new Date(e.at).toLocaleTimeString();
      return `<tr class="${e.integrity ? "tl-integrity" : ""}">
        <td class="hist" style="white-space:nowrap">${esc(t)}</td>
        <td>${esc(e.label)}</td>
      </tr>`;
    })
    .join("");
  return `<h2 style="margin-top:16px">Activity timeline — human-readable</h2>
    <table class="timeline"><tbody>${rows}</tbody></table>`;
}

function renderDevices(devices, profile) {
  const changed =
    profile && profile.device_changed
      ? `<p class="bad">⚠︎ Device changed mid-attempt — ${profile.device_count} distinct devices seen.</p>`
      : "";
  if (!devices.length) return changed || '<p class="muted">No device capture recorded.</p>';
  const rows = devices
    .map(
      (d) => `<tr>
        <td>${esc(new Date(d.at).toLocaleString())}</td>
        <td class="hist">${esc(d.server_ip || "—")}</td>
        <td class="hist">${esc(d.platform || "—")} · ${esc(d.screen || "—")} · ${esc(d.timezone || "—")} · ${esc(d.language || "—")}</td>
        <td class="hist">${esc(d.user_agent || "—")}</td>
      </tr>`
    )
    .join("");
  return `<h2 style="margin-top:16px">Device &amp; session</h2>${changed}
    <table><thead><tr><th>At</th><th>IP</th><th>Platform · screen · tz · lang</th><th>User agent</th></tr></thead>
    <tbody>${rows}</tbody></table>`;
}

function renderBreakdown(responses) {
  if (!responses.length) return "";
  const rows = responses.map((r) => {
    const mark =
      r.is_correct === true
        ? '<span class="ok">✓</span>'
        : r.is_correct === false
        ? '<span class="bad">✗</span>'
        : '<span class="muted">—</span>';
    const student =
      r.item_type === "open_writing"
        ? (r.writing_text
            ? `<div class="writing">${esc(r.writing_text)}</div>`
            : '<span class="muted">— no answer —</span>')
        : r.student_answer
        ? esc(r.student_answer)
        : '<span class="muted">— no answer —</span>';
    const correct = r.correct_answer ? esc(r.correct_answer) : '<span class="muted">manual / LLM</span>';
    const hist =
      r.history && r.history.length > 1
        ? `<div class="hist">tried: ${r.history.map((h) => esc(h.value)).join(" → ")}</div>`
        : "";
    // Typed-text answers (gap_fill) get a teacher ✓/✗ override — auto fuzzy match
    // can miss an acceptable spelling. Writing is graded in its own section.
    const override =
      r.item_type === "gap_fill"
        ? `<div class="hist">override:
             <button data-grade data-item="${esc(r.item_id)}" data-awarded="${r.max_points}">✓</button>
             <button data-grade data-item="${esc(r.item_id)}" data-awarded="0">✗</button>
             ${r.manual ? '<span class="ok">manual</span>' : ""}</div>`
        : "";
    return `<tr>
      <td>${mark}</td>
      <td><span class="tag">${esc(r.item_id)}</span>${esc(r.prompt)}</td>
      <td>${student}${hist}</td>
      <td class="ok">${correct}</td>
      <td>${r.awarded}/${r.max_points}${r.needs_review ? ' <span class="pill draft">review</span>' : ""}${override}</td>
    </tr>`;
  });
  return `<h2 style="margin-top:16px">Answers — per question</h2>
    <table class="breakdown"><thead>
      <tr><th></th><th>Question</th><th>Student answer</th><th>Correct</th><th>Pts</th></tr>
    </thead><tbody>${rows.join("")}</tbody></table>`;
}

async function addStudent() {
  const name = $("#studentName").value.trim();
  if (!name || !currentTestId) return;
  await api(`/tests/${currentTestId}/roster`, {
    method: "POST",
    body: { display_name: name },
  });
  $("#studentName").value = "";
  await loadRoster(currentTestId);
}

async function reopenAttempt(r) {
  if (!confirm(`Re-open ${r.display_name}'s attempt? Their answers are kept and they can sign back in — but the deadline keeps running (no extra time).`)) {
    return;
  }
  await api(`/roster/${r.roster_entry_id}/reopen`, { method: "POST" });
  await loadRoster(currentTestId);
}

async function resetAttempt(r) {
  if (!confirm(`Reset ${r.display_name}'s attempt? Their answers and replay are deleted and they can sit the test again.`)) {
    return;
  }
  await api(`/roster/${r.roster_entry_id}/reset`, { method: "POST" });
  await loadRoster(currentTestId);
}

async function removeStudent(r) {
  if (!confirm(`Remove ${r.display_name} from this test? Their attempt (if any) is deleted.`)) {
    return;
  }
  await api(`/roster/${r.roster_entry_id}`, { method: "DELETE" });
  await loadRoster(currentTestId);
}

// -- live monitoring (auto-refresh the roster + results) --------------------- //
let liveTimer = null;
function toggleLive() {
  if ($("#liveToggle").checked) {
    liveTimer = setInterval(() => {
      if (currentTestId) loadRoster(currentTestId);
    }, 5000);
  } else {
    clearInterval(liveTimer);
    liveTimer = null;
  }
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// -- wire up ----------------------------------------------------------------- //
$("#loginBtn").onclick = signIn;
$("#password").addEventListener("keydown", (e) => e.key === "Enter" && signIn());
$("#logoutBtn").onclick = signOut;
$("#refreshBtn").onclick = refresh;
$("#addStudentBtn").onclick = addStudent;
$("#studentName").addEventListener("keydown", (e) => e.key === "Enter" && addStudent());
$("#liveToggle").addEventListener("change", toggleLive);
$("#finalizeMissingBtn").onclick = finalizeMissing;

if (token) showApp();
