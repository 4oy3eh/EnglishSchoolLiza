/* Teacher content editor (Phase 13): fix a test without code.
 *
 * Loaded after teacher.js and reuses its helpers (api, guard, toast, showError,
 * esc, assetUrl, levelLabel, LEVELS, SKILLS, TEST_STATUS, refresh,
 * renderStimulusReview, windowLabel) and its B2–C2 constants FIVE_SKILL_LEVELS /
 * KWT_LIMITS / kwtLimits — so teacher.html must load teacher.js BEFORE this file. Every server value is interpolated through esc(); asset
 * ids go through assetUrl() (app/admin/CLAUDE.md).
 *
 * Server rules it surfaces (app/content/editor.py):
 * - a DRAFT without student attempts is edited freely; the review queue
 *   ("Проверка черновиков") is still the only way to publish (golden rule #5);
 * - a test that is PUBLISHED or already HAS ATTEMPTS ("locked") only takes cosmetic
 *   text fixes (titles, prompts, stimulus text — never option / A–H list texts,
 *   which carry the key), and only after the teacher explicitly switches on
 *   "исправление опечаток" (the saves then carry ?force=true). Changing a key / the
 *   item set needs a draft copy (with attempts: ONLY a draft copy — unpublishing
 *   would not unlock it, a regrade would re-score finished work);
 * - items inside a section that shares a stimulus are never reordered (rule #7) —
 *   the arrows only appear where the server allows it.
 * "Предпросмотр как ученик" renders GET /admin/tests/{id}/preview, the delivery
 * projection: it has no answer key at all (rule #1).
 */
"use strict";

let editorTest = null; // the Test being edited (authoring shape, with the key)
let editorStats = null; // { attempt_count, ... } for the banner
let editorForce = false; // locked test: the teacher confirmed "typo fixes only"
let editorPapers = null; // GET /admin/tests/{id}/papers: {explicit, papers: [{paper, label, minutes, official_minutes}]}

const ITEM_TYPES = {
  single_choice: "выбор ответа",
  gap_fill: "вписать ответ",
  matching: "сопоставление (буква)",
  open_writing: "письмо",
  colour_task: "раскрасить",
  key_word_transformation: "трансформация с ключевым словом (B2–C2)",
};
const STIMULUS_KINDS = {
  passage_text: "текст",
  gapped_text: "текст с пропусками",
  audio_asset: "аудио",
  image_set: "картинки",
  matching_pool: "список вариантов A–H",
};

const edQ = (id) => encodeURIComponent(String(id));
const lines = (s) => String(s || "").split(/[\n,]/).map((x) => x.trim()).filter(Boolean);
const textLines = (s) => String(s || "").split("\n").map((x) => x.trim()).filter(Boolean);
const idList = (s) => String(s || "").split(/[\s,]+/).map((x) => x.trim()).filter(Boolean);
const variants = (s) => String(s || "").split("/").map((x) => x.trim()).filter(Boolean);

// Key word transformation answers <-> text: one alternative per line,
// "part A | part B", variants of one half separated by "/":
//   "a good idea | to go"
//   "are said / is said | to be"
function kwtAnswersText(answers) {
  return (answers || [])
    .map((a) => `${(a.part_a || []).join(" / ")} | ${(a.part_b || []).join(" / ")}`)
    .join("\n");
}

function parseKwtAnswers(text) {
  return textLines(text).map((line) => {
    const cut = line.indexOf("|");
    if (cut < 0) return { part_a: variants(line), part_b: [] };
    return { part_a: variants(line.slice(0, cut)), part_b: variants(line.slice(cut + 1)) };
  });
}

const REPORTED_SKILL_OPTS = [["", "как навык раздела"], ["use_of_english", "Use of English (B2–C2, части 2–4)"]];

function canReorderItems(sec) {
  const st = sec.stimulus || {};
  if (st.kind !== "passage_text" && st.kind !== "gapped_text") return false;
  return !String(st.text || "").trim() && !(st.images || []).length;
}

// -- open / close ------------------------------------------------------------ //
async function openEditor(testId) {
  const [t, stats] = await Promise.all([
    api(`/tests/${edQ(testId)}`),
    api(`/tests/${edQ(testId)}/stats`),
  ]);
  editorTest = t;
  editorStats = stats;
  editorForce = false;
  editorPapers = await api(`/tests/${edQ(testId)}/papers`).catch(() => null);
  $("#edPreview").classList.add("hidden");
  $("#edPreview").innerHTML = "";
  $("#editorSection").classList.remove("hidden");
  renderEditor();
  $("#editorSection").scrollIntoView({ behavior: "smooth" });
}

function closeEditor() {
  editorTest = null;
  editorStats = null;
  editorPapers = null;
  editorForce = false;
  const sec = $("#editorSection");
  if (sec) sec.classList.add("hidden");
  if ($("#edBody")) $("#edBody").innerHTML = "";
  if ($("#edPreview")) $("#edPreview").innerHTML = "";
}

function forceQs() {
  return editorForce ? "?force=true" : "";
}

// Apply a server response (the whole updated Test) and redraw, keeping open sections.
function applyTest(t, { full = true } = {}) {
  editorTest = t;
  $("#edTitle").textContent = `${t.title} (${levelLabel(t.level)}, ${TEST_STATUS[t.status] || t.status})`;
  if (full) renderEditor();
  // Sections / duration may have changed the papers or their minutes: refresh them.
  api(`/tests/${edQ(t.id)}/papers`)
    .then((p) => {
      if (editorTest && editorTest.id === t.id) {
        editorPapers = p;
        renderPaperMinutes();
      }
    })
    .catch(() => {});
}

