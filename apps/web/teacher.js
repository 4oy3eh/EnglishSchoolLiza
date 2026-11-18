/* Teacher dashboard client (Phase 10, hardened in Phase 13).
 *
 * Talks to the same-origin /admin API. The teacher signs in once with the shared
 * password (POST /admin/login), and every call carries the returned bearer token.
 * It surfaces the review-queue approval (the draft->published human gate), bank
 * management, live roster, and results ranked suspicious-first (by the verdict
 * STORED at finalize) with a raw-replay detail view right next to the advisory
 * "Анализ честности" section (golden rule #6). The verdict never changes a grade.
 *
 * Robustness rules (see app/admin/CLAUDE.md):
 * - ONE error path: every action runs through `guard()`, failures become a
 *   dismissible Russian toast (server `detail` is already Russian); nothing is left
 *   as an unhandled rejection.
 * - A 401 anywhere signs out exactly once and stops live polling.
 * - Live polling never overlaps (in-flight flag), stops on sign-out / test switch,
 *   and refreshes the open attempt detail without losing scroll or open <details>.
 * The UI is Russian throughout; identifiers / API stay English.
 */
"use strict";

const TOKEN_KEY = "teacher_token";
const LIVE_INTERVAL_MS = 5000;
let token = sessionStorage.getItem(TOKEN_KEY);
let currentTest = null; // the Test object whose roster/results are open
let currentAttemptId = null; // the attempt shown in the detail panel
let viewGen = 0; // bumped on sign-out / test switch: late responses for an old view are dropped
let lastResults = []; // the results rows in the SERVER's order (suspicious-first)
let lastRoster = [];
let sortState = { key: null, dir: 1 }; // null key = the server's default order

const $ = (sel) => document.querySelector(sel);

// The printable report window reads the token from its opener when the browser
// did not copy sessionStorage into it.
window.__teacherToken = () => token;

// -- labels (Russian UI) ----------------------------------------------------- //
const TEST_STATUS = { draft: "черновик", published: "опубликован" };
const LEVELS = {
  PRE_A1_STARTERS: "Pre A1 Starters",
  A1_MOVERS: "YLE Movers (A1)",
  A2_FLYERS: "A2 Flyers",
  A2_KEY: "A2 Key",
  B1_PRELIMINARY: "B1 Preliminary",
};
const SKILLS = { reading: "Reading (чтение)", listening: "Listening (аудирование)", writing: "Writing (письмо)" };
const ROSTER_STATUS_LABELS = {
  not_started: "не начинал",
  in_progress: "пишет",
  submitted: "сдал",
  expired: "время вышло",
};
const levelLabel = (l) => LEVELS[l] || l;
const skillLabel = (s) => SKILLS[s] || s || "—";
const rosterStatusLabel = (s) => ROSTER_STATUS_LABELS[s] || s;
const fmtDateTime = (iso) => (iso ? new Date(iso).toLocaleString("ru-RU") : "—");
const fmtTime = (iso) => (iso ? new Date(iso).toLocaleTimeString("ru-RU") : "—");

// -- integrity verdict (advisory) labels ------------------------------------- //
// Every code the integrity + analysis code can emit: the rule engine's codes
// (app/integrity/rules.py RULE_TITLES — a rule card shows the server's own Russian
// title; this table labels the same codes when they appear as chips), the writing
// flags (app/integrity/writing.py), the segment kinds (app/analysis/segments.py) and
// the second opinion's signals. Gemini may add its own snake_case extras: those are
// shown as-is (with the code as tooltip).
const FLAG_LABELS = {
  systematic_tab_switching: "систематически уходил со вкладки перед ответами",
  fast_answer_after_return: "ответил сразу после возвращения на вкладку",
  long_time_away: "долго был вне вкладки теста",
  frequent_fullscreen_exits: "часто выходил из полноэкранного режима",
  possible_screenshot: "возможный снимок экрана",
  level_mismatch: "текст заметно сложнее уровня ученика",
  long_hidden: "долго был вне вкладки теста",
  fast_post_return: "отвечал сразу после возвращения на вкладку",
  writing_flagged: "подозрительные признаки при письме",
  device_changed: "продолжил тест на другом устройстве",
  text_appeared_without_typing: "текст появился без набора с клавиатуры",
  scripted_input: "ввод сгенерирован скриптом",
  large_paste_attempt: "попытка вставить большой фрагмент текста",
  typing_speed_implausible: "неправдоподобно быстрая печать",
  away_while_writing: "уходил со страницы во время письма",
  fullscreen_exit_while_writing: "выходил из полноэкранного режима во время письма",
  no_input_telemetry: "текст сохранён без зафиксированного набора",
  instruction_injection_attempt: "попытка дать указания проверяющему в тексте",
  telemetry_gap: "пробел в записи телеметрии — данные неполные",
};
// Own keys only: a model-supplied code such as "constructor" must not resolve to
// an Object.prototype member. The result is still passed through esc() by callers.
const flagLabel = (f) => (Object.hasOwn(FLAG_LABELS, f) ? FLAG_LABELS[f] : f);
// Thresholds for the suspicion chip / bars and the "✍ AI?" marker (advisory).
const SUSPICION_MID = 0.3;
const SUSPICION_HIGH = 0.6;
const AI_ASSIST_MARK = 0.6;
const levelClass = (x) => (x >= SUSPICION_HIGH ? "bad" : x >= SUSPICION_MID ? "warn" : "ok");
// Numbers from the server are formatted here and still pass through esc().
const fix2 = (x) => Number(x).toFixed(2);
const levelWord = (x) => (x >= SUSPICION_HIGH ? "высокая" : x >= SUSPICION_MID ? "средняя" : "низкая");
// Rule severities (contracts RuleSeverity). Own keys only: the severity is server data.
const SEVERITY_LABELS = { strong: "сильный сигнал", medium: "средний сигнал", weak: "слабый сигнал" };
const severityKey = (s) => (Object.hasOwn(SEVERITY_LABELS, s) ? s : "weak");
const fmtSecs = (ms) => {
  const s = Math.round((ms || 0) / 1000);
  return s >= 60 ? `${Math.floor(s / 60)} мин ${s % 60} с` : `${s} с`;
};

// -- errors + toasts ----------------------------------------------------------- //
class ApiError extends Error {
  constructor(message, status, { silent = false, detail = null } = {}) {
    super(message);
    this.status = status;
    this.silent = silent; // already handled (401 -> signed out): no toast
    this.detail = detail;
  }
}

function toast(message, kind = "info", { timeout } = {}) {
  const box = $("#toasts");
  // Don't stack the same message (a failing live poll would repeat every 5 s).
  for (const el of box.children) {
    if (el.dataset.msg === message) return el;
  }
  while (box.children.length >= 4) box.firstChild.remove();
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.dataset.msg = message;
  el.setAttribute("role", kind === "error" ? "alert" : "status");
  el.innerHTML = `<span class="msg"></span><button type="button" aria-label="Закрыть" title="Закрыть">×</button>`;
  el.querySelector(".msg").textContent = message;
  el.querySelector("button").onclick = () => el.remove();
  box.appendChild(el);
  const ms = timeout ?? (kind === "error" ? 0 : 4000); // errors stay until dismissed
  if (ms) setTimeout(() => el.remove(), ms);
  return el;
}

function showError(err, context) {
  if (err && err.silent) return;
  const msg = (err && err.message) || String(err);
  toast(context ? `${context}: ${msg}` : msg, "error");
}

// Wrap an async UI action: any failure becomes a toast, never an unhandled rejection.
function guard(context, fn) {
  return async (...args) => {
    try {
      return await fn(...args);
    } catch (e) {
      showError(e, context);
      return undefined;
    }
  };
}

// Safety net for anything that slipped past `guard`.
window.addEventListener("unhandledrejection", (ev) => {
  ev.preventDefault();
  showError(ev.reason, "Ошибка");
});

async function readDetail(resp) {
  try {
    const text = await resp.text();
    try {
      const data = JSON.parse(text);
      const d = data && data.detail;
      if (typeof d === "string") return d;
      if (Array.isArray(d)) return d.map((x) => x.msg || JSON.stringify(x)).join("; ");
      return d ? JSON.stringify(d) : text;
    } catch (_) {
      return text;
    }
  } catch (_) {
    return "";
  }
}

function humanMessage(status, detail) {
  if (status === 422) return `Неверные данные запроса${detail ? ` (${detail})` : ""}.`;
  if (status >= 500 && status !== 503) {
    return `Ошибка сервера (${status}). Попробуйте ещё раз; если повторяется — сообщите администратору.`;
  }
  if (detail) return detail; // the server's teacher-facing (Russian) message
  if (status === 404) return "Не найдено (возможно, уже удалено).";
  if (status === 409) return "Действие сейчас невозможно.";
  return `Запрос не выполнен (код ${status}).`;
}

