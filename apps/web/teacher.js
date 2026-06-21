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

const $ = (sel) => document.querySelector(sel);

async function api(path, { method = "GET", body } = {}) {
  const resp = await fetch(`/admin${path}`, {
    method,
    headers: {
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
      <td><button>Roster &amp; results</button></td>`;
    tr.querySelector("button").onclick = () => openTest(t);
    body.appendChild(tr);
  }
}

// -- roster + results -------------------------------------------------------- //
async function openTest(test) {
  currentTestId = test.id;
  $("#rosterTitle").textContent = test.title;
  $("#rosterSection").classList.remove("hidden");
  $("#detailSection").classList.add("hidden");
  await loadRoster(test.id);
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
    tr.innerHTML = `<td>${esc(r.display_name)}</td><td>${r.status}</td>
      <td class="muted">${r.attempt_id || "—"}</td>`;
    rbody.appendChild(tr);
  }

  const body = $("#resultsTable tbody");
  body.innerHTML = "";
  for (const o of results) {
    const hi = o.suspicion_score >= 0.5 ? "hi" : "";
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${esc(o.display_name || o.attempt_id)}</td>
      <td>${o.score} / ${o.max_score}</td>
      <td>${o.needs_review ? "⚠︎" : ""}</td>
      <td class="susp ${hi}">${o.suspicion_score.toFixed(2)}</td>
      <td class="susp">${o.confidence.toFixed(2)}</td>
      <td>${o.event_count}</td>
      <td><button>Detail</button></td>`;
    tr.querySelector("button").onclick = () => loadDetail(o.attempt_id);
    body.appendChild(tr);
  }
}

async function loadDetail(attemptId) {
  const d = await api(`/results/${attemptId}`);
  $("#detailSection").classList.remove("hidden");
  $("#detailBody").innerHTML = `
    <p><strong>${esc(d.display_name || d.attempt_id)}</strong></p>
    <p>Score: <strong>${d.grading.score} / ${d.grading.max_score}</strong>
       ${d.grading.needs_review ? '<span class="pill draft">needs review</span>' : ""}</p>
    <p>Advisory verdict — suspicion <strong>${d.verdict.suspicion_score.toFixed(2)}</strong>,
       confidence ${d.verdict.confidence.toFixed(2)}
       ${d.verdict.model_id ? `<span class="muted">(${esc(d.verdict.model_id)})</span>` : '<span class="muted">(no analyst)</span>'}</p>
    <p class="muted">${esc(d.verdict.summary)}</p>
    ${d.verdict.flags.length ? `<p>Flags: ${d.verdict.flags.map(esc).join(", ")}</p>` : ""}
    <p>${d.event_count} captured events — raw replay (auditable next to the verdict):</p>
    <pre>${esc(JSON.stringify(d.events, null, 2))}</pre>`;
  $("#detailSection").scrollIntoView({ behavior: "smooth" });
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

if (token) showApp();