// -- time per part (paper): explicit minutes or the proportional default ------- //
// The whole test always takes «Длительность»; a student given only some parts
// gets the sum of their minutes. Explicit minutes must sum to the duration.
function renderPaperMinutes() {
  const box = $("#edPaperMinutes");
  if (!box || !editorTest) return;
  const info = editorPapers;
  if (!info || info.papers.length < 2) {
    box.innerHTML = "";
    return;
  }
  const rows = info.papers
    .map(
      (p) => `<label>${esc(p.label)}, мин
        <input type="number" min="1" max="600" data-paper-min="${esc(p.paper)}" value="${esc(p.minutes)}" />
        <span class="muted">офиц. ${esc(p.official_minutes)}</span></label>`
    )
    .join("");
  box.innerHTML = `<p style="margin:8px 0 4px"><b>Время по частям</b>
      <span class="muted">— ${info.explicit ? "задано вручную" : "пропорционально официальному времени Cambridge"}.
      Нужно, когда ученику выдают только некоторые части (его время — сумма минут этих частей).</span></p>
    <div class="ed-grid">${rows}</div>
    <div class="row wrap" style="margin-top:6px">
      <span data-f="paper-sum"></span>
      <button data-act="save-paper-min">Сохранить время частей</button>
      ${info.explicit ? '<button data-act="reset-paper-min" title="Разделить длительность пропорционально официальному времени">Пропорционально</button>' : ""}
    </div><p class="err" data-f="paper-err"></p>`;
  box.querySelectorAll("[data-paper-min]").forEach((i) => (i.oninput = updatePaperSum));
  box.querySelector('[data-act="save-paper-min"]').onclick = () => savePaperMinutes(readPaperMinutes());
  const reset = box.querySelector('[data-act="reset-paper-min"]');
  if (reset) reset.onclick = () => savePaperMinutes(null);
  updatePaperSum();
}

function readPaperMinutes() {
  const out = {};
  document.querySelectorAll("#edPaperMinutes [data-paper-min]").forEach((i) => {
    out[i.dataset.paperMin] = Number(i.value);
  });
  return out;
}

// Live «сумма = N мин» check against the duration field (green = equal).
function updatePaperSum() {
  const el = document.querySelector('#edPaperMinutes [data-f="paper-sum"]');
  if (!el) return;
  const sum = Object.values(readPaperMinutes()).reduce((a, x) => a + (Number.isFinite(x) ? x : 0), 0);
  const dur = Number($("#edMeta [data-f='duration']").value);
  const ok = sum === dur;
  el.className = ok ? "ok" : "err";
  el.textContent = ok
    ? `сумма = ${sum} мин ✓ (= длительности)`
    : `сумма = ${sum} мин, а длительность ${dur} мин — должно совпадать`;
}

function savePaperMinutes(minutes) {
  const errEl = document.querySelector('#edPaperMinutes [data-f="paper-err"]');
  return withErr(errEl, "Время частей не сохранено", async () => {
    // Timing meta (like the exam window): allowed even on a published test with work.
    const t = await api(`/tests/${edQ(editorTest.id)}/paper-minutes`, {
      method: "PUT",
      body: { paper_minutes: minutes },
    });
    applyTest(t, { full: false });
    toast(minutes ? "Время частей сохранено." : "Время частей — пропорционально.", "ok", { timeout: 2000 });
  });
}

// -- rendering --------------------------------------------------------------- //
// Mirrors app/content/editor.py `is_locked`: published OR already written by students.
function editorLocked() {
  const n = editorStats ? editorStats.attempt_count : 0;
  return editorTest.status === "published" || n > 0;
}

function renderBanner() {
  const t = editorTest;
  const n = editorStats ? editorStats.attempt_count : 0;
  const published = t.status === "published";
  const el = $("#edBanner");
  if (!editorLocked()) {
    el.innerHTML = `<div class="notice">Черновик: можно менять всё — тексты, ключи, вопросы и разделы.
      После правок одобрите тест в разделе «Проверка черновиков», чтобы опубликовать.</div>`;
    return;
  }
  const head = n
    ? `${published ? "Тест опубликован, и п" : "П"}о нему уже есть работы учеников (${esc(n)}).`
    : "Тест опубликован.";
  const how = n
    ? `Ключ ответов, варианты ответа, вопросы и разделы менять нельзя — иначе сданные работы
       при переоценке будут проверены по новому ключу. Нажмите «Создать черновик-копию» и
       правьте копию (снятие с публикации здесь не поможет).`
    : "Менять ключ ответов, варианты ответа, вопросы и разделы можно только в черновике.";
  el.innerHTML = `<div class="notice">
    <b>${head}</b> ${how}
    <div class="actions" style="margin-top:6px">
      <button class="primary" data-act="copy">Создать черновик-копию</button>
      ${published && !n ? `<button data-act="unpub">Снять с публикации</button>` : ""}
      <label class="muted"><input type="checkbox" data-act="force" ${editorForce ? "checked" : ""} />
        исправить только опечатки (название, формулировки вопросов, тексты разделов)</label>
    </div></div>`;
  el.querySelector('[data-act="copy"]').onclick = guard("Не удалось создать копию", duplicateForEdit);
  const unpub = el.querySelector('[data-act="unpub"]');
  if (unpub) {
    unpub.onclick = guard("Не удалось снять с публикации", async () => {
      await unpublishTest(editorTest);
      await openEditor(editorTest.id);
    });
  }
  el.querySelector('[data-act="force"]').onchange = (e) => {
    if (e.target.checked && !confirm(
      (n ? `По тесту уже есть работы учеников (${n}).` : "Тест опубликован.") +
      " Исправлять в нём текст?\n\nМожно менять только формулировки: название, текст " +
      "вопросов и разделов, пункты задания. Тексты вариантов ответа и списка A–H, ключ " +
      "ответов и состав вопросов сервер не даст изменить — для этого создайте " +
      "черновик-копию. Каждое сохранение записывается в журнал."
    )) {
      e.target.checked = false;
      return;
    }
    editorForce = e.target.checked;
  };
}

async function duplicateForEdit() {
  const copy = await api(`/tests/${edQ(editorTest.id)}/duplicate`, { method: "POST" });
  toast(`Создан черновик «${copy.title}» (id ${copy.id}). Редактируйте его и одобрите в «Проверке черновиков».`, "ok", { timeout: 8000 });
  await refresh();
  await openEditor(copy.id);
}