// `form` (a FormData) sends a multipart upload — the browser sets its own boundary.
async function api(path, { method = "GET", body, raw = false, form } = {}) {
  const gen = viewGen;
  let resp;
  try {
    resp = await fetch(`/admin${path}`, {
      method,
      headers: {
        "ngrok-skip-browser-warning": "true", // skip ngrok free interstitial on XHR
        ...(body ? { "Content-Type": "application/json" } : {}),
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: form || (body ? JSON.stringify(body) : undefined),
    });
  } catch (_) {
    throw new ApiError("Нет связи с сервером. Проверьте интернет и попробуйте ещё раз.", 0);
  }
  if (resp.status === 401 && path !== "/login") {
    // Only the view that sent the request may sign out (a stale poll can't log the
    // teacher out of a fresh session), and only once.
    if (gen === viewGen) signOut("Сессия истекла — войдите заново.");
    throw new ApiError("Сессия истекла — войдите заново.", 401, { silent: true });
  }
  if (!resp.ok) {
    const detail = await readDetail(resp);
    throw new ApiError(humanMessage(resp.status, detail), resp.status, { detail });
  }
  if (raw) return resp;
  return resp.status === 204 ? null : resp.json();
}

// -- auth -------------------------------------------------------------------- //
async function signIn() {
  const password = $("#password").value;
  const errEl = $("#loginErr");
  errEl.textContent = "";
  if (!password) {
    errEl.textContent = "Введите пароль.";
    return;
  }
  const btn = $("#loginBtn");
  btn.disabled = true;
  try {
    const { token: t } = await api("/login", { method: "POST", body: { password } });
    token = t;
    sessionStorage.setItem(TOKEN_KEY, token);
    $("#password").value = "";
    showApp();
  } catch (e) {
    if (e.status === 401) errEl.textContent = "Неверный пароль.";
    else if (e.status === 0) errEl.textContent = "Нет связи с сервером — проверьте интернет и попробуйте ещё раз.";
    else errEl.textContent = e.message; // 429 (too many attempts) / 503 (server misconfigured) / other
  } finally {
    btn.disabled = false;
  }
}

function signOut(message) {
  if (!token && $("#app").classList.contains("hidden")) return; // already signed out: once
  viewGen++;
  stopLive();
  token = null;
  sessionStorage.removeItem(TOKEN_KEY);
  currentTest = null;
  currentAttemptId = null;
  $("#rosterSection").classList.add("hidden");
  $("#detailSection").classList.add("hidden");
  $("#testViewSection").classList.add("hidden");
  if (typeof closeEditor === "function") closeEditor();
  $("#app").classList.add("hidden");
  $("#login").classList.remove("hidden");
  $("#loginErr").textContent = message || "";
}

function showApp() {
  $("#login").classList.add("hidden");
  $("#app").classList.remove("hidden");
  guard("Не удалось загрузить данные", refresh)();
}

// -- bank + review ----------------------------------------------------------- //
async function refresh() {
  // The server clock first: the bank's window labels are computed against it.
  await loadServerTime().catch(() => null);
  await Promise.all([loadReview(), loadTests()]);
  if (currentTest) {
    await loadRoster();
    if (currentAttemptId) await refreshDetail();
  }
}

async function loadReview() {
  const drafts = await api("/review");
  const body = $("#reviewTable tbody");
  body.innerHTML = "";
  $("#reviewEmpty").classList.toggle("hidden", drafts.length > 0);
  for (const d of drafts) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${esc(d.title)}</td><td>${esc(levelLabel(d.level))}</td>
      <td>${esc(d.section_count)}</td><td>${esc(d.item_count)}</td>
      <td><div class="actions">
        <button data-act="view">Просмотр</button>
        <button class="primary" data-act="approve">Одобрить и опубликовать</button>
      </div></td>`;
    tr.querySelector('[data-act="view"]').onclick = guard("Не удалось открыть тест", () => viewTest(d.test_id));
    tr.querySelector('[data-act="approve"]').onclick = guard("Не удалось опубликовать", () => approve(d));
    body.appendChild(tr);
  }
}

async function approve(d) {
  if (!confirm(`Опубликовать «${d.title}»? После этого ученики смогут открыть тест по ссылке.`)) return;
  await api(`/review/${encodeURIComponent(d.test_id)}/approve`, { method: "POST" });
  toast(`Тест «${d.title}» опубликован.`, "ok");
  await refresh();
}

async function loadTests() {
  const tests = await api("/tests");
  const body = $("#testsTable tbody");
  body.innerHTML = "";
  $("#testsEmpty").classList.toggle("hidden", tests.length > 0);
  for (const t of tests) {
    const published = t.status === "published";
    const tr = document.createElement("tr");
    tr.dataset.testId = t.id;
    const win = windowLabel(t);
    tr.innerHTML = `<td>${esc(t.title)}</td><td>${esc(levelLabel(t.level))}</td>
      <td><span class="pill ${esc(t.status)}">${esc(TEST_STATUS[t.status] || t.status)}</span></td>
      <td class="win-cell ${esc(win.cls)}">${esc(win.text)}</td>
      <td><div class="actions">
        <button data-act="view">Просмотр</button>
        <button data-act="edit" title="Исправить вопросы, ключи, тексты, картинки и аудио">Редактировать</button>
        <button data-act="export" title="Скачать тест вместе с ключом ответов (JSON)">Экспорт JSON</button>
        <button data-act="open">Ученики и результаты</button>
        ${published ? '<button data-act="link" title="Одна ссылка на тест для всех учеников">Скопировать ссылку для учеников</button>' : ""}
        ${published ? '<button data-act="unpublish">Снять с публикации</button>' : ""}
        <button data-act="delete" class="danger">Удалить</button>
      </div></td>`;
    tr.querySelector('[data-act="view"]').onclick = guard("Не удалось открыть тест", () => viewTest(t.id));
    tr.querySelector('[data-act="open"]').onclick = guard("Не удалось открыть результаты", () => openTest(t));
    tr.querySelector('[data-act="edit"]').onclick = guard("Не удалось открыть редактор", () => openEditor(t.id));
    tr.querySelector('[data-act="export"]').onclick = guard("Не удалось скачать тест", () => exportTest(t));
    const link = tr.querySelector('[data-act="link"]');
    if (link) link.onclick = guard("Не удалось скопировать ссылку", () => copyStudentLink(t));
    const unpub = tr.querySelector('[data-act="unpublish"]');
    if (unpub) unpub.onclick = guard("Не удалось снять с публикации", () => unpublishTest(t));
    tr.querySelector('[data-act="delete"]').onclick = guard("Не удалось удалить тест", () => deleteTest(t));
    body.appendChild(tr);
  }
  // Keep the open test's object (title/status) current.
  if (currentTest) {
    const fresh = tests.find((t) => t.id === currentTest.id);
    if (fresh) {
      currentTest = fresh;
      updateRosterHeader();
    }
  }
}

// -- JSON export / import (the "no Python per test" path) -------------------- //
async function exportTest(t) {
  const resp = await api(`/tests/${encodeURIComponent(t.id)}/export.json`, { raw: true });
  const blob = await resp.blob();
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `${t.id}.json`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  toast(`Файл ${t.id}.json скачан (с ключом ответов — не давайте его ученикам).`, "ok");
}

async function importTest() {
  const jsonFile = $("#importJson").files[0];
  if (!jsonFile) {
    toast("Выберите файл теста (JSON).", "error");
    return;
  }
  const form = new FormData();
  form.append("test", jsonFile, jsonFile.name);
  for (const f of $("#importAssets").files) form.append("assets", f, f.name);
  const params = new URLSearchParams();
  if ($("#importReplace").checked) params.set("replace", "true");
  const newId = $("#importNewId").value.trim();
  if (newId) params.set("new_id", newId);
  const qs = params.toString() ? `?${params}` : "";
  const res = await api(`/tests/import${qs}`, { method: "POST", form });
  let msg = `Тест «${res.title}» импортирован как черновик (${res.section_count} разделов, ` +
    `${res.item_count} вопросов). Проверьте его и одобрите в «Проверке черновиков».`;
  if (res.unpublished) {
    msg += "\nВнимание: тест снят с публикации и ждёт одобрения — ученики не смогут " +
      "его начать, пока вы не одобрите его снова.";
  }
  if (res.ignored_assets.length) msg += `\nНе использованы файлы: ${res.ignored_assets.join(", ")}`;
  toast(msg, res.unpublished ? "info" : "ok", { timeout: res.unpublished ? 20000 : 8000 });
  $("#importJson").value = "";
  $("#importAssets").value = "";
  $("#importNewId").value = "";
  $("#importReplace").checked = false;
  await refresh();
}

// The link the student runner reads: apps/web/exam.js takes `?test=<id>`.
function studentLink(testId) {
  return `${location.origin}/web/exam.html?test=${encodeURIComponent(testId)}`;
}

async function copyStudentLink(t) {
  const link = studentLink(t.id);
  let copied = false;
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(link);
      copied = true;
    }
  } catch (_) {
    copied = false;
  }
  if (!copied) {
    // Fallback for http:// (no Clipboard API): a hidden textarea + execCommand.
    const ta = document.createElement("textarea");
    ta.value = link;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    try {
      copied = document.execCommand("copy");
    } catch (_) {
      copied = false;
    }
    ta.remove();
  }
  if (copied) toast(`Ссылка для учеников скопирована:\n${link}`, "ok", { timeout: 6000 });
  else window.prompt("Скопируйте ссылку для учеников вручную:", link);
}

async function unpublishTest(t) {
  const stats = await api(`/tests/${encodeURIComponent(t.id)}/stats`);
  let force = false;
  if (stats.active_count > 0) {
    if (!confirm(
      `Тест «${t.title}» сейчас пишут ученики: ${stats.active_count}.\n` +
      "Если снять его с публикации, их работа прервётся. Всё равно снять с публикации?"
    )) return;
    force = true;
  } else if (!confirm(`Снять «${t.title}» с публикации? Тест вернётся в черновики, новые ученики не смогут его открыть.`)) {
    return;
  }
  await api(`/tests/${encodeURIComponent(t.id)}/unpublish${force ? "?force=true" : ""}`, { method: "POST" });
  toast(`Тест «${t.title}» снят с публикации.`, "ok");
  await refresh();
}

async function deleteTest(t) {
  const stats = await api(`/tests/${encodeURIComponent(t.id)}/stats`);
  let force = false;
  if (stats.attempt_count > 0) {
    if (!confirm(
      `Удалить тест «${t.title}»?\n\nВместе с ним будут БЕЗВОЗВРАТНО удалены работы учеников: ` +
      `${stats.attempt_count} (ответы, оценки, отчёты, записи действий) и список из ${stats.roster_count} учеников.`
    )) return;
    force = true;
  } else if (!confirm(
    `Удалить тест «${t.title}»?` +
    (stats.roster_count ? ` Список учеников (${stats.roster_count}) тоже будет удалён.` : "") +
    " Это действие нельзя отменить."
  )) {
    return;
  }
  // Without force the server re-checks: if a student started meanwhile it refuses
  // (409) and the toast explains it — nothing is wiped behind the teacher's back.
  await api(`/tests/${encodeURIComponent(t.id)}${force ? "?force=true" : ""}`, { method: "DELETE" });
  toast(`Тест «${t.title}» удалён.`, "ok");
  if (currentTest && currentTest.id === t.id) closeTest();
  $("#testViewSection").classList.add("hidden");
  if (typeof closeEditor === "function" && editorTest && editorTest.id === t.id) closeEditor();
  await refresh();
}

// -- test review (vet the composition, with the answer key) ------------------ //
async function viewTest(testId) {
  const t = await api(`/tests/${encodeURIComponent(testId)}`);
  $("#testViewTitle").textContent =
    `${t.title} (${levelLabel(t.level)}, ${TEST_STATUS[t.status] || t.status}, ${t.duration_minutes} мин)`;
  $("#testViewSection").classList.remove("hidden");
  $("#testViewBody").innerHTML = t.sections.map(renderSectionReview).join("");
  $("#testViewSection").scrollIntoView({ behavior: "smooth" });
}

function renderSectionReview(sec) {
  return `<div class="tv-sec">
    <h3>${esc(sec.title || sec.id)} <span class="tag">${esc(skillLabel(sec.skill))}</span></h3>
    ${renderStimulusReview(sec.stimulus)}
    ${sec.items.map(renderItemReview).join("")}
  </div>`;
}

// An asset id is one path segment: percent-encode it (no "../", "?", "#" escaping
// /assets/), then HTML-escape the whole attribute value.
const assetUrl = (id) => esc(`/assets/${encodeURIComponent(String(id ?? ""))}`);

function renderStimulusReview(st) {
  if (!st) return "";
  if (st.kind === "passage_text" || st.kind === "gapped_text") {
    return `<div class="tv-stim">${esc(st.text)}</div>`;
  }
  if (st.kind === "image_set") {
    return `<div>${st.asset_ids.map((a) => `<img class="tv-img" src="${assetUrl(a)}" alt="" />`).join("")}</div>`;
  }
  if (st.kind === "audio_asset") {
    const preview = st.preview_asset_id
      ? `<div class="hist">Проверка звука: <audio controls preload="none" src="${assetUrl(st.preview_asset_id)}"></audio></div>`
      : "";
    return `<div class="tv-stim">🎧 прослушиваний: ${esc(st.plays)}, перемотка запрещена: ${st.locked ? "да" : "нет"}
      <div>Запись: <audio controls preload="none" src="${assetUrl(st.asset_id)}"></audio></div>${preview}</div>`;
  }
  if (st.kind === "matching_pool") {
    return `<div class="tv-stim">Варианты: ${st.options.map((o) => `<b>${esc(o.key)}</b> ${esc(o.text)}`).join(" · ")}</div>`;
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
        const label = o.kind === "image" ? `[картинка ${esc(o.asset_id)}]` : esc(o.text);
        return `<div class="tv-opt ${isKey ? "ok" : ""}">${isKey ? "✓ " : ""}<b>${esc(o.key)}</b> — ${label}</div>`;
      })
      .join("");
  } else if (it.item_type === "gap_fill") {
    const acc = [...(it.accepted || []), ...(it.accepted_variants || [])];
    key = `<span class="ok qkey">принимается: ${acc.map(esc).join(" / ")}</span>`;
  } else if (it.item_type === "matching") {
    key = `<span class="ok qkey">верно: ${esc(it.correct)}</span>`;
  } else if (it.item_type === "open_writing") {
    const mode = it.grade_mode === "manual" ? "вручную" : "ИИ";
    key = `<span class="qkey muted">письмо · минимум ${esc(it.word_min)} слов · оценивание: ${mode}</span>`;
    if (it.bullet_points && it.bullet_points.length) {
      body = `<ul>${it.bullet_points.map((b) => `<li>${esc(b)}</li>`).join("")}</ul>`;
    }
  }
  return `<div style="margin:8px 0"><div><span class="tag">${esc(it.id)}</span>${esc(it.prompt)} ${key}</div>${body}</div>`;
}

// -- roster + results -------------------------------------------------------- //
function updateRosterHeader() {
  if (!currentTest) return;
  $("#rosterTitle").textContent = `${currentTest.title} (${levelLabel(currentTest.level)})`;
  $("#copyLinkBtn").classList.toggle("hidden", currentTest.status !== "published");
  fillWindowForm();
}

// -- exam window (schedule) ---------------------------------------------------- //
// The SERVER clock is authoritative (windows + student timers). `serverOffsetMs` =
// server - this computer, measured on every refresh; the header shows the server
// time and warns when this computer's clock is off by more than a minute.
let serverOffsetMs = 0;
let serverClockTimer = null;
let windowFormKey = null; // test id + stored window the form was filled from
const SKEW_WARN_MS = 60 * 1000;
const serverNow = () => new Date(Date.now() + serverOffsetMs);
const fmtShort = (d) =>
  d.toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });

async function loadServerTime() {
  const t0 = Date.now();
  const r = await api("/server-time");
  const t1 = Date.now();
  serverOffsetMs = new Date(r.now).getTime() - (t0 + t1) / 2;
  renderServerClock();
  if (!serverClockTimer) serverClockTimer = setInterval(renderServerClock, 1000);
}

function renderServerClock() {
  const el = $("#serverClock");
  if (!el || !token) return;
  const skew = Math.abs(serverOffsetMs) >= SKEW_WARN_MS;
  el.textContent = `Время сервера: ${serverNow().toLocaleTimeString("ru-RU")}` +
    (skew ? ` — часы этого компьютера расходятся с сервером на ${Math.round(Math.abs(serverOffsetMs) / 60000)} мин` : "");
  el.classList.toggle("warn", skew);
}

// The window as one short label on the SERVER clock (bank list, roster summary).
function windowLabel(t) {
  const opens = t.opens_at ? new Date(t.opens_at) : null;
  const closes = t.closes_at ? new Date(t.closes_at) : null;
  if (!opens && !closes) return { text: "без ограничений", cls: "muted" };
  const grace = Number(t.late_start_grace_minutes || 0);
  const hardClose = closes ? new Date(closes.getTime() + grace * 60000) : null;
  const now = serverNow();
  const graceNote = grace ? ` (+${grace} мин)` : "";
  if (opens && now < opens) {
    return { text: `откроется ${fmtShort(opens)}` + (closes ? `, до ${fmtShort(closes)}${graceNote}` : ""), cls: "warn" };
  }
  if (hardClose && now > hardClose) return { text: `закрыт с ${fmtShort(hardClose)}`, cls: "muted" };
  return { text: closes ? `открыт до ${fmtShort(closes)}${graceNote}` : `открыт с ${fmtShort(opens)}`, cls: "ok" };
}

// <input type="datetime-local"> works in THIS computer's local time; the API in UTC.
function toLocalInput(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
const fromLocalInput = (v) => (v ? new Date(v).toISOString() : null);

// Fill the window form from the stored test — only when the test or its stored
// window changed, so a refresh never wipes what the teacher is typing.
function fillWindowForm({ force = false } = {}) {
  if (!currentTest) return;
  const t = currentTest;
  const label = windowLabel(t);
  const summary = $("#windowSummary");
  summary.textContent = label.text;
  summary.className = label.cls;
  const key = JSON.stringify([t.id, t.opens_at, t.closes_at, t.late_start_grace_minutes]);
  if (!force && key === windowFormKey) return;
  windowFormKey = key;
  $("#windowOpens").value = toLocalInput(t.opens_at);
  $("#windowCloses").value = toLocalInput(t.closes_at);
  $("#windowGrace").value = String(t.late_start_grace_minutes || 0);
  // Default "open now for N": the test's duration + 10 minutes to get everyone in.
  $("#windowNowMinutes").value = String((t.duration_minutes || 30) + 10);
}

async function applyWindow(updated, message) {
  currentTest = updated;
  fillWindowForm({ force: true });
  toast(message, "ok");
  await loadTests();
}

async function saveWindow() {
  if (!currentTest) return;
  const body = {
    opens_at: fromLocalInput($("#windowOpens").value),
    closes_at: fromLocalInput($("#windowCloses").value),
    late_start_grace_minutes: Number($("#windowGrace").value || 0),
  };
  const t = await api(`/tests/${encodeURIComponent(currentTest.id)}/window`, { method: "PUT", body });
  await applyWindow(t, `Окно экзамена сохранено: ${windowLabel(t).text}.`);
}

async function clearWindow() {
  if (!currentTest) return;
  const t = await api(`/tests/${encodeURIComponent(currentTest.id)}/window`, { method: "PUT", body: {} });
  await applyWindow(t, "Окно экзамена снято: тест открыт, пока опубликован.");
}

async function openWindowNow() {
  if (!currentTest) return;
  const minutes = Number($("#windowNowMinutes").value);
  if (!Number.isInteger(minutes) || minutes < 1 || minutes > 1440) {
    toast("Укажите, на сколько минут открыть тест (от 1 до 1440).", "error");
    return;
  }
  if (minutes < currentTest.duration_minutes &&
      !confirm(`Тест длится ${currentTest.duration_minutes} мин, а окно — ${minutes} мин: начавшие позже не успеют писать полное время. Открыть всё равно?`)) return;
  const t = await api(`/tests/${encodeURIComponent(currentTest.id)}/window/open-now`, { method: "POST", body: { minutes } });
  await applyWindow(t, `Тест открыт сейчас на ${minutes} мин.`);
}

async function closeWindowNow() {
  if (!currentTest) return;
  if (!confirm("Закрыть тест сейчас? Новые работы начать будет нельзя. Ученики, которые уже пишут, допишут до своего срока.")) return;
  const t = await api(`/tests/${encodeURIComponent(currentTest.id)}/window/close-now`, { method: "POST" });
  await applyWindow(t, "Тест закрыт: новые работы начать нельзя.");
}

async function openTest(test) {
  if (!currentTest || currentTest.id !== test.id) {
    // Switching tests: stop live polling for the old one, drop its in-flight
    // responses (viewGen), reset sort/search and close the old detail.
    stopLive();
    viewGen++;
    currentAttemptId = null;
    sortState = { key: null, dir: 1 };
    $("#resultsSearch").value = "";
    $("#detailSection").classList.add("hidden");
  }
  currentTest = test;
  updateRosterHeader();
  $("#rosterSection").classList.remove("hidden");
  await loadRoster();
  $("#rosterSection").scrollIntoView({ behavior: "smooth" });
}

function closeTest() {
  stopLive();
  viewGen++;
  currentTest = null;
  currentAttemptId = null;
  $("#rosterSection").classList.add("hidden");
  $("#detailSection").classList.add("hidden");
}

async function loadRoster() {
  if (!currentTest) return;
  const testId = currentTest.id;
  const gen = viewGen;
  const [roster, results, pinActivity] = await Promise.all([
    api(`/tests/${encodeURIComponent(testId)}/roster`),
    api(`/tests/${encodeURIComponent(testId)}/results`),
    // The wrong-PIN monitor is advisory: a failure here must not hide the roster.
    api(`/tests/${encodeURIComponent(testId)}/pin-activity`).catch(() => null),
  ]);
  if (gen !== viewGen || !currentTest || currentTest.id !== testId) return; // stale view
  lastRoster = roster;
  lastResults = results;
  renderPinActivity(pinActivity);
  renderRoster(roster);
  renderResults();
  const missing = results.filter(
    (o) => o.report_status === "failed" || o.report_status === "missing"
  ).length;
  const fm = $("#finalizeMissingBtn");
  if (fm && !fm.disabled) {
    fm.classList.toggle("hidden", missing === 0);
    fm.textContent = missing ? `Дооценить все (${missing})` : "Дооценить все";
  }
}

function deadlineCell(r) {
  if (!r.deadline) return '<span class="muted">—</span>';
  const dl = new Date(r.deadline);
  const left = Math.round((dl - Date.now()) / 60000);
  if (r.status === "in_progress") {
    return left > 0
      ? `до ${esc(fmtTime(r.deadline))} <span class="muted">(ещё ${left} мин)</span>`
      : `<span class="warn">истёк в ${esc(fmtTime(r.deadline))}</span>`;
  }
  return `<span class="muted">${esc(fmtDateTime(r.deadline))}</span>`;
}

// Wrong-PIN monitor banner: many wrong PINs on this test, locked students, or a
// throttled class address. Advisory — "Снять блокировки" is the remedy.
function renderPinActivity(act) {
  const box = $("#pinAlert");
  const locked = act ? act.students.filter((s) => s.locked_seconds > 0) : [];
  if (!act || (!act.alert && !locked.length && !act.throttled_ips)) {
    box.classList.add("hidden");
    box.innerHTML = "";
    return;
  }
  const parts = [];
  if (act.alert) {
    parts.push(
      `<b>Много неверных PIN для этого теста за последние ${esc(act.window_minutes)} мин: ` +
        `${esc(act.wrong_pins)}</b> (порог ${esc(act.threshold)}). Возможно, кто-то подбирает PIN.`
    );
  }
  if (locked.length) {
    parts.push(
      "<b>Заблокированы:</b> " +
        locked
          .map((s) => `${esc(s.display_name)} (ещё ${esc(Math.ceil(s.locked_seconds / 60))} мин)`)
          .join(", ")
    );
  }
  if (act.throttled_ips) {
    parts.push(
      "<b>Вход с общего адреса класса временно приостановлен</b> — неверные PIN на многих разных учеников."
    );
  }
  const counts = act.students
    .filter((s) => s.wrong_pins > 0)
    .map((s) => `${esc(s.display_name)} — ${esc(s.wrong_pins)}`)
    .join(", ");
  if (counts) parts.push(`Неверных PIN по ученикам: ${counts}.`);
  parts.push(
    "Таймер у заблокированных идёт. Нажмите «Снять блокировки» — ученики смогут сразу войти со своим PIN."
  );
  box.innerHTML = parts.map((p) => `<div>${p}</div>`).join("");
  box.classList.remove("hidden");
}

async function clearPinLocks() {
  if (!currentTest) return;
  const res = await api(`/tests/${encodeURIComponent(currentTest.id)}/pin-locks/clear`, {
    method: "POST",
  });
  toast(
    `Блокировки сняты: учеников — ${res.students_unlocked}, адресов — ${res.ips_unlocked}.`,
    "ok",
    { timeout: 6000 }
  );
  await loadRoster();
}

function renderRoster(roster) {
  const rbody = $("#rosterTable tbody");
  rbody.innerHTML = "";
  $("#rosterEmpty").classList.toggle("hidden", roster.length > 0);
  for (const r of roster) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${esc(r.display_name)}</td>
      <td><code class="pin" data-pin>${esc(r.pin || "—")}</code></td>
      <td>${esc(rosterStatusLabel(r.status))}</td>
      <td>${deadlineCell(r)}</td>
      <td><div class="actions">
        <button data-act="pin" title="Старый PIN перестанет подходить для входа; уже открытый тест ученика продолжит работать">Сгенерировать новый PIN</button>
        ${r.attempt_id ? '<button data-act="reopen" title="Ответы сохранятся; можно добавить время">Переоткрыть</button>' : ""}
        ${r.attempt_id ? '<button data-act="reset" class="danger" title="Удалить работу, ученик начнёт заново">Сбросить</button>' : ""}
        <button data-act="remove" class="danger">Удалить</button>
      </div></td>`;
    const reopenBtn = tr.querySelector('[data-act="reopen"]');
    if (reopenBtn) reopenBtn.onclick = guard("Не удалось переоткрыть работу", () => reopenAttempt(r));
    const resetBtn = tr.querySelector('[data-act="reset"]');
    if (resetBtn) resetBtn.onclick = guard("Не удалось сбросить работу", () => resetAttempt(r));
    tr.querySelector('[data-act="remove"]').onclick = guard("Не удалось удалить ученика", () => removeStudent(r));
    tr.querySelector('[data-act="pin"]').onclick = guard("Не удалось сменить PIN", () => regeneratePin(r));
    rbody.appendChild(tr);
  }
}