function renderEditor() {
  const t = editorTest;
  if (!t) return;
  const open = new Set([...document.querySelectorAll("details.ed-sec[open]")].map((d) => d.dataset.sid));
  $("#edTitle").textContent = `${t.title} (${levelLabel(t.level)}, ${TEST_STATUS[t.status] || t.status})`;
  renderBanner();
  const levelOpts = Object.entries(LEVELS)
    .map(([k, v]) => `<option value="${esc(k)}" ${k === t.level ? "selected" : ""}>${esc(v)}</option>`)
    .join("");
  const kindOpts = Object.entries(STIMULUS_KINDS)
    .map(([k, v]) => `<option value="${esc(k)}">${esc(v)}</option>`)
    .join("");
  $("#edBody").innerHTML = `
    <div class="ed-item" id="edMeta">
      <div class="ed-grid">
        <label>Название <input data-f="title" value="${esc(t.title)}" /></label>
        <label>Уровень <select data-f="level">${levelOpts}</select></label>
        <label>Длительность, мин <input data-f="duration" type="number" min="1" value="${esc(t.duration_minutes)}" /></label>
      </div>
      <div class="row" style="margin-top:6px"><button class="primary" data-act="save-meta">Сохранить</button>
        <span class="muted">id теста: ${esc(t.id)}</span></div>
      <div id="edPaperMinutes"></div>
      <p class="muted" data-f="window-note">Окно экзамена: ${esc(windowLabel(t).text)} — задаётся в
        «Ученики и результаты» и меняется в любой момент, даже у опубликованного теста с работами.</p>
      <p class="err"></p>
    </div>
    <div id="edSections"></div>
    <div class="row wrap" style="margin-top:8px">
      <span class="muted">Новый раздел с материалом:</span>
      <select id="edNewSectionKind">${kindOpts}</select>
      <button id="edAddSectionBtn">Добавить раздел</button>
    </div>`;
  $("#edMeta [data-act='save-meta']").onclick = () => saveMeta();
  $("#edMeta [data-f='duration']").oninput = updatePaperSum;
  renderPaperMinutes();
  $("#edAddSectionBtn").onclick = guard("Не удалось добавить раздел", addSection);
  const box = $("#edSections");
  t.sections.forEach((sec, idx) => {
    const d = document.createElement("details");
    d.className = "ed-sec";
    d.dataset.sid = sec.id;
    if (open.has(sec.id)) d.open = true;
    box.appendChild(d);
    renderSection(d, sec, idx);
  });
}

function renderSection(d, sec, idx) {
  const n = editorTest.sections.length;
  const skillOpts = [["", "—"], ...Object.entries(SKILLS)]
    .map(([k, v]) => `<option value="${esc(k)}" ${k === (sec.skill || "") ? "selected" : ""}>${esc(v)}</option>`)
    .join("");
  const typeOpts = Object.entries(ITEM_TYPES)
    .filter(([k]) => k !== "colour_task")
    .filter(([k]) => k !== "matching" || sec.stimulus.kind === "matching_pool")
    .map(([k, v]) => `<option value="${esc(k)}">${esc(v)}</option>`)
    .join("");
  d.innerHTML = `
    <summary>
      <span>Раздел ${esc(idx + 1)}: ${esc(sec.title || sec.id)}</span>
      <span class="tag">${esc(skillLabel(sec.skill))}</span>
      <span class="tag">${esc(STIMULUS_KINDS[sec.stimulus.kind] || sec.stimulus.kind)}</span>
      <span class="muted">${esc(sec.items.length)} вопр.</span>
      <span class="spacer"></span>
      <button class="small" data-act="sec-up" ${idx === 0 ? "disabled" : ""} title="Выше">↑</button>
      <button class="small" data-act="sec-down" ${idx === n - 1 ? "disabled" : ""} title="Ниже">↓</button>
      <button class="small danger" data-act="sec-del" title="Удалить раздел">Удалить</button>
    </summary>
    <div class="ed-sec-body">
      <div class="ed-grid">
        <label>Название раздела / инструкция <input data-f="sec-title" value="${esc(sec.title || "")}" /></label>
        <label>Навык <select data-f="sec-skill">${skillOpts}</select></label>
        <label>Навык в отчёте <select data-f="sec-reported">${REPORTED_SKILL_OPTS
          .map(([k, v]) => `<option value="${esc(k)}" ${k === (sec.reported_skill || "") ? "selected" : ""}>${esc(v)}</option>`)
          .join("")}</select></label>
      </div>
      ${stimulusForm(sec.stimulus)}
      <div class="row"><button class="primary" data-act="save-sec">Сохранить раздел</button>
        <span class="muted">id: ${esc(sec.id)}</span></div>
      <p class="err" data-err="sec"></p>
      <div class="ed-items"></div>
      <div class="row wrap">
        <span class="muted">Новый вопрос:</span>
        <select data-f="new-item-type">${typeOpts}</select>
        <button data-act="add-item">Добавить вопрос</button>
      </div>
    </div>`;
  const stop = (fn) => (e) => {
    e.preventDefault(); // buttons inside <summary> must not toggle the section
    e.stopPropagation();
    fn();
  };
  d.querySelector('[data-act="sec-up"]').onclick = stop(guard("Не удалось переместить", () => moveSection(idx, -1)));
  d.querySelector('[data-act="sec-down"]').onclick = stop(guard("Не удалось переместить", () => moveSection(idx, 1)));
  d.querySelector('[data-act="sec-del"]').onclick = stop(guard("Не удалось удалить раздел", () => removeSection(sec)));
  d.querySelector('[data-act="save-sec"]').onclick = () => saveSection(d, sec);
  d.querySelector('[data-act="add-item"]').onclick = guard("Не удалось добавить вопрос", () => addItem(d, sec));
  wireUploads(d.querySelector(".ed-sec-body"));
  const items = d.querySelector(".ed-items");
  const movable = canReorderItems(sec);
  sec.items.forEach((it, i) => {
    const card = document.createElement("div");
    card.className = "ed-item";
    card.dataset.itemId = it.id;
    items.appendChild(card);
    renderItem(card, sec, it, i, movable);
  });
}

// An asset field: the id input + upload button (+ a thumbnail / player).
function assetField(label, f, value, { accept = ".png,.jpg,.jpeg,.webp", append = false } = {}) {
  const isAudio = accept.includes("mp3");
  const preview = value && !append
    ? (isAudio
      ? `<audio controls preload="none" src="${assetUrl(value)}"></audio>`
      : `<img class="ed-thumb" src="${assetUrl(value)}" alt="" />`)
    : "";
  return `<div class="ed-field">${esc(label)}
    <div class="ed-row"><input data-f="${esc(f)}" value="${esc(value || "")}" />
      <button type="button" class="small" data-upload="${esc(f)}" data-accept="${esc(accept)}" data-append="${append ? "1" : ""}">Загрузить файл…</button>
      ${preview}</div></div>`;
}

function stimulusForm(st) {
  const images = assetField("Картинки к материалу (id через запятую)", "st-images", (st.images || []).join(", "), { append: true });
  if (st.kind === "passage_text" || st.kind === "gapped_text") {
    return `<div class="ed-field">Текст материала${st.kind === "gapped_text" ? " (с пропусками)" : ""}
      <textarea data-f="st-text" rows="6">${esc(st.text || "")}</textarea></div>${images}`;
  }
  if (st.kind === "audio_asset") {
    return `<p class="muted">У каждой части аудирования может быть свой mp3: укажите его здесь.
        Тот же файл, что у предыдущей части, — запись просто продолжается. Записи звучат
        по очереди: следующая начинается, только когда закончилась предыдущая.</p>
      ${assetField("Аудиозапись (mp3)", "st-asset", st.asset_id, { accept: ".mp3" })}
      ${assetField("Вступление / проверка звука (необязательно)", "st-preview", st.preview_asset_id, { accept: ".mp3" })}
      <div class="ed-grid">
        <label>Прослушиваний <input data-f="st-plays" type="number" min="1" value="${esc(st.plays)}" /></label>
        <label class="muted"><span><input type="checkbox" data-f="st-locked" ${st.locked ? "checked" : ""} /> запрет перемотки и паузы</span></label>
      </div>${images}`;
  }
  if (st.kind === "image_set") {
    return assetField("Картинки (id через запятую)", "st-assets", (st.asset_ids || []).join(", "), { append: true });
  }
  if (st.kind === "matching_pool") {
    const pool = (st.options || []).map((o) => `${o.key}: ${o.text}${o.image ? ` | ${o.image}` : ""}`).join("\n");
    return `<div class="ed-field">Текст над списком (необязательно)
        <textarea data-f="st-text" rows="3">${esc(st.text || "")}</textarea></div>
      <div class="ed-field">Варианты списка — по одному в строке, «буква: текст»; картинка к слову —
        «буква: текст | файл.png» (YLE: у каждого слова своя картинка)
        <textarea data-f="st-pool" rows="6">${esc(pool)}</textarea></div>
      ${images}
      <label class="muted"><span><input type="checkbox" data-f="st-once" ${st.each_option_once ? "checked" : ""} />
        каждый вариант — только к одному вопросу (текст с пропусками; лишний вариант не нужен)</span></label>
      <p class="muted">Аудирование с двумя заданиями на одну запись (C1/C2, часть 4): укажите одну и ту же
        запись в обоих разделах — она прозвучит один раз для обоих заданий.</p>
      ${assetField("Аудиозапись этой части (необязательно, mp3)", "st-pool-audio", st.audio ? st.audio.asset_id : "", { accept: ".mp3" })}`;
  }
  return `<p class="muted">Этот тип материала редактируется через экспорт/импорт JSON.</p>`;
}

function readStimulus(root, st) {
  const v = (f) => {
    const el = root.querySelector(`[data-f="${f}"]`);
    return el ? el.value : "";
  };
  const out = { ...st };
  if ("images" in st || ["passage_text", "gapped_text", "audio_asset", "matching_pool"].includes(st.kind)) {
    out.images = idList(v("st-images"));
  }
  if (st.kind === "passage_text" || st.kind === "gapped_text") out.text = v("st-text");
  if (st.kind === "audio_asset") {
    out.asset_id = v("st-asset").trim();
    out.preview_asset_id = v("st-preview").trim() || null;
    out.plays = Number(v("st-plays"));
    out.locked = root.querySelector('[data-f="st-locked"]').checked;
  }
  if (st.kind === "image_set") out.asset_ids = idList(v("st-assets"));
  if (st.kind === "matching_pool") {
    out.text = v("st-text");
    out.each_option_once = root.querySelector('[data-f="st-once"]').checked;
    const audioId = v("st-pool-audio").trim();
    out.audio = audioId
      ? { ...(st.audio || { kind: "audio_asset", plays: 1, locked: true }), asset_id: audioId }
      : null;
    out.options = textLines(v("st-pool")).map((line) => {
      const m = line.match(/^\s*([^:]+?)\s*[:.)—-]\s*(.*)$/);
      const opt = m ? { key: m[1], text: m[2] } : { key: "", text: line };
      // "A: cat | cat.png" -> the option's own picture.
      const pic = opt.text.match(/^(.*?)\s*\|\s*(\S+)\s*$/);
      if (pic) {
        opt.text = pic[1];
        opt.image = pic[2];
      }
      return opt;
    });
  }
  return out;
}

function optionRow(it, o, i) {
  const checked = o.key === it.correct ? "checked" : "";
  const body = o.kind === "image"
    ? `<input data-f="opt-asset" value="${esc(o.asset_id)}" placeholder="id картинки" />
       <button type="button" class="small" data-upload-opt="${esc(i)}">Загрузить…</button>
       <img class="ed-thumb" src="${assetUrl(o.asset_id)}" alt="" />
       <input data-f="opt-alt" value="${esc(o.alt || "")}" placeholder="подпись (необязательно)" />`
    : `<input data-f="opt-text" value="${esc(o.text)}" />`;
  return `<div class="ed-opt" data-kind="${esc(o.kind)}">
    <label title="Правильный ответ"><input type="radio" name="correct-${esc(it.id)}" data-f="opt-correct" ${checked} /> верно</label>
    <input data-f="opt-key" value="${esc(o.key)}" title="Буква варианта" />
    ${body}
    <button type="button" class="small danger" data-act="opt-del" title="Убрать вариант">×</button>
  </div>`;
}