// -- results table: client-side sort + search over the server's rows ---------- //
const STATUS_RANK = { in_progress: 0, reopened: 1, pending: 2, missing: 3, failed: 4, complete: 5 };

function resultStatusKey(o) {
  if (o.report_status === "reopened") return "reopened";
  if (o.attempt_status === "in_progress" && o.report_status === "pending") return "in_progress";
  return o.report_status;
}

function percentOf(o) {
  const finished = o.attempt_status === "submitted" || o.attempt_status === "expired";
  if (!finished || o.report_status === "reopened" || !o.max_score) return -1;
  return o.score / o.max_score;
}

const SORTERS = {
  name: (o) => (o.display_name || o.attempt_id || "").toLocaleLowerCase("ru"),
  percent: percentOf,
  // A re-opened attempt's stored suspicion is stale (shown as "—"): sort it as none.
  suspicion: (o) =>
    o.suspicion_score == null || o.report_status === "reopened" ? -1 : o.suspicion_score,
  status: (o) => STATUS_RANK[resultStatusKey(o)] ?? 9,
  submitted: (o) => (o.submitted_at ? Date.parse(o.submitted_at) : Number.MAX_SAFE_INTEGER),
};

function visibleResults() {
  const q = $("#resultsSearch").value.trim().toLocaleLowerCase("ru");
  let rows = lastResults.filter(
    (o) => !q || (o.display_name || "").toLocaleLowerCase("ru").includes(q)
  );
  if (sortState.key) {
    const key = SORTERS[sortState.key];
    rows = rows
      .map((o, i) => ({ o, i }))
      .sort((a, b) => {
        const ka = key(a.o);
        const kb = key(b.o);
        if (ka < kb) return -sortState.dir;
        if (ka > kb) return sortState.dir;
        return a.i - b.i; // stable: ties keep the server order
      })
      .map((x) => x.o);
  }
  return rows;
}