function itemForm(sec, it) {
  const prompt = `<div class="ed-field">${it.item_type === "key_word_transformation" ? "Исходное предложение" : "Текст вопроса"}
    <textarea data-f="prompt" rows="2">${esc(it.prompt)}</textarea></div>`;
  if (it.item_type === "key_word_transformation") {
    return `${prompt}
      <div class="ed-grid">
        <label>Ключевое слово <input data-f="key_word" value="${esc(it.key_word || "")}" /></label>
        <label>Второе предложение (один пропуск «____»)
          <input data-f="gapped_sentence" value="${esc(it.gapped_sentence || "")}" /></label>
        <label>Ответы — по одному варианту в строке: «часть A | часть B», варианты части через «/»
          <textarea data-f="kwt-answers" rows="4">${esc(kwtAnswersText(it.answers))}</textarea>
          <span class="muted">каждая часть — 1 балл; ключевое слово должно входить в ответ</span></label>
        <label>Слов от <input data-f="min_words" type="number" min="1" max="10" value="${esc(it.min_words ?? "")}" placeholder="${esc(kwtLimits({}, editorTest && editorTest.level)[0])}" /></label>
        <label>Слов до <input data-f="max_words" type="number" min="1" max="12" value="${esc(it.max_words ?? "")}" placeholder="${esc(kwtLimits({}, editorTest && editorTest.level)[1])}" />
          <span class="muted">пусто = по уровню (B2 2–5, C1 3–6, C2 3–8, с ключевым словом)</span></label>
      </div>`;
  }
  if (it.item_type === "single_choice") {
    return `${prompt}
      ${assetField("Картинка к вопросу (необязательно)", "image", it.image)}
      <div class="ed-field">Варианты ответа (отметьте правильный)
        <div data-f="options">${it.options.map((o, i) => optionRow(it, o, i)).join("")}</div>
        <div class="row"><button type="button" class="small" data-act="opt-add-text">+ текстовый вариант</button>
          <button type="button" class="small" data-act="opt-add-image">+ вариант-картинка</button></div></div>
      <label class="muted"><span><input type="checkbox" data-f="shuffle_options" ${it.shuffle_options === false ? "" : "checked"} />
        перемешивать варианты для каждого ученика (снимите для «yes / no», «true / false / doesn't say»)</span></label>`;
  }
  if (it.item_type === "gap_fill") {
    return `${prompt}
      <div class="ed-grid">
        <label>Принимаемые ответы (через запятую или с новой строки)
          <textarea data-f="accepted" rows="3">${esc((it.accepted || []).join("\n"))}</textarea></label>
        <label>Допустимые варианты написания (необязательно)
          <textarea data-f="variants" rows="3">${esc((it.accepted_variants || []).join("\n"))}</textarea></label>
        <label>Неверные формы (не засчитывать, даже если похожи на ответ)
          <textarea data-f="rejected" rows="3">${esc((it.rejected || []).join("\n"))}</textarea></label>
        <label>Орфография
          <select data-f="spelling">
            <option value=""${it.spelling ? "" : " selected"}>по разделу (чтение и Use of English — точно, аудирование — с опечатками; C1/C2 — точно)</option>
            <option value="exact"${it.spelling === "exact" ? " selected" : ""}>только точное написание</option>
            <option value="tolerant"${it.spelling === "tolerant" ? " selected" : ""}>допускать опечатки</option>
          </select></label>
        <label>Исходное слово (словообразование, необязательно)
          <input data-f="stem_word" value="${esc(it.stem_word || "")}" placeholder="например COMPETE" /></label>
        <label>Максимум слов в ответе (необязательно)
          <input data-f="max_words" type="number" min="1" max="10" value="${esc(it.max_words ?? "")}" placeholder="без ограничения" />
          <span class="muted">1 — для open cloze / словообразования: длиннее или «два ответа» (in/on) — неверно</span></label>
      </div>`;
  }
  if (it.item_type === "matching") {
    const keys = (sec.stimulus.options || []).map((o) => o.key);
    const opts = keys
      .map((k) => `<option value="${esc(k)}" ${k === it.correct ? "selected" : ""}>${esc(k)}</option>`)
      .join("");
    return `${prompt}<label class="ed-field">Правильная буква (из списка раздела)
      <select data-f="correct">${opts}</select></label>`;
  }
  if (it.item_type === "open_writing") {
    return `${prompt}
      <div class="ed-grid">
        <label>Пункты задания (по одному в строке)
          <textarea data-f="bullets" rows="3">${esc((it.bullet_points || []).join("\n"))}</textarea></label>
        <label>Критерии оценивания (ученик не видит)
          <textarea data-f="rubric" rows="3">${esc(it.rubric || "")}</textarea></label>
        <label>Минимум слов <input data-f="word_min" type="number" min="1" value="${esc(it.word_min)}" /></label>
        <label>Оценивание <select data-f="grade_mode">
          <option value="llm" ${it.grade_mode === "llm" ? "selected" : ""}>ИИ (с проверкой учителя)</option>
          <option value="manual" ${it.grade_mode === "manual" ? "selected" : ""}>вручную</option></select></label>
        <label class="muted"><span><input type="checkbox" data-f="optional" ${it.optional ? "checked" : ""} /> задание на выбор (одно из нескольких)</span></label>
      </div>`;
  }
  if (it.item_type === "colour_task") {
    return `${prompt}
      ${assetField("Картинка для раскрашивания", "asset", it.asset_id)}
      <div class="ed-grid">
        <label>Цвета (через запятую) <input data-f="palette" value="${esc((it.palette || []).join(", "))}" /></label>
        <label>Что чем раскрасить (ученик не видит)
          <textarea data-f="ckey" rows="2">${esc(it.key || "")}</textarea></label>
      </div>`;
  }
  return `<p class="muted">Неизвестный тип вопроса — используйте экспорт/импорт JSON.</p>`;
}

// Per-item weight (contracts `points`): empty = the default (objective 1, writing /
// colouring 5). Writing: the AI's 0–5 mark is scaled to it (whole marks, half-up).
function defaultPoints(it) {
  if (it.item_type === "key_word_transformation") return 2;
  return it.item_type === "open_writing" || it.item_type === "colour_task" ? 5 : 1;
}

const numOrNull = (s) => (String(s).trim() === "" ? null : Number(s));

function pointsField(it) {
  const value = it.points == null ? "" : it.points;
  return `<div class="ed-grid"><label>Баллы
      <input data-f="points" type="number" min="1" max="20" step="1" value="${esc(value)}"
        placeholder="${esc(defaultPoints(it))}" />
      <span class="muted">пусто = по умолчанию (${esc(defaultPoints(it))})</span></label></div>`;
}

function renderItem(card, sec, it, i, movable) {
  const n = sec.items.length;
  card.innerHTML = `
    <div class="ed-item-head">
      <span class="tag">${esc(i + 1)}</span><span class="tag">${esc(ITEM_TYPES[it.item_type] || it.item_type)}</span>
      <span class="muted">${esc(it.id)}</span><span class="spacer" style="flex:1"></span>
      ${movable ? `<button class="small" data-act="item-up" ${i === 0 ? "disabled" : ""}>↑</button>
        <button class="small" data-act="item-down" ${i === n - 1 ? "disabled" : ""}>↓</button>` : ""}
      <button class="small danger" data-act="item-del">Удалить вопрос</button>
    </div>
    ${itemForm(sec, it)}
    ${pointsField(it)}
    <div class="row" style="margin-top:6px"><button class="primary" data-act="save-item">Сохранить вопрос</button></div>
    <p class="err" data-err="item"></p>`;
  card.querySelector('[data-act="save-item"]').onclick = () => saveItem(card, sec, it);
  card.querySelector('[data-act="item-del"]').onclick = guard("Не удалось удалить вопрос", () => removeItem(it));
  const up = card.querySelector('[data-act="item-up"]');
  if (up) up.onclick = guard("Не удалось переместить", () => moveItem(sec, i, -1));
  const down = card.querySelector('[data-act="item-down"]');
  if (down) down.onclick = guard("Не удалось переместить", () => moveItem(sec, i, 1));
  wireUploads(card);
  if (it.item_type === "single_choice") wireOptions(card, it);
}

function wireOptions(card, it) {
  const box = card.querySelector('[data-f="options"]');
  const rewire = () => {
    box.querySelectorAll('[data-act="opt-del"]').forEach((b) => {
      b.onclick = () => b.closest(".ed-opt").remove();
    });
    box.querySelectorAll("[data-upload-opt]").forEach((b) => {
      b.onclick = guard("Не удалось загрузить файл", async () => {
        const id = await pickAndUpload(".png,.jpg,.jpeg,.webp");
        if (id) b.closest(".ed-opt").querySelector('[data-f="opt-asset"]').value = id;
      });
    });
  };
  const nextKey = () => {
    const used = new Set([...box.querySelectorAll('[data-f="opt-key"]')].map((x) => x.value));
    for (const c of "ABCDEFGHIJ") if (!used.has(c)) return c;
    return "";
  };
  const add = (o) => {
    box.insertAdjacentHTML("beforeend", optionRow(it, o, box.children.length));
    rewire();
  };
  card.querySelector('[data-act="opt-add-text"]').onclick = () => add({ kind: "text", key: nextKey(), text: "" });
  card.querySelector('[data-act="opt-add-image"]').onclick = guard("Не удалось загрузить файл", async () => {
    const id = await pickAndUpload(".png,.jpg,.jpeg,.webp");
    if (id) add({ kind: "image", key: nextKey(), asset_id: id, alt: null });
  });
  rewire();
}

function readItem(card, sec, it) {
  const v = (f) => {
    const el = card.querySelector(`[data-f="${f}"]`);
    return el ? el.value : "";
  };
  const out = { ...it, prompt: v("prompt") };
  const pts = v("points").trim();
  out.points = pts === "" ? null : Number(pts);
  if (it.item_type === "single_choice") {
    out.image = v("image").trim() || null;
    const sh = card.querySelector('[data-f="shuffle_options"]');
    out.shuffle_options = sh ? sh.checked : it.shuffle_options !== false;
    out.options = [];
    out.correct = "";
    card.querySelectorAll(".ed-opt").forEach((row) => {
      const key = row.querySelector('[data-f="opt-key"]').value.trim();
      const o = row.dataset.kind === "image"
        ? { kind: "image", key, asset_id: row.querySelector('[data-f="opt-asset"]').value.trim(),
          alt: row.querySelector('[data-f="opt-alt"]').value.trim() || null }
        : { kind: "text", key, text: row.querySelector('[data-f="opt-text"]').value };
      out.options.push(o);
      if (row.querySelector('[data-f="opt-correct"]').checked) out.correct = key;
    });
  } else if (it.item_type === "gap_fill") {
    out.accepted = lines(v("accepted"));
    out.accepted_variants = lines(v("variants"));
    out.rejected = lines(v("rejected"));
    out.spelling = v("spelling") || null;
    out.stem_word = v("stem_word").trim() || null;
    out.max_words = numOrNull(v("max_words"));
  } else if (it.item_type === "key_word_transformation") {
    out.key_word = v("key_word").trim();
    out.gapped_sentence = v("gapped_sentence");
    out.answers = parseKwtAnswers(v("kwt-answers"));
    out.min_words = numOrNull(v("min_words"));
    out.max_words = numOrNull(v("max_words"));
  } else if (it.item_type === "matching") {
    out.correct = v("correct");
  } else if (it.item_type === "open_writing") {
    out.bullet_points = textLines(v("bullets"));
    out.rubric = v("rubric");
    out.word_min = Number(v("word_min"));
    out.grade_mode = v("grade_mode");
    out.optional = card.querySelector('[data-f="optional"]').checked;
  } else if (it.item_type === "colour_task") {
    out.asset_id = v("asset").trim();
    out.palette = lines(v("palette"));
    out.key = v("ckey");
  }
  return out;
}

// -- uploads ------------------------------------------------------------------ //
function pickAndUpload(accept) {
  return new Promise((resolve, reject) => {
    const input = document.createElement("input");
    input.type = "file";
    input.accept = accept;
    input.style.display = "none";
    input.onchange = async () => {
      const file = input.files[0];
      input.remove();
      if (!file) return resolve(null);
      try {
        const form = new FormData();
        form.append("file", file, file.name);
        const res = await api("/assets", { method: "POST", form });
        toast(`Файл загружен: ${res.asset_id}. Не забудьте сохранить.`, "ok");
        resolve(res.asset_id);
      } catch (e) {
        reject(e);
      }
    };
    document.body.appendChild(input);
    input.click();
  });
}