function onSortClick(key) {
  // asc -> desc -> back to the server's default (suspicious-first) order.
  if (sortState.key !== key) sortState = { key, dir: 1 };
  else if (sortState.dir === 1) sortState = { key, dir: -1 };
  else sortState = { key: null, dir: 1 };
  renderResults();
}

function renderResults() {
  document.querySelectorAll("#resultsTable th.sortable").forEach((th) => {
    const active = th.dataset.sort === sortState.key;
    th.querySelector(".arrow").textContent = active ? (sortState.dir === 1 ? "▲" : "▼") : "";
    th.setAttribute("aria-sort", active ? (sortState.dir === 1 ? "ascending" : "descending") : "none");
  });
  const body = $("#resultsTable tbody");
  body.innerHTML = "";
  const rows = visibleResults();
  const empty = $("#resultsEmpty");
  empty.classList.toggle("hidden", rows.length > 0);
  empty.textContent = lastResults.length ? "Никого не найдено." : "Пока никто не начал тест.";
  for (const o of rows) {
    const finished = o.attempt_status === "submitted" || o.attempt_status === "expired";
    const reopened = o.report_status === "reopened";
    // Offer an in-row re-grade when the finalize failed or never landed.
    const canRegrade = o.report_status === "failed" || (o.report_status === "missing" && finished);
    let score;
    let pct;
    if (reopened) {
      score = '<span class="muted" title="Старая оценка устарела — дождитесь повторной сдачи">—</span>';
      pct = "";
    } else if (!finished) {
      score = `<span class="muted" title="Предварительно — ученик ещё пишет">${esc(o.score)} / ${esc(o.max_score)}</span>`;
      pct = "";
    } else {
      score = `${esc(o.score)} / ${esc(o.max_score)}`;
      pct = o.max_score ? `${Math.round((100 * o.score) / o.max_score)}%` : "";
    }
    const tr = document.createElement("tr");
    tr.dataset.attempt = o.attempt_id;
    tr.innerHTML = `<td class="name">${esc(o.display_name || o.attempt_id)}</td>
      <td>${score}</td>
      <td>${pct}</td>
      <td>${gradingStatus(o.report_status, o.attempt_status, o.report_error)}
          ${canRegrade ? '<button data-act="regrade" class="small">Переоценить</button>' : ""}</td>
      <td>${o.needs_review && !reopened ? '<span title="Нужна ручная проверка (письмо)">⚠︎</span>' : ""}</td>
      <td class="susp">${suspicionCell(o)}</td>
      <td class="susp">${o.confidence == null || reopened ? '<span class="muted">—</span>' : esc(fix2(o.confidence))}</td>
      <td>${esc(o.event_count)}</td>
      <td class="hist">${o.submitted_at ? esc(fmtDateTime(o.submitted_at)) : "—"}</td>
      <td><div class="actions">
        <button data-act="detail">Подробнее</button>
        ${finished && !reopened ? '<button data-act="print" title="Отчёт для ученика/родителей (без данных о списывании)">Печать отчёта</button>' : ""}
      </div></td>`;
    tr.querySelector('[data-act="detail"]').onclick = guard("Не удалось открыть работу", () => loadDetail(o.attempt_id));
    const pr = tr.querySelector('[data-act="print"]');
    if (pr) pr.onclick = () => openPrintReport(o.attempt_id);
    const rg = tr.querySelector('[data-act="regrade"]');
    if (rg) rg.onclick = guard("Не удалось переоценить", () => regradeRow(rg, o.attempt_id));
    body.appendChild(tr);
  }
}

// The stored advisory suspicion (computed once at finalize by the deterministic
// RULE ENGINE — never by a model) as a coloured chip whose tooltip names the top two
// rules behind it, plus
// a "✍ AI?" marker when any writing task's AI-assist likelihood is >= 0.6. No stored
// verdict yet (still writing / grading / an old report) -> "—". A re-opened attempt
// also shows "—": its stored verdict judges the PREVIOUS submit (the detail still
// shows it, labelled "от прошлой сдачи") and is redone on the re-submit. Advisory only.
function suspicionCell(o) {
  if (o.report_status === "reopened") {
    const old = o.suspicion_score == null ? "" : ` (было ${fix2(o.suspicion_score)})`;
    return `<span class="muted susp-stale" title="${esc(`Подозрение от прошлой сдачи${old} — обновится после повторной сдачи`)}">—</span>`;
  }
  if (o.suspicion_score == null) {
    return '<span class="muted" title="Анализ честности появится после сдачи и оценивания">—</span>';
  }
  const x = o.suspicion_score;
  // Rule titles and the model id are server data: into the title only via esc().
  const top = (o.top_rules || []).slice(0, 2);
  const src = o.verdict_legacy
    ? "анализ в старом формате (до правил) — нажмите «Переоценить»"
    : "по правилам, без ИИ";
  const why = top.length ? ` Главное: ${top.join("; ")}.` : o.verdict_legacy ? "" : " Правила не сработали.";
  const title = `Подозрение ${levelWord(x)} (${src}).${why} Рекомендательно — на балл не влияет.`;
  const chip = `<span class="chip ${levelClass(x)} susp-chip" title="${esc(title)}">${esc(fix2(x))}</span>`;
  const aiSrc = o.verdict_model_id ? `второе мнение Gemini ${o.verdict_model_id}` : "по правилам";
  const ai =
    o.ai_assist_max != null && o.ai_assist_max >= AI_ASSIST_MARK
      ? ` <span class="chip bad ai-mark" title="${esc(`Письмо: вероятность помощи ИИ/посторонней помощи ${fix2(o.ai_assist_max)} (${aiSrc}) — проверьте хронологию`)}">✍ AI?</span>`
      : "";
  return chip + ai;
}

// The finalize state chip for the per-attempt grade + skill report (computed once
// on submit). "pending" = the background grade is still expected (⏳); "complete" =
// ✓; "failed" = the finalize errored — the cause is in the tooltip; "missing" = a
// finished attempt whose finalize was lost (restart) and needs a re-grade;
// "reopened" = the teacher re-opened a graded attempt, its old report is stale.
// A student still working shows "пишет" — there is nothing to grade yet.
function gradingStatus(status, attemptStatus, error) {
  const finished = !attemptStatus || attemptStatus === "submitted" || attemptStatus === "expired";
  if (status === "reopened") {
    return '<span class="chip warn" title="Работа переоткрыта учителем; оценка обновится после повторной сдачи">переоткрыт — ждёт сдачи</span>';
  }
  if (status === "complete") return '<span class="chip ok">✓ готово</span>';
  if (status === "failed") {
    return `<span class="chip bad" title="${esc(error || "ошибка оценивания")}">⚠ ошибка</span>`;
  }
  if (!finished) return '<span class="chip muted">пишет…</span>';
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
    await api(`/results/${encodeURIComponent(attemptId)}/regrade`, { method: "POST" });
    toast("Работа переоценена.", "ok");
  } finally {
    await loadRoster();
  }
}

// Grade every finished attempt of this test whose report is missing or failed.
async function finalizeMissing() {
  const btn = $("#finalizeMissingBtn");
  if (!currentTest || !btn) return;
  btn.disabled = true;
  const label = btn.textContent;
  btn.textContent = "Оцениваю…";
  try {
    const r = await api(`/tests/${encodeURIComponent(currentTest.id)}/finalize-missing`, { method: "POST" });
    toast(`Оценено работ: ${r.attempt_ids.length}.`, "ok");
  } finally {
    btn.disabled = false;
    btn.textContent = label;
    await loadRoster();
  }
}