function wireUploads(root) {
  root.querySelectorAll("[data-upload]").forEach((b) => {
    b.onclick = guard("Не удалось загрузить файл", async () => {
      const id = await pickAndUpload(b.dataset.accept);
      if (!id) return;
      const input = b.parentElement.querySelector("input[data-f]");
      input.value = b.dataset.append && input.value.trim() ? `${input.value.trim()}, ${id}` : id;
    });
  });
}

// -- saves (inline Russian error + toast) ------------------------------------ //
async function withErr(errEl, context, fn) {
  errEl.textContent = "";
  try {
    await fn();
  } catch (e) {
    if (!e.silent) errEl.textContent = e.message || String(e);
    showError(e, context);
  }
}

function saveMeta() {
  const root = $("#edMeta");
  return withErr(root.querySelector(".err"), "Не сохранено", async () => {
    const body = {
      title: root.querySelector('[data-f="title"]').value,
      level: root.querySelector('[data-f="level"]').value,
      duration_minutes: Number(root.querySelector('[data-f="duration"]').value),
    };
    // Explicit part minutes must keep summing to the duration: a new duration is
    // sent together with the minutes shown (the server checks the sum).
    if (editorPapers && editorPapers.explicit && body.duration_minutes !== editorTest.duration_minutes) {
      body.paper_minutes = readPaperMinutes();
    }
    applyTest(await api(`/tests/${edQ(editorTest.id)}${forceQs()}`, { method: "PATCH", body }));
    toast("Сохранено.", "ok", { timeout: 1500 });
    await refresh();
  });
}

function saveSection(d, sec) {
  return withErr(d.querySelector('[data-err="sec"]'), "Раздел не сохранён", async () => {
    const body = {
      title: d.querySelector('[data-f="sec-title"]').value.trim() || null,
      skill: d.querySelector('[data-f="sec-skill"]').value || null,
      reported_skill: d.querySelector('[data-f="sec-reported"]').value || null,
      stimulus: readStimulus(d.querySelector(".ed-sec-body"), sec.stimulus),
    };
    applyTest(await api(`/tests/${edQ(editorTest.id)}/sections/${edQ(sec.id)}${forceQs()}`, { method: "PATCH", body }));
    toast("Раздел сохранён.", "ok", { timeout: 1500 });
  });
}

function saveItem(card, sec, it) {
  return withErr(card.querySelector('[data-err="item"]'), "Вопрос не сохранён", async () => {
    const body = readItem(card, sec, it);
    const t = await api(`/tests/${edQ(editorTest.id)}/items/${edQ(it.id)}${forceQs()}`, { method: "PUT", body });
    applyTest(t, { full: false });
    // Redraw only this card (other unsaved cards keep their edits).
    const fresh = t.sections.find((s) => s.id === sec.id);
    const idx = fresh ? fresh.items.findIndex((x) => x.id === it.id) : -1;
    if (fresh && idx >= 0) renderItem(card, fresh, fresh.items[idx], idx, canReorderItems(fresh));
    toast("Вопрос сохранён.", "ok", { timeout: 1500 });
  });
}

// -- structure (draft only — the server refuses on a published test) --------- //
async function moveSection(idx, delta) {
  const order = editorTest.sections.map((s) => s.id);
  const j = idx + delta;
  if (j < 0 || j >= order.length) return;
  [order[idx], order[j]] = [order[j], order[idx]];
  applyTest(await api(`/tests/${edQ(editorTest.id)}/sections/reorder`, { method: "POST", body: { order } }));
}

async function moveItem(sec, i, delta) {
  const order = sec.items.map((x) => x.id);
  const j = i + delta;
  if (j < 0 || j >= order.length) return;
  [order[i], order[j]] = [order[j], order[i]];
  applyTest(await api(`/tests/${edQ(editorTest.id)}/sections/${edQ(sec.id)}/items/reorder`, { method: "POST", body: { order } }));
}

async function removeSection(sec) {
  if (!confirm(`Удалить раздел «${sec.title || sec.id}» со всеми вопросами (${sec.items.length})?`)) return;
  applyTest(await api(`/tests/${edQ(editorTest.id)}/sections/${edQ(sec.id)}`, { method: "DELETE" }));
  toast("Раздел удалён.", "ok", { timeout: 1500 });
  await refresh();
}

async function removeItem(it) {
  if (!confirm(`Удалить вопрос «${String(it.prompt).slice(0, 60)}»?`)) return;
  applyTest(await api(`/tests/${edQ(editorTest.id)}/items/${edQ(it.id)}`, { method: "DELETE" }));
  toast("Вопрос удалён.", "ok", { timeout: 1500 });
  await refresh();
}

function itemTemplate(type, sec) {
  if (type === "single_choice") {
    return { item_type: type, prompt: "Новый вопрос", correct: "A",
      options: [{ kind: "text", key: "A", text: "вариант A" }, { kind: "text", key: "B", text: "вариант B" }] };
  }
  if (type === "gap_fill") return { item_type: type, prompt: "Новый вопрос ____", accepted: ["ответ"] };
  if (type === "matching") {
    return { item_type: type, prompt: "Новый вопрос", correct: ((sec.stimulus.options || [])[0] || {}).key || "A" };
  }
  if (type === "key_word_transformation") {
    return { item_type: type, prompt: "Going there now would be a good plan.", key_word: "IDEA",
      gapped_sentence: "It would be ____ there now.", answers: [{ part_a: ["a good idea"], part_b: ["to go"] }] };
  }
  // B2–C2 writing is marked out of 20 (4 criteria × 0–5).
  const five = editorTest && FIVE_SKILL_LEVELS.has(editorTest.level);
  return { item_type: "open_writing", prompt: "Новое задание", word_min: five ? 140 : 25, rubric: "",
    bullet_points: [], ...(five ? { points: 20 } : {}) };
}