// -- CSV export ------------------------------------------------------------------ //
async function downloadCsv() {
  if (!currentTest) return;
  const resp = await api(`/tests/${encodeURIComponent(currentTest.id)}/results.csv`, { raw: true });
  const blob = await resp.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  const safeTitle = (currentTest.title || currentTest.id).replace(/[\\/:*?"<>|]+/g, "_");
  a.download = `Результаты — ${safeTitle}.csv`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}

// -- printable student report ---------------------------------------------------- //
function openPrintReport(attemptId) {
  const w = window.open(`teacher-report.html?attempt=${encodeURIComponent(attemptId)}`, "_blank");
  if (!w) toast("Браузер заблокировал новое окно — разрешите всплывающие окна для этого сайта.", "error");
}

// -- attempt detail ---------------------------------------------------------------- //
async function loadDetail(attemptId) {
  currentAttemptId = attemptId;
  const gen = viewGen;
  const d = await api(`/results/${encodeURIComponent(attemptId)}`);
  if (gen !== viewGen || currentAttemptId !== attemptId) return;
  activeRuleIndex = null; // another attempt: no rule is highlighted yet
  renderDetail(d);
  $("#detailSection").scrollIntoView({ behavior: "smooth" });
}

// Refresh the open detail in place (Refresh button / live mode): keeps the scroll
// position and which <details> are open; skipped while the teacher is using a
// control inside it (e.g. a half-chosen mark), so nothing is yanked away.
async function refreshDetail() {
  if (!currentAttemptId) return;
  const active = document.activeElement;
  if (active && $("#detailBody").contains(active) && /^(SELECT|INPUT|TEXTAREA)$/.test(active.tagName)) {
    return;
  }
  const attemptId = currentAttemptId;
  const gen = viewGen;
  const d = await api(`/results/${encodeURIComponent(attemptId)}`);
  if (gen !== viewGen || currentAttemptId !== attemptId) return;
  renderDetail(d, { preserve: true });
}

function renderDetail(d, { preserve = false } = {}) {
  const body = $("#detailBody");
  const scrollY = window.scrollY;
  const open = new Set(
    preserve ? [...body.querySelectorAll("details[data-key]")].filter((x) => x.open).map((x) => x.dataset.key) : []
  );
  $("#detailSection").classList.remove("hidden");
  const reopened = d.report_status === "reopened";
  body.innerHTML = `
    <p><strong>${esc(d.display_name || d.attempt_id)}</strong>
       ${gradingStatus(d.report_status, undefined, d.report_error)}
       <button id="regradeBtn" data-attempt="${esc(d.attempt_id)}">Переоценить</button>
       ${reopened ? "" : `<button id="printBtn" data-attempt="${esc(d.attempt_id)}" title="Отчёт для ученика/родителей">Печать отчёта</button>`}</p>
    ${reopened ? '<p class="notice">Работа переоткрыта: ученик может продолжить. Оценка ниже — от прошлой сдачи и обновится после повторной.</p>' : ""}
    <p>Балл: <strong>${esc(d.grading.score)} / ${esc(d.grading.max_score)}</strong>
       ${d.grading.needs_review ? '<span class="pill draft">нужна проверка</span>' : ""}</p>
    ${renderSkills(d.skills || [], d.summary)}
    ${renderWriting(d.responses || [])}
    ${renderIntegrity(d)}
    ${renderTimeline(d.timeline || [])}
    <details data-key="raw" style="margin-top:14px">
      <summary class="muted">${esc(d.event_count)} событий в исходном виде — для скриптов и анализа</summary>
      <pre>${esc(JSON.stringify(d.events, null, 2))}</pre>
    </details>
    ${renderDevices(d.devices || [], d.profile)}
    ${renderBreakdown(d.responses || [])}`;
  if (preserve) {
    body.querySelectorAll("details[data-key]").forEach((x) => {
      x.open = open.has(x.dataset.key); // keep exactly what the teacher had open/closed
    });
    window.scrollTo(0, scrollY);
  }
  wireGrading();
  wireEvidence();
  const rb = $("#regradeBtn");
  if (rb) rb.onclick = guard("Не удалось переоценить", () => regrade(rb.dataset.attempt));
  const pb = $("#printBtn");
  if (pb) pb.onclick = () => openPrintReport(pb.dataset.attempt);
}

// -- integrity verdict (advisory; golden rules #2 / #6) ------------------------- //
// The verdict + integrity profile STORED at finalize. The deterministic rule engine
// is the verdict of record: its hits come first (strongest first), each with the
// numbers behind it and a jump to the raw moments in the timeline. An LLM, when
// enabled, only adds a clearly separated "second opinion" on the writing answers.
// Rendered right above the human timeline and the raw events, so the teacher always
// checks the evidence next to the advice. It never changes a grade.
function meter(x, cls) {
  const pct = Math.round(Math.max(0, Math.min(1, x)) * 100);
  return `<span class="meter" aria-hidden="true"><span class="${cls}" style="width:${pct}%"></span></span>`;
}

function flagChips(list) {
  return list
    .map((f) => `<span class="chip warn flag" title="${esc(f)}">${esc(flagLabel(f))}</span>`)
    .join(" ");
}

const INTEGRITY_TITLE = '<h2 style="margin-top:16px">Анализ честности (рекомендательно)</h2>';
const INTEGRITY_DISCLAIMER =
  '<p class="notice disclaimer">Это лишь рекомендация: она никогда не меняет оценку. Прежде чем делать выводы, проверьте хронологию действий ниже.</p>';

function renderIntegrity(d) {
  const v = d.verdict;
  if (!v) {
    // No stored verdict and the live computation failed: "—" + the server's note.
    return `<section class="integrity" data-key-section="integrity">
      ${INTEGRITY_TITLE}
      ${INTEGRITY_DISCLAIMER}
      <div class="verdict-grid"><div>Подозрение: <strong class="muted verdict-none">—</strong></div></div>
      <p class="notice verdict-unavailable">${esc(d.verdict_note || "Анализ честности для этой работы недоступен.")}</p>
    </section>`;
  }
  const legacy = v.rules_version == null;
  const rules = v.rules || [];
  const live =
    d.verdict_source === "live_fallback"
      ? ' <span class="pill draft" title="Итоговый анализ сохраняется при сдаче работы">предварительно — рассчитано сейчас по правилам</span>'
      : d.report_status === "reopened"
      ? ' <span class="pill draft" title="Работа переоткрыта: анализ пересчитается после повторной сдачи">от прошлой сдачи — обновится после повторной</span>'
      : "";
  const source = legacy
    ? `старый формат${v.model_id ? ` (Gemini ${esc(v.model_id)})` : " (формула)"}`
    : "детерминированные правила (без ИИ)";
  const susp = v.suspicion_score;
  const conf = v.confidence;
  const confWord = conf >= 0.6 ? "высокая" : conf >= 0.3 ? "средняя" : "низкая";
  const legacyNote = legacy
    ? '<p class="notice verdict-legacy">Этот анализ выполнен до введения правил: подозрение посчитано по старой схеме. Нажмите «Переоценить», чтобы получить разбор по правилам.</p>'
    : "";
  const hits = rules.length
    ? rules.map((r, i) => renderRuleCard(r, i, d.responses || [])).join("")
    : legacy
    ? v.flags && v.flags.length
      ? `<p>Признаки: ${flagChips(v.flags)}</p>`
      : '<p class="hist">Признаков нарушений не найдено.</p>'
    : '<p class="hist rules-none">Ни одно правило не сработало.</p>';
  return `<section class="integrity" data-key-section="integrity">
    ${INTEGRITY_TITLE}
    ${INTEGRITY_DISCLAIMER}
    <div class="verdict-grid">
      <div>Подозрение: ${meter(susp, levelClass(susp))}
        <strong class="${levelClass(susp)}">${levelWord(susp)}</strong> <span class="hist">(${esc(fix2(susp))})</span></div>
      <div title="Насколько полной была запись действий ученика — а не уверенность в нарушении">Полнота данных: ${meter(conf, "neutral")}
        <strong>${confWord}</strong> <span class="hist">(${esc(fix2(conf))})</span></div>
      <div class="hist">Источник: ${source}${live}</div>
    </div>
    ${v.summary ? `<p class="verdict-summary">${esc(v.summary)}</p>` : ""}
    ${legacyNote}
    <h3 style="margin-top:12px">Сработавшие правила</h3>
    <div class="rule-list">${hits}</div>
    ${renderWritingFeatures((d.profile && d.profile.writing) || [], v, d.responses || [])}
    ${renderSecondOpinion(v, d.responses || [])}
  </section>`;
}

// One rule hit: severity chip, the server's Russian title + explanation (its real
// numbers), the task it concerns, and a button that highlights the raw moments in
// the timeline. `evidence` travels in data-* attributes as JSON via esc().
function renderRuleCard(r, index, responses) {
  const sev = severityKey(r.severity);
  const ev = r.evidence || {};
  const items = ev.item_ids || [];
  const events = ev.events || [];
  const isWriting = items.length === 1 && responses.some((x) => x.item_id === items[0] && x.item_type === "open_writing");
  const where = isWriting ? `<span class="hist rule-item">${writingTitle(items[0], responses)}</span>` : "";
  const refs = events.map((e) => [e.type, Date.parse(e.at), e.item_id == null ? null : e.item_id]);
  const canShow = refs.length > 0 || items.length > 0;
  const count = refs.length ? ` (${refs.length})` : "";
  return `<div class="rule-card sev-${sev}" data-rule="${esc(r.code)}" data-rule-index="${esc(index)}">
    <span class="chip sev-${sev} rule-severity">${esc(SEVERITY_LABELS[sev])}</span>
    <h3>${esc(r.title_ru || flagLabel(r.code))}</h3> ${where}
    <p class="rule-explanation">${esc(r.explanation_ru || "")}</p>
    ${
      canShow
        ? `<button type="button" class="small rule-evidence" data-refs="${esc(JSON.stringify(refs))}" data-items="${esc(JSON.stringify(items))}">Показать в хронологии${count}</button>`
        : ""
    }
  </div>`;
}

// Highlight the timeline rows behind one rule card and scroll to the first one.
// Rows are matched by (event type, trusted time, item) — the same `at` the server
// put in the rule's evidence; with no event pointers, by the rule's item ids.
let activeRuleIndex = null;

function showEvidence(btn, { scroll = true } = {}) {
  const body = $("#detailBody");
  body.querySelectorAll("tr.tl-evidence").forEach((tr) => tr.classList.remove("tl-evidence"));
  body.querySelectorAll(".rule-card.active").forEach((c) => c.classList.remove("active"));
  let refs = [];
  let items = [];
  try {
    refs = JSON.parse(btn.dataset.refs || "[]");
    items = JSON.parse(btn.dataset.items || "[]");
  } catch {
    return;
  }
  const keys = new Set(refs.map((r) => `${r[0]}|${r[1]}|${r[2] == null ? "" : r[2]}`));
  const rows = [...body.querySelectorAll("table.timeline tr[data-at]")];
  let hits = rows.filter((tr) => keys.has(`${tr.dataset.type}|${tr.dataset.at}|${tr.dataset.item || ""}`));
  if (!hits.length && items.length) hits = rows.filter((tr) => items.includes(tr.dataset.item || ""));
  const card = btn.closest(".rule-card");
  if (card) {
    card.classList.add("active");
    activeRuleIndex = card.dataset.ruleIndex;
  }
  if (!hits.length) {
    if (scroll) toast("В хронологии нет записей для этого правила — смотрите исходные события.", "info");
    return;
  }
  hits.forEach((tr) => tr.classList.add("tl-evidence"));
  if (scroll) {
    const details = body.querySelector('details[data-key="timeline"]');
    if (details) details.open = true;
    hits[0].scrollIntoView({ behavior: "smooth", block: "center" });
  }
}

function wireEvidence() {
  const body = $("#detailBody");
  body.querySelectorAll("button.rule-evidence").forEach((btn) => {
    btn.onclick = () => showEvidence(btn);
  });
  // A live refresh re-renders the detail: keep the teacher's highlighted rule.
  if (activeRuleIndex != null) {
    const again = body.querySelector(`.rule-card[data-rule-index="${CSS.escape(String(activeRuleIndex))}"] button.rule-evidence`);
    if (again) showEvidence(again, { scroll: false });
    else activeRuleIndex = null;
  }
}

function writingTitle(itemId, responses) {
  const ws = responses.filter((r) => r.item_type === "open_writing");
  const i = ws.findIndex((r) => r.item_id === itemId);
  if (i < 0) return esc(itemId);
  const r = ws[i];
  const no = r.item_no ? ` (задание ${esc(r.item_no)})` : "";
  return `Письмо ${i + 1}${no}${r.section_title ? ` — ${esc(r.section_title)}` : ""}`;
}

function writingFeatureRows(f) {
  if (!f) return '<li class="hist">Нет данных телеметрии по этому заданию.</li>';
  const rows = [];
  const without = (f.non_keyboard_chars || 0) + (f.unexplained_chars || 0);
  const ratio = f.typed_ratio != null ? ` (${Math.round(f.typed_ratio * 100)}% набрано)` : "";
  rows.push(
    `<li>Набрано с клавиатуры: <b>${esc(f.typed_chars)}</b> симв. · появилось без набора: <b class="${without ? "warn" : ""}">${esc(without)}</b> симв.${ratio}` +
      (f.unexplained_chars ? ` <span class="hist">(из них ${esc(f.unexplained_chars)} — без какого-либо события ввода)</span>` : "") +
      ` <span class="hist">· итоговый текст ${esc(f.final_length)} симв.</span></li>`
  );
  rows.push(
    `<li>Попытки вставки: <b class="${f.paste_attempts ? "warn" : ""}">${esc(f.paste_attempts)}</b>` +
      (f.paste_attempts ? ` · максимальная длина <b>${esc(f.max_paste_attempt_length)}</b> симв.` : "") +
      "</li>"
  );
  const speed =
    f.chars_per_active_minute != null ? `<b>≈${Math.round(f.chars_per_active_minute)}</b> симв./мин` : "нет данных";
  const ik = f.inter_key_p50_ms != null ? ` <span class="hist">(медиана между нажатиями ${Math.round(f.inter_key_p50_ms)} мс)</span>` : "";
  rows.push(`<li>Скорость печати: ${speed}${ik}</li>`);
  const away = Math.max(f.hidden_ms_while_writing || 0, f.blur_ms_while_writing || 0);
  rows.push(
    `<li>Время вне вкладки во время письма: <b class="${away >= 30000 ? "warn" : ""}">${esc(fmtSecs(away))}</b>` +
      (f.away_count ? ` <span class="hist">(уходов: ${esc(f.away_count)})</span>` : "") +
      "</li>"
  );
  rows.push(
    `<li>Выходы из полноэкранного режима: <b class="${f.fullscreen_exits_while_writing ? "warn" : ""}">${esc(f.fullscreen_exits_while_writing)}</b></li>`
  );
  if (f.untrusted_inputs) rows.push(`<li class="bad">Ввод, сгенерированный скриптом: ${esc(f.untrusted_inputs)}</li>`);
  if (!f.has_keystroke_telemetry) rows.push('<li class="hist">Телеметрии набора нет (старая версия страницы или данные не дошли).</li>');
  return rows.join("");
}

// A per-item assessment line (likelihood meter + word + number).
function likelihoodLine(a) {
  return `Вероятность помощи ИИ / посторонней помощи: ${meter(a.ai_assist_likelihood, levelClass(a.ai_assist_likelihood))}
    <strong class="${levelClass(a.ai_assist_likelihood)}">${levelWord(a.ai_assist_likelihood)}</strong>
    <span class="hist">(${esc(fix2(a.ai_assist_likelihood))})</span>`;
}

// Deterministic per-writing-task facts (the numbers the writing rules read). Without
// an LLM the verdict's `writing[]` is the rule engine's own assessment and is shown
// here; with one it is the second opinion and is shown in its own block below.
function renderWritingFeatures(features, v, responses) {
  const byRules = !v.model_id; // writing[] produced by the rules (or a legacy verdict's)
  const assessments = byRules ? v.writing || [] : [];
  const ids = features.map((f) => f.item_id);
  for (const a of assessments) if (!ids.includes(a.item_id)) ids.push(a.item_id);
  if (!ids.length) return "";
  const cards = ids
    .map((id) => {
      const f = features.find((x) => x.item_id === id);
      const a = assessments.find((x) => x.item_id === id);
      const signals = a && a.signals.length ? `<div>Сигналы: ${flagChips(a.signals)}</div>` : "";
      return `<div class="tv-sec integrity-writing" data-item="${esc(id)}">
        <h3>${writingTitle(id, responses)}</h3>
        ${a ? `<div>${likelihoodLine(a)} <span class="hist">— по правилам</span></div>` : ""}
        ${signals}
        ${a && a.rationale ? `<div class="hist">${esc(a.rationale)}</div>` : ""}
        <ul class="features">${writingFeatureRows(f)}</ul>
      </div>`;
    })
    .join("");
  return `<h3 style="margin-top:12px">Письменные задания — данные набора текста</h3>${cards}`;
}

// The optional LLM second opinion on the writing answers — only when one exists (or
// was attempted and failed). Clearly advisory: it never moved the suspicion above.
function renderSecondOpinion(v, responses) {
  if (v.rules_version == null) return ""; // a legacy verdict has no separate opinion
  const error = v.error
    ? `<p class="notice verdict-error">Второе мнение ИИ получить не удалось: ${esc(v.error)}. Вердикт по правилам от этого не зависит; нажмите «Переоценить», чтобы повторить запрос.</p>`
    : "";
  if (!v.model_id) return error ? `<div class="second-opinion">${error}</div>` : "";
  const cards = (v.writing || [])
    .map((a) => {
      const signals = a.signals.length ? `<div>Сигналы: ${flagChips(a.signals)}</div>` : "";
      return `<div class="tv-sec opinion-writing" data-item="${esc(a.item_id)}">
        <h3>${writingTitle(a.item_id, responses)}</h3>
        <div>${likelihoodLine(a)}</div>
        ${signals}
        ${a.rationale ? `<div class="hist">${esc(a.rationale)}</div>` : ""}
      </div>`;
    })
    .join("");
  return `<div class="second-opinion">
    <h3>Второе мнение (Gemini) по письму</h3>
    <p class="hist opinion-note">Рекомендательно. Модель ${esc(v.model_id)} оценивала только письменные ответы; на подозрение выше и на сортировку учеников это мнение не влияет.</p>
    ${cards || '<p class="hist">Письменных ответов для оценки нет.</p>'}
    ${error}
  </div>`;
}

// Per-skill pedagogical feedback (strengths/weaknesses), produced once by Gemini at
// submit and read from the persisted report. The teacher's academic-performance view,
// separate from the advisory cheating verdict (golden rules #2/#6).
function renderSkills(skills, summary) {
  if (!skills.length && !summary) return "";
  const cards = skills
    .map(
      (s) => `<div class="tv-sec">
        <h3>${esc(skillLabel(s.skill))} — <strong>${esc(s.score)} / ${esc(s.max_score)}</strong></h3>
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
    const d = await api(`/results/${encodeURIComponent(attemptId)}/regrade`, { method: "POST" });
    if (currentAttemptId === attemptId) renderDetail(d, { preserve: true });
    toast("Работа переоценена.", "ok");
    await loadRoster();
  } finally {
    const b = $("#regradeBtn");
    if (b) { b.disabled = false; b.textContent = "Переоценить"; }
  }
}

// Each open_writing as its own read-and-grade card (Письмо 1, Письмо 2, …).
function renderWriting(responses) {
  const ws = responses.filter((r) => r.item_type === "open_writing");
  if (!ws.length) return "";
  const cards = ws
    .map((r, i) => {
      // Choose-one writing (B1 Part 2, both tasks written): the uncounted task is
      // stored 0 / 0, but a mark on it is out of the full maximum and makes it the
      // counted task instead of the other one (the server swaps the pair).
      const uncounted = r.max_points === 0;
      // Its counted sibling sits in the same section (choose-one tasks share points);
      // matched by section id — two parts may share a title.
      const sibling = ws.find((w) => w !== r && w.max_points > 0 && w.section_id === r.section_id);
      const full = uncounted ? (sibling ? sibling.max_points : 5) : r.max_points;
      const marked = r.manual && !uncounted;
      const opts = ['<option value="" disabled' + (marked ? "" : " selected") + ">—</option>"];
      for (let p = 0; p <= full; p++) {
        opts.push(`<option value="${p}" ${marked && r.awarded === p ? "selected" : ""}>${p}</option>`);
      }
      const status = uncounted
        ? '<span class="hist">не засчитано (выбрано другое задание) — поставить оценку — засчитать это задание вместо другого</span>'
        : marked
          ? `<span class="ok">✓ оценено: ${esc(r.awarded)} / ${esc(r.max_points)}</span>`
          : '<span class="bad">ещё не оценено учителем</span>';
      return `<div class="tv-sec">
        <h3>Письмо ${i + 1} — ${esc(r.section_title || "")}</h3>
        <div><b>Задание:</b> ${esc(r.prompt)}</div>
        ${r.bullet_points.length ? `<ul>${r.bullet_points.map((b) => `<li>${esc(b)}</li>`).join("")}</ul>` : ""}
        ${r.word_min ? `<div class="hist">минимум ${esc(r.word_min)} слов</div>` : ""}
        <div class="writing">${r.writing_text ? esc(r.writing_text) : "— нет ответа —"}</div>
        <div class="row" style="margin-top:8px">
          <label>Оценка:
            <select data-grade-select data-item="${esc(r.item_id)}">${opts.join("")}</select> / ${esc(full)}
          </label>
          ${status}
        </div>
      </div>`;
    })
    .join("");
  return `<h2 style="margin-top:16px">Письмо — прочитать и оценить</h2>${cards}`;
}

async function gradeItem(itemId, awarded) {
  const attemptId = currentAttemptId;
  const d = await api(`/results/${encodeURIComponent(attemptId)}/grades/${encodeURIComponent(itemId)}`, {
    method: "PUT",
    body: { awarded },
  });
  // Re-render in place: the teacher stays where they were on the page.
  if (currentAttemptId === attemptId) renderDetail(d, { preserve: true });
  toast("Оценка сохранена.", "ok", { timeout: 2000 });
  // The results table shows the merged score — keep it in step with the mark.
  await loadRoster();
}

function wireGrading() {
  document.querySelectorAll("[data-grade]").forEach((b) => {
    b.onclick = guard("Не удалось сохранить оценку", () => gradeItem(b.dataset.item, Number(b.dataset.awarded)));
  });
  document.querySelectorAll("[data-grade-select]").forEach((sel) => {
    sel.onchange = guard("Не удалось сохранить оценку", async () => {
      if (sel.value === "") return;
      sel.blur(); // let live refresh resume once the mark is saved
      await gradeItem(sel.dataset.item, Number(sel.value));
    });
  });
}

function renderTimeline(timeline) {
  if (!timeline.length) return "";
  const rows = timeline
    .map((e) => {
      // data-* let a rule card find this row (type + trusted time + item).
      return `<tr class="${e.integrity ? "tl-integrity" : ""}" data-type="${esc(e.type)}" data-at="${esc(Date.parse(e.at))}" data-item="${esc(e.item_id == null ? "" : e.item_id)}">
        <td class="hist" style="white-space:nowrap">${esc(fmtTime(e.at))}</td>
        <td>${esc(e.label)}</td>
      </tr>`;
    })
    .join("");
  return `<details data-key="timeline" open style="margin-top:16px">
    <summary><h2 style="display:inline">Хронология действий</h2></summary>
    <table class="timeline"><tbody>${rows}</tbody></table></details>`;
}

function renderDevices(devices, profile) {
  const changed =
    profile && profile.device_changed
      ? `<p class="bad">⚠︎ Устройство сменилось во время теста — замечено разных устройств: ${esc(profile.device_count)}.</p>`
      : "";
  if (!devices.length) return changed || '<p class="muted">Данных об устройстве нет.</p>';
  const rows = devices
    .map(
      (d) => `<tr>
        <td>${esc(fmtDateTime(d.at))}</td>
        <td class="hist">${esc(d.server_ip || "—")}</td>
        <td class="hist">${esc(d.platform || "—")} · ${esc(d.screen || "—")} · ${esc(d.timezone || "—")} · ${esc(d.language || "—")}</td>
        <td class="hist">${esc(d.user_agent || "—")}</td>
      </tr>`
    )
    .join("");
  return `<h2 style="margin-top:16px">Устройство и сеанс</h2>${changed}
    <div class="table-scroll"><table><thead><tr><th>Время</th><th>IP</th><th>Платформа · экран · часовой пояс · язык</th><th>Браузер</th></tr></thead>
    <tbody>${rows}</tbody></table></div>`;
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
            : '<span class="muted">— нет ответа —</span>')
        : r.student_answer
        ? esc(r.student_answer)
        : '<span class="muted">— нет ответа —</span>';
    const correct = r.correct_answer ? esc(r.correct_answer) : '<span class="muted">вручную / ИИ</span>';
    const hist =
      r.history && r.history.length > 1
        ? `<div class="hist">варианты: ${r.history.map((h) => esc(h.value)).join(" → ")}</div>`
        : "";
    // Typed-text answers (gap_fill) get a teacher ✓/✗ override — auto fuzzy match
    // can miss an acceptable spelling. Writing is graded in its own section.
    const override =
      r.item_type === "gap_fill"
        ? `<div class="hist">исправить:
             <button data-grade data-item="${esc(r.item_id)}" data-awarded="${esc(r.max_points)}" title="Засчитать">✓</button>
             <button data-grade data-item="${esc(r.item_id)}" data-awarded="0" title="Не засчитывать">✗</button>
             ${r.manual ? '<span class="ok">вручную</span>' : ""}</div>`
        : "";
    return `<tr>
      <td>${mark}</td>
      <td><span class="tag">${esc(r.item_id)}</span>${esc(r.prompt)}</td>
      <td>${student}${hist}</td>
      <td class="ok">${correct}</td>
      <td>${esc(r.awarded)}/${esc(r.max_points)}${r.needs_review ? ' <span class="pill draft">проверка</span>' : ""}${override}</td>
    </tr>`;
  });
  return `<details data-key="breakdown" open style="margin-top:16px">
    <summary><h2 style="display:inline">Ответы по вопросам</h2></summary>
    <div class="table-scroll"><table class="breakdown"><thead>
      <tr><th></th><th>Вопрос</th><th>Ответ ученика</th><th>Правильно</th><th>Баллы</th></tr>
    </thead><tbody>${rows.join("")}</tbody></table></div></details>`;
}

// -- roster actions -------------------------------------------------------------- //
async function addStudent() {
  const name = $("#studentName").value.trim();
  if (!currentTest) return;
  if (!name) {
    toast("Введите имя ученика.", "error", { timeout: 3000 });
    return;
  }
  await api(`/tests/${encodeURIComponent(currentTest.id)}/roster`, {
    method: "POST",
    body: { display_name: name },
  });
  $("#studentName").value = "";
  toast(`Добавлен(а): ${name}.`, "ok");
  await loadRoster();
}

async function addStudentsBulk() {
  if (!currentTest) return;
  const names = $("#bulkNames").value.split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
  if (!names.length) {
    toast("Впишите имена — по одному в строке.", "error", { timeout: 3000 });
    return;
  }
  const btn = $("#bulkAddBtn");
  btn.disabled = true;
  try {
    const r = await api(`/tests/${encodeURIComponent(currentTest.id)}/roster/bulk`, {
      method: "POST",
      body: { names },
    });
    let msg = `Добавлено учеников: ${r.added.length}.`;
    if (r.skipped.length) msg += `\nПропущены (уже в списке или повтор): ${r.skipped.join(", ")}.`;
    toast(msg, r.added.length ? "ok" : "info", { timeout: r.skipped.length ? 8000 : 4000 });
    $("#bulkNames").value = "";
    $("#bulkBox").classList.add("hidden");
    await loadRoster();
  } finally {
    btn.disabled = false;
  }
}

async function reopenAttempt(r) {
  const past = r.deadline && new Date(r.deadline) <= new Date();
  const answer = prompt(
    `Переоткрыть работу «${r.display_name}»? Ответы сохранятся, ученик сможет войти снова.\n\n` +
      (past
        ? "Срок уже истёк — без продления работа сразу закроется снова.\n"
        : r.deadline ? `Текущий срок: ${fmtTime(r.deadline)}.\n` : "") +
      "Сколько минут дать ученику? (0 — без продления, срок не меняется)",
    past ? "10" : "0"
  );
  if (answer === null) return; // cancelled
  const minutes = Number(answer.trim() || "0");
  if (!Number.isInteger(minutes) || minutes < 0 || minutes > 1440) {
    toast("Укажите целое число минут от 0 до 1440.", "error");
    return;
  }
  if (minutes === 0 && past && !confirm("Срок истёк, а продление не задано — работа сразу закроется снова. Всё равно переоткрыть?")) {
    return;
  }
  await api(`/roster/${encodeURIComponent(r.roster_entry_id)}/reopen`, {
    method: "POST",
    body: minutes > 0 ? { extend_minutes: minutes } : undefined,
  });
  toast(
    minutes > 0
      ? `Работа «${r.display_name}» переоткрыта, добавлено ${minutes} мин.`
      : `Работа «${r.display_name}» переоткрыта.`,
    "ok"
  );
  await loadRoster();
  if (currentAttemptId && currentAttemptId === r.attempt_id) await refreshDetail();
}

async function resetAttempt(r) {
  if (!confirm(`Сбросить работу «${r.display_name}»? Ответы и запись действий будут удалены, ученик напишет тест заново.`)) {
    return;
  }
  await api(`/roster/${encodeURIComponent(r.roster_entry_id)}/reset`, { method: "POST" });
  if (currentAttemptId && currentAttemptId === r.attempt_id) {
    currentAttemptId = null;
    $("#detailSection").classList.add("hidden");
  }
  toast(`Работа «${r.display_name}» сброшена.`, "ok");
  await loadRoster();
}

// A new PIN only matters for FUTURE starts: a student already inside the test
// keeps going (their session token stays valid). It also lifts a wrong-PIN lock.
async function regeneratePin(r) {
  if (
    !confirm(
      `Сгенерировать новый PIN для «${r.display_name}»?\n\n` +
        "Старый PIN перестанет подходить для входа. Если ученик уже пишет тест, " +
        "он продолжит без перерыва. Не забудьте дать ученику новую карточку."
    )
  ) {
    return;
  }
  const res = await api(`/roster/${encodeURIComponent(r.roster_entry_id)}/pin`, { method: "POST" });
  toast(`Новый PIN для «${r.display_name}»: ${res.pin}`, "ok", { timeout: 10000 });
  await loadRoster();
}

// Printable cut-out PIN cards (A4): name + test + link + PIN per student.
function openPinCards() {
  if (!currentTest) return;
  const w = window.open(`teacher-pins.html?test=${encodeURIComponent(currentTest.id)}`, "_blank");
  if (!w) toast("Браузер заблокировал новое окно — разрешите всплывающие окна для этого сайта.", "error");
}

async function removeStudent(r) {
  if (!confirm(`Удалить «${r.display_name}» из списка? Его работа (если есть) тоже будет удалена.`)) {
    return;
  }
  await api(`/roster/${encodeURIComponent(r.roster_entry_id)}`, { method: "DELETE" });
  if (currentAttemptId && currentAttemptId === r.attempt_id) {
    currentAttemptId = null;
    $("#detailSection").classList.add("hidden");
  }
  toast(`«${r.display_name}» удалён(а) из списка.`, "ok");
  await loadRoster();
}

// -- live monitoring (auto-refresh roster + results + the open detail) ----------- //
let liveTimer = null;
let pollInFlight = false;

async function liveTick() {
  if (pollInFlight || !token || !currentTest) return; // never overlap a slow poll
  pollInFlight = true;
  try {
    await loadRoster();
    if (currentAttemptId) await refreshDetail();
  } catch (e) {
    showError(e, "Обновление вживую");
  } finally {
    pollInFlight = false;
  }
}

function startLive() {
  stopLive();
  liveTimer = setInterval(liveTick, LIVE_INTERVAL_MS);
  $("#liveToggle").checked = true;
}

function stopLive() {
  if (liveTimer) clearInterval(liveTimer);
  liveTimer = null;
  const box = $("#liveToggle");
  if (box) box.checked = false;
}

function toggleLive() {
  if ($("#liveToggle").checked) startLive();
  else stopLive();
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// -- wire up ----------------------------------------------------------------- //
$("#loginBtn").onclick = signIn;
$("#password").addEventListener("keydown", (e) => e.key === "Enter" && signIn());
$("#logoutBtn").onclick = () => signOut("");
$("#refreshBtn").onclick = guard("Не удалось обновить", async () => {
  await refresh();
  toast("Обновлено.", "ok", { timeout: 1500 });
});
$("#addStudentBtn").onclick = guard("Не удалось добавить ученика", addStudent);
$("#studentName").addEventListener("keydown", (e) => {
  if (e.key === "Enter") guard("Не удалось добавить ученика", addStudent)();
});
$("#bulkToggleBtn").onclick = () => {
  $("#bulkBox").classList.toggle("hidden");
  if (!$("#bulkBox").classList.contains("hidden")) $("#bulkNames").focus();
};
$("#bulkAddBtn").onclick = guard("Не удалось добавить учеников", addStudentsBulk);
$("#importToggleBtn").onclick = () => $("#importBox").classList.toggle("hidden");
$("#importBtn").onclick = guard("Не удалось импортировать тест", importTest);
$("#copyLinkBtn").onclick = guard("Не удалось скопировать ссылку", () => currentTest && copyStudentLink(currentTest));
$("#pinCardsBtn").onclick = openPinCards;
$("#clearPinLocksBtn").onclick = guard("Не удалось снять блокировки", clearPinLocks);
$("#liveToggle").addEventListener("change", toggleLive);
$("#finalizeMissingBtn").onclick = guard("Не удалось дооценить", finalizeMissing);
$("#windowSaveBtn").onclick = guard("Не удалось сохранить окно экзамена", saveWindow);
$("#windowClearBtn").onclick = guard("Не удалось снять окно экзамена", clearWindow);
$("#windowOpenNowBtn").onclick = guard("Не удалось открыть тест", openWindowNow);
$("#windowCloseNowBtn").onclick = guard("Не удалось закрыть тест", closeWindowNow);
$("#csvBtn").onclick = guard("Не удалось скачать CSV", downloadCsv);
$("#resultsSearch").addEventListener("input", renderResults);
document.querySelectorAll("#resultsTable th.sortable").forEach((th) => {
  th.onclick = () => onSortClick(th.dataset.sort);
});

if (token) showApp();