async function addItem(d, sec) {
  const type = d.querySelector('[data-f="new-item-type"]').value;
  const t = await api(`/tests/${edQ(editorTest.id)}/sections/${edQ(sec.id)}/items`, {
    method: "POST", body: { item: itemTemplate(type, sec) },
  });
  applyTest(t);
  toast("Вопрос добавлен — заполните его и сохраните.", "ok", { timeout: 2500 });
  await refresh();
}

async function addSection() {
  const kind = $("#edNewSectionKind").value;
  let stimulus = { kind, text: "" };
  let item = itemTemplate("gap_fill");
  if (kind === "audio_asset") {
    const id = await pickAndUpload(".mp3");
    if (!id) return;
    stimulus = { kind, asset_id: id };
    item = itemTemplate("single_choice");
  } else if (kind === "image_set") {
    const id = await pickAndUpload(".png,.jpg,.jpeg,.webp");
    if (!id) return;
    stimulus = { kind, asset_ids: [id] };
  } else if (kind === "matching_pool") {
    stimulus = { kind, text: "", options: [{ key: "A", text: "вариант A" }, { key: "B", text: "вариант B" }] };
    item = { item_type: "matching", prompt: "Новый вопрос", correct: "A" };
  }
  const t = await api(`/tests/${edQ(editorTest.id)}/sections`, {
    method: "POST", body: { section: { title: "Новый раздел", stimulus, items: [item] } },
  });
  applyTest(t);
  const last = t.sections[t.sections.length - 1];
  const d = document.querySelector(`details.ed-sec[data-sid="${CSS.escape(last.id)}"]`);
  if (d) d.open = true;
  toast("Раздел добавлен.", "ok", { timeout: 1500 });
  await refresh();
}

// -- preview as a student (keyless projection) ------------------------------- //
async function showPreview() {
  const box = $("#edPreview");
  const p = await api(`/tests/${edQ(editorTest.id)}/preview`);
  box.innerHTML = `<h2 style="margin-top:16px">Предпросмотр как ученик — ${esc(p.title)} (${esc(p.duration_minutes)} мин)</h2>
    <p class="muted">Так ученик увидит тест: без ключа ответов. Порядок вариантов у каждого ученика свой.</p>
    ${p.sections.map(renderPreviewSection).join("")}`;
  box.classList.remove("hidden");
  box.scrollIntoView({ behavior: "smooth" });
  await loadPreviewStudyImages(box);
}

// The study (memory) picture of an IQ subtest. The public /assets/ refuses study
// assets (a student could re-fetch them after the study window), so the teacher's
// preview loads it from the bearer-authenticated admin route as a blob.
let previewStudyUrls = [];
function renderPreviewStudy(sec) {
  const src = (editorTest.sections || []).find((s) => s.id === sec.id);
  const st = src && src.study;
  if (!st) return "";
  return `<div class="pv-study"><b>Запоминание (${esc(st.study_seconds)} с)</b>
    <span class="muted">— ученик видит это только в окне запоминания</span>
    ${st.text ? `<div>${esc(st.text)}</div>` : ""}
    ${st.asset_id ? `<div><img class="tv-img" data-study-section="${esc(sec.id)}" alt="" /></div>` : ""}</div>`;
}

async function loadPreviewStudyImages(box) {
  previewStudyUrls.forEach((u) => URL.revokeObjectURL(u));
  previewStudyUrls = [];
  for (const img of box.querySelectorAll("img[data-study-section]")) {
    try {
      const sid = img.getAttribute("data-study-section");
      const resp = await api(`/tests/${edQ(editorTest.id)}/sections/${edQ(sid)}/study/asset`, { raw: true });
      const url = URL.createObjectURL(await resp.blob());
      previewStudyUrls.push(url);
      img.src = url;
    } catch (_) {
      img.alt = "картинка для запоминания не загрузилась";
    }
  }
}

function renderPreviewSection(sec) {
  return `<div class="pv-sec"><h3>${esc(sec.title || "")} <span class="tag">${esc(skillLabel(sec.skill))}</span></h3>
    ${renderStimulusReview(sec.stimulus)}
    ${renderPreviewStudy(sec)}
    ${sec.items.map((it, i) => renderPreviewItem(it, i)).join("")}</div>`;
}

function renderPreviewItem(it, i) {
  const img = it.image ? `<div><img class="tv-img" src="${assetUrl(it.image)}" alt="" /></div>` : "";
  let body = "";
  if (it.item_type === "single_choice") {
    body = it.options
      .map((o) => `<div class="pv-opt">○ ${o.kind === "image"
        ? `<img class="tv-img" src="${assetUrl(o.asset_id)}" alt="${esc(o.alt || "")}" />`
        : esc(o.text)}</div>`)
      .join("");
  } else if (it.item_type === "gap_fill") {
    body = `<input disabled placeholder="ответ ученика" />` +
      (it.stem_word ? ` <b>(${esc(String(it.stem_word).toUpperCase())})</b>` : "") +
      (it.max_words ? ` <span class="muted">не больше ${esc(it.max_words)} сл.</span>` : "");
  } else if (it.item_type === "key_word_transformation") {
    body = `<div><b>${esc(String(it.key_word || "").toUpperCase())}</b></div>
      <div>${esc(it.gapped_sentence)}</div>
      <span class="muted">${esc(it.min_words)}–${esc(it.max_words)} слов, включая ключевое слово</span>`;
  } else if (it.item_type === "matching") {
    body = `<span class="muted">ученик выбирает букву из списка</span>`;
  } else if (it.item_type === "open_writing") {
    body = `${(it.bullet_points || []).length ? `<ul>${it.bullet_points.map((b) => `<li>${esc(b)}</li>`).join("")}</ul>` : ""}
      <span class="muted">минимум ${esc(it.word_min)} слов</span>`;
  } else if (it.item_type === "colour_task") {
    body = `<img class="tv-img" src="${assetUrl(it.asset_id)}" alt="" />`;
  }
  return `<div class="pv-item" style="margin:8px 0"><b>${esc(i + 1)}.</b> ${esc(it.prompt)}${img}${body}</div>`;
}

$("#edPreviewBtn").onclick = guard("Не удалось открыть предпросмотр", showPreview);
$("#edCloseBtn").onclick = () => closeEditor();
