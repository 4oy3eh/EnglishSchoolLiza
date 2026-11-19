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
 * Listening audio is per part (see "Per-part listening audio" below): each
 * listening part may carry its own recording (track) or continue the previous
 * one. Tracks play strictly in order; moving between parts never stops or
 * restarts a playing track. The optional sound-check (`preview_asset_id`) is
 * freely replayable; a track is locked — single play, no pause/seek/restart.
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
 *
 * Access (Phase 14): after picking a name the student types their 4-character
 * classroom PIN; `/start` answers with a per-attempt session token that is kept in
 * the localStorage resume state and sent as `X-Attempt-Token` on every
 * `/exam/attempts/...` call (and by the recorder). A reload resumes with the stored
 * token (no PIN); a 401 (token gone / not valid) brings back the PIN screen.
 *
 * Language (Phase 14): the runner is English like the Cambridge exam. For YLE
 * levels (`YLE_LEVELS`) the start screen offers EN / УКР / РУС for the UI chrome
 * and instructions ONLY (`I18N` + `t()`); test content — stimuli, prompts, options,
 * part titles — is never translated. Non-YLE tests stay English, no switcher.
 *
 * Answering aids (Phase 15, view only — see "Answering aids" below): matching
 * buttons show letter + text and "→ N" when used by another question; parts whose
 * options are all short words get a tap-to-place word bank (desktop drag too);
 * numbered gaps in a passage mirror their answers inline. The answer model and
 * every PUT payload are unchanged, so grading / telemetry / integrity are too.
 */
"use strict";

const ACTIVE_KEY = "exam:active"; // {testId, attemptId, rosterEntryId, name, token}
const LANG_KEY = "exam:lang"; // chosen UI language for YLE tests: en | uk | ru
const TOKEN_HEADER = "X-Attempt-Token";
const $ = (sel) => document.querySelector(sel);

const params = new URLSearchParams(location.search);
const linkTestId = params.get("test");

let rec = null; // telemetry recorder
let units = []; // [{blockIdx, skill, section, trackIdx}] in served order (one per section)
let blocks = []; // [{skill, label, unitIdxs:[...]}]
let tracks = []; // listening tracks in play order (see "Per-part listening audio")
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
let tickTimer = null;
let pollTimer = null;
let finished = false;
let reviewOpen = false; // the review-before-submit panel is showing
let submitting = false; // a manual submit is in progress (drain -> confirm -> POST)
let warnedBelow = Infinity; // smallest timer-warning threshold (s) already shown

// Display-only timer warnings (seconds left -> message key). The server still
// decides expiry (rule #3); these only make sure the student notices the clock.
const TIME_WARNINGS = [
  { at: 120, crit: true, key: "warn2" },
  { at: 600, crit: false, key: "warn10" },
];

// -------------------------------------------------------------------------- //
// UI language (chrome + instructions only — NEVER the test content).
// -------------------------------------------------------------------------- //
// YLE levels get the EN / УКР / РУС switch. A set, so Starters / Flyers only need
// their level code added here once the item bank has them.
const YLE_LEVELS = new Set(["A1_MOVERS", "PRE_A1_STARTERS", "A2_FLYERS"]);
let lang = "en";
let yle = false;

const I18N = {
  en: {
    langLabel: "Language",
    pickTitle: "Choose your name to begin",
    pickEmpty: "No students on this test yet — ask your teacher.",
    noTestId: "This link is missing its test id.",
    rosterLoadErr: "Could not load the class list. Try again.",
    done: "(done)",
    disclosure:
      "For exam integrity, this test records your answers, on-screen activity, and basic " +
      "device information (browser, operating system, screen size, time zone, language and " +
      "IP address). In writing tasks it also records typing patterns (how fast you type, " +
      "pauses, corrections, and blocked paste attempts) — not the keys you press or what you " +
      "tried to paste. The one exception: pressing a screenshot key (such as PrintScreen) is " +
      "noted — only the fact, never an image of your screen. By choosing your name and " +
      "starting, you acknowledge this monitoring.",
    pinHello: (name) => `Hi, ${name}!`,
    pinPrompt: "Type the PIN your teacher gave you.",
    pinResume: "Type your PIN again to continue your test.",
    pinLabel: "Your PIN",
    pinStart: "Start the test",
    pinContinue: "Continue the test",
    pinBack: "This is not me",
    pinTimer: "The timer starts when you press the button, and it does not stop.",
    pinEmpty: "Type your 4-character PIN.",
    pinShape: "A PIN has 4 letters or numbers. Look at your PIN card.",
    pinWrong: (left) =>
      "Wrong PIN. Check your PIN card and try again." +
      (left > 0 ? ` Tries left: ${left}.` : ""),
    pinLocked: (min) => `Too many wrong tries. Wait ${min} min and try again, or ask your teacher.`,
    nameGone: "Your name is not on the list any more. Ask your teacher.",
    startErr: "Could not start the test. Try again.",
    skill_reading: "Reading",
    skill_writing: "Writing",
    skill_listening: "Listening",
    skill_other: "Section",
    partOf: (i, n) => `Part ${i} of ${n}`,
    partN: (i) => `Part ${i}`,
    back: "Back",
    next: "Next",
    reviewSubmit: "Review & submit",
    timeLeftAria: "Time left",
    hideWarn: "Hide the time warning",
    ok: "OK",
    warn2: "Only 2 minutes left! The test is submitted automatically when time runs out.",
    warn10: "10 minutes left. The timer does not stop — check your answers and submit.",
    audioWarn: (o) =>
      (o.multi
        ? "🎧 Each part has its own recording, and they play in order. This recording plays "
        : "🎧 One continuous recording. The test track plays ") +
      `${o.plays === 1 ? "once" : `${o.plays} times`} and ` +
      (o.locked ? "**cannot be paused, rewound, or restarted**" : "should be played carefully") +
      " — moving between questions does **not** stop it. " +
      (o.resuming
        ? "If your page reloaded, the test track continues from where you were — it cannot start over."
        : o.hasPreview
          ? "Play the sound-check below as many times as you like, then start the test."
          : "Make sure your sound works before you start."),
    soundCheck: "▶ Sound check",
    replayFreely: "Replay freely to check your sound",
    startListening: "▶ Start listening test",
    resumeListening: "▶ Resume listening",
    previewFirst: "Listen to the sound check first",
    continuesWhere: "Continues from where you were",
    plays: (n) => `${n} play(s)`,
    playsLeft: (n) => `${n} play(s) left`,
    continuing: "Continuing…",
    playing: "Playing…",
    finished: "Finished",
    trackLabel: (i, n) => `Recording ${i} of ${n}`,
    startTrack: "▶ Play the recording",
    trackDone: "✓ The recording for this part has finished.",
    trackNextHint: "The next recording starts when you go to the next part.",
    trackWaits: (part) =>
      `🎧 The recording for this part plays after the recording for “${part}” ends. Recordings play in order.`,
    trackNowPlaying: (part) => `Now playing: the recording for “${part}”.`,
    goToPart: (part) => `Go to “${part}”`,
    replayTrack: "↻ Play it again",
    goToNextTrack: "Go to the next recording",
    replayOrNext: "You can play this recording again, or go on to the next one.",
    prevReplayLeft: (part, n) =>
      `The recording for “${part}” can still be played again (${n} left). Starting this one ends that.`,
    trackInterrupted: "⚠ The recording stopped. Press the button to continue from where it stopped.",
    trackFailed: "⚠ The recording could not be played — tell your teacher. You can go on with the test.",
    picFor: (title) => `Picture for ${title}`,
    picForPart: "Picture for this part",
    picOf: (i, n) => ` (${i} of ${n})`,
    chooseFrom: "Choose from:",
    question: "Question ",
    optPicture: (prompt, i) => `${prompt} — picture ${i}`,
    colourAlt: (prompt) => `Picture to colour: ${prompt}`,
    rubber: "rubber",
    words: (n, min) => `${n} / ${min} words`,
    usedIn: (n) => `used in question ${n}`,
    wbBankLabel: "Words to choose from",
    wbHint: "Tap a box, then tap a word. Tap the word again to take it out.",
    wbHintDrag: " You can also drag a word into a box.",
    wbEmpty: "tap here",
    wbPickBox: "Tap a question's box first.",
    wbAllDone: "All boxes are full. Tap a box to change its word.",
    wbActive: (n) => `Question ${n}: now tap a word.`,
    wbPlaced: (w, n) => `“${w}” is now in question ${n}.`,
    wbCleared: (n) => `Question ${n} is empty now.`,
    wbSlot: (n, w) =>
      w ? `Question ${n}: ${w}. Tap to change.` : `Question ${n}: empty. Tap it, then choose a word.`,
    clearAnswer: (n) => `Clear question ${n}`,
    gapGo: (n, w) =>
      w ? `Gap ${n}: ${w}. Go to the question.` : `Gap ${n}: not answered. Go to the question.`,
    toText: "↑ In the text",
    toTextAria: (n) => `Show gap ${n} in the text`,
    keyWord: "Key word",
    kwtRubric: (min, max) => `Use between ${min} and ${max} words, including the word given.`,
    kwtCount: (n) => `${n} word${n === 1 ? "" : "s"}`,
    oneWord: "One word only.",
    maxWords: (n) => `No more than ${n} words.`,
    onceWarn: (k, n) =>
      `${k} is also your answer to question ${n}. Each option fits only one gap — check which one is right.`,
    navTitle: "Your answers",
    answered: "answered",
    notAnswered: "not answered",
    answerRejected: "answer rejected",
    navLabel: (n, state) => `Question ${n}, ${state}`,
    reviewTitle: "Review your answers",
    timeLeftLabel: "Time left:",
    backToTest: "Back to the test",
    submitTest: "Submit test",
    savingSuffix: " (saving…)",
    goToState: (n, state) => `Go to question ${n} — ${state}`,
    goTo: (n) => `Go to question ${n}`,
    notAnsweredList: "Not answered: ",
    partCount: (a, n) => `${a} of ${n} answered`,
    reviewCount: (a, n, todo) => `${a} of ${n} answered · ${todo} not answered`,
    allAnswered: (n) => `All ${n} questions answered`,
    unsavedLine: (n, qs, failing) =>
      `⚠ ${n} answer(s) not saved yet (question ${qs}) — ` +
      (failing ? "check your connection, retrying…" : "saving…"),
    rejectedLine: (n, qs) =>
      `⚠ ${n} answer(s) rejected by the server (question ${qs}) — go back and change them.`,
    allSaved: "✓ All your answers are saved.",
    saved: "Saved",
    savingNow: "Saving…",
    notSavedRetry: (n) => `⚠ ${n} answer${n === 1 ? "" : "s"} not saved — retrying`,
    rejectedCount: (n) => `⚠ ${n} answer${n === 1 ? "" : "s"} rejected by the server`,
    sumUnsaved: (n, qs) => `${n} answer(s) could not be saved (question ${qs}).`,
    sumRejected: (n, qs) => `${n} answer(s) were rejected by the server (question ${qs}).`,
    confirmUnanswered: (n) => `You have ${n} unanswered question(s). Submit anyway?`,
    confirmUnsavedNet:
      "Check your connection and press Cancel to keep trying, or OK to submit without them.",
    confirmRejected: "Press Cancel to change those answers, or OK to submit without them.",
    submitFailed: "Submit failed — try again.",
    doneTitle: "Test submitted",
    doneMsg: "Your answers have been recorded. You can close this tab.",
    timeUp: "Time's up",
    timeUpMsg: "The exam time ended and your answers so far have been recorded.",
    already: "Already submitted",
    alreadyMsg:
      "This test was submitted earlier. Your answers have been recorded. You can close this tab.",
    tellTeacher: "Please tell your teacher.",
    windowNotYet: (iso) => `This test opens: ${fmtWhen(iso)}. Wait for your teacher.`,
    windowOpenUntil: (iso) => `You can start the test until ${fmtWhen(iso)}.`,
    windowClosed: "This test is closed. You can only continue a test you have already started.",
    pinWindowNotYet: (iso) => `The test is not open yet. It opens: ${fmtWhen(iso)}.`,
    pinWindowClosed: "The test is closed — you cannot start it now. Ask your teacher.",
  },
  uk: {
    langLabel: "Мова",
    pickTitle: "Вибери своє ім'я, щоб почати",
    pickEmpty: "У цьому тесті ще немає учнів. Запитай учителя.",
    noTestId: "У цьому посиланні немає номера тесту. Запитай учителя.",
    rosterLoadErr: "Не вдалося завантажити список класу. Спробуй ще раз.",
    done: "(готово)",
    disclosure:
      "Щоб тест був чесним, ми записуємо твої відповіді, що ти робиш на екрані, і просту " +
      "інформацію про пристрій (браузер, система, розмір екрана, часовий пояс, мова та " +
      "IP-адреса). У письмових завданнях ми також записуємо, як ти друкуєш (швидкість, паузи, " +
      "виправлення і заблоковані спроби вставити текст). Ми не записуємо, які клавіші ти " +
      "натискаєш, і не бачимо, що ти хотів(-ла) вставити. Якщо натиснути клавішу знімка " +
      "екрана (наприклад, PrintScreen), ми запишемо лише сам факт, а не картинку екрана. " +
      "Коли ти вибираєш своє ім'я і починаєш тест, ти погоджуєшся з цим.",
    pinHello: (name) => `Привіт, ${name}!`,
    pinPrompt: "Введи PIN-код, який тобі дав учитель.",
    pinResume: "Введи PIN-код ще раз, щоб продовжити тест.",
    pinLabel: "Твій PIN-код",
    pinStart: "Почати тест",
    pinContinue: "Продовжити тест",
    pinBack: "Це не я",
    pinTimer: "Час почне йти, коли ти натиснеш кнопку. Таймер не зупиняється.",
    pinEmpty: "Введи PIN-код із 4 знаків.",
    pinShape: "PIN-код — це 4 літери або цифри. Подивись на свою картку.",
    pinWrong: (left) =>
      "Неправильний PIN-код. Перевір свою картку і спробуй ще раз." +
      (left > 0 ? ` Залишилося спроб: ${left}.` : ""),
    pinLocked: (min) =>
      `Забагато неправильних спроб. Зачекай ${min} хв і спробуй ще раз або запитай учителя.`,
    nameGone: "Твого імені вже немає в списку. Запитай учителя.",
    startErr: "Не вдалося почати тест. Спробуй ще раз.",
    skill_reading: "Читання",
    skill_writing: "Письмо",
    skill_listening: "Аудіювання",
    skill_other: "Розділ",
    partOf: (i, n) => `Частина ${i} з ${n}`,
    partN: (i) => `Частина ${i}`,
    back: "Назад",
    next: "Далі",
    reviewSubmit: "Перевірити і здати",
    timeLeftAria: "Залишилось часу",
    hideWarn: "Сховати попередження про час",
    ok: "OK",
    warn2: "Залишилося лише 2 хвилини! Коли час скінчиться, тест здасться сам.",
    warn10: "Залишилося 10 хвилин. Таймер не зупиняється — перевір відповіді і здай тест.",
    audioWarn: (o) =>
      (o.multi
        ? "🎧 У кожної частини свій запис, вони звучать по черзі. Цей запис звучить "
        : "🎧 Це один довгий запис. Він звучить ") +
      (o.plays === 1 ? "один раз" : `${o.plays} рази`) +
      (o.locked ? ", і **його не можна зупинити, перемотати чи почати знову**" : "") +
      ". Коли ти переходиш до іншого питання, запис **не** зупиняється. " +
      (o.resuming
        ? "Якщо сторінка перезавантажилась, запис іде далі з того самого місця. Почати спочатку не можна."
        : o.hasPreview
          ? "Спочатку перевір звук — це можна робити скільки завгодно разів. Потім почни тест."
          : "Перед початком перевір, що звук працює."),
    soundCheck: "▶ Перевірка звуку",
    replayFreely: "Можна слухати скільки завгодно",
    startListening: "▶ Почати аудіювання",
    resumeListening: "▶ Слухати далі",
    previewFirst: "Спочатку послухай перевірку звуку",
    continuesWhere: "Запис продовжиться з того самого місця",
    plays: (n) => `Прослуховувань: ${n}`,
    playsLeft: (n) => `Залишилось прослуховувань: ${n}`,
    continuing: "Слухаємо далі…",
    playing: "Звучить…",
    finished: "Закінчено",
    trackLabel: (i, n) => `Запис ${i} з ${n}`,
    startTrack: "▶ Увімкнути запис",
    trackDone: "✓ Запис до цієї частини вже закінчився.",
    trackNextHint: "Наступний запис почнеться, коли ти перейдеш до наступної частини.",
    trackWaits: (part) =>
      `🎧 Запис до цієї частини зазвучить після того, як закінчиться запис до «${part}». Записи звучать по черзі.`,
    trackNowPlaying: (part) => `Зараз звучить запис до «${part}».`,
    goToPart: (part) => `Перейти до «${part}»`,
    replayTrack: "↻ Послухати ще раз",
    goToNextTrack: "Перейти до наступного запису",
    replayOrNext: "Цей запис можна послухати ще раз або перейти до наступного.",
    prevReplayLeft: (part, n) =>
      `Запис до «${part}» ще можна послухати знову (залишилось: ${n}). Якщо увімкнути цей запис, це вже буде неможливо.`,
    trackInterrupted: "⚠ Запис зупинився. Натисни кнопку, щоб слухати далі з того самого місця.",
    trackFailed: "⚠ Запис не вдалося відтворити — скажи вчителеві. Можна продовжувати тест.",
    picFor: (title) => `Малюнок до: ${title}`,
    picForPart: "Малюнок до цієї частини",
    picOf: (i, n) => ` (${i} з ${n})`,
    chooseFrom: "Вибери з:",
    question: "Питання ",
    optPicture: (prompt, i) => `${prompt} — малюнок ${i}`,
    colourAlt: (prompt) => `Малюнок, який треба розфарбувати: ${prompt}`,
    rubber: "гумка",
    words: (n, min) => `${n} / ${min} слів`,
    usedIn: (n) => `уже в питанні ${n}`,
    wbBankLabel: "Слова, з яких треба вибрати",
    wbHint: "Натисни на віконце, потім на слово. Натисни на слово ще раз, щоб забрати його.",
    wbHintDrag: " Слово можна також перетягнути у віконце.",
    wbEmpty: "натисни тут",
    wbPickBox: "Спочатку натисни на віконце питання.",
    wbAllDone: "Усі віконця заповнені. Натисни на віконце, щоб змінити слово.",
    wbActive: (n) => `Питання ${n}: тепер натисни на слово.`,
    wbPlaced: (w, n) => `«${w}» тепер у питанні ${n}.`,
    wbCleared: (n) => `Питання ${n} тепер порожнє.`,
    wbSlot: (n, w) =>
      w ? `Питання ${n}: ${w}. Натисни, щоб змінити.` : `Питання ${n}: порожньо. Натисни і вибери слово.`,
    clearAnswer: (n) => `Очистити питання ${n}`,
    gapGo: (n, w) =>
      w ? `Пропуск ${n}: ${w}. Перейти до питання.` : `Пропуск ${n}: немає відповіді. Перейти до питання.`,
    toText: "↑ У тексті",
    toTextAria: (n) => `Показати пропуск ${n} у тексті`,
    keyWord: "Ключове слово",
    kwtRubric: (min, max) =>
      `Використай від ${min} до ${max} слів, включно з даним словом.`,
    kwtCount: (n) => `Слів: ${n}`,
    oneWord: "Лише одне слово.",
    maxWords: (n) => `Не більше ${n} слів.`,
    onceWarn: (k, n) =>
      `${k} — також твоя відповідь у питанні ${n}. Кожен варіант підходить лише до одного пропуску — перевір, де він правильний.`,
    navTitle: "Твої відповіді",
    answered: "є відповідь",
    notAnswered: "немає відповіді",
    answerRejected: "відповідь не прийнято",
    navLabel: (n, state) => `Питання ${n}, ${state}`,
    reviewTitle: "Перевір свої відповіді",
    timeLeftLabel: "Залишилось часу:",
    backToTest: "Назад до тесту",
    submitTest: "Здати тест",
    savingSuffix: " (зберігаємо…)",
    goToState: (n, state) => `До питання ${n} — ${state}`,
    goTo: (n) => `До питання ${n}`,
    notAnsweredList: "Немає відповіді: ",
    partCount: (a, n) => `Відповіді: ${a} з ${n}`,
    reviewCount: (a, n, todo) => `Відповіді: ${a} з ${n} · без відповіді: ${todo}`,
    allAnswered: (n) => `Відповіді є на всі питання (${n})`,
    unsavedLine: (n, qs, failing) =>
      `⚠ Ще не збережено відповідей: ${n} (питання ${qs}) — ` +
      (failing ? "перевір інтернет, пробуємо ще раз…" : "зберігаємо…"),
    rejectedLine: (n, qs) =>
      `⚠ Сервер не прийняв відповідей: ${n} (питання ${qs}). Повернись і зміни їх.`,
    allSaved: "✓ Усі твої відповіді збережено.",
    saved: "Збережено",
    savingNow: "Зберігаємо…",
    notSavedRetry: (n) => `⚠ Не збережено: ${n} — пробуємо ще раз`,
    rejectedCount: (n) => `⚠ Сервер не прийняв: ${n}`,
    sumUnsaved: (n, qs) => `Не вдалося зберегти відповідей: ${n} (питання ${qs}).`,
    sumRejected: (n, qs) => `Сервер не прийняв відповідей: ${n} (питання ${qs}).`,
    confirmUnanswered: (n) => `Питань без відповіді: ${n}. Все одно здати тест?`,
    confirmUnsavedNet:
      "Перевір інтернет. Натисни «Скасувати», щоб спробувати ще, або «OK», щоб здати без них.",
    confirmRejected: "Натисни «Скасувати», щоб змінити ці відповіді, або «OK», щоб здати без них.",
    submitFailed: "Не вдалося здати тест. Спробуй ще раз.",
    doneTitle: "Тест здано",
    doneMsg: "Твої відповіді збережено. Можеш закрити цю вкладку.",
    timeUp: "Час вийшов",
    timeUpMsg: "Час тесту скінчився. Усі твої відповіді збережено.",
    already: "Тест уже здано",
    alreadyMsg: "Цей тест уже здали раніше. Відповіді збережено. Можеш закрити цю вкладку.",
    tellTeacher: "Скажи про це вчителю.",
    windowNotYet: (iso) => `Тест відкриється: ${fmtWhen(iso)}. Зачекай на вчителя.`,
    windowOpenUntil: (iso) => `Почати тест можна до ${fmtWhen(iso)}.`,
    windowClosed: "Тест закрито. Можна лише продовжити тест, який ти вже почав.",
    pinWindowNotYet: (iso) => `Тест ще не відкрито. Він відкриється: ${fmtWhen(iso)}.`,
    pinWindowClosed: "Тест закрито — почати його зараз не можна. Звернися до вчителя.",
  },
  ru: {
    langLabel: "Язык",
    pickTitle: "Выбери своё имя, чтобы начать",
    pickEmpty: "В этом тесте пока нет учеников. Спроси учителя.",
    noTestId: "В этой ссылке нет номера теста. Спроси учителя.",
    rosterLoadErr: "Не получилось загрузить список класса. Попробуй ещё раз.",
    done: "(готово)",
    disclosure:
      "Чтобы тест был честным, мы записываем твои ответы, что ты делаешь на экране, и простую " +
      "информацию об устройстве (браузер, система, размер экрана, часовой пояс, язык и " +
      "IP-адрес). В письменных заданиях мы также записываем, как ты печатаешь (скорость, " +
      "паузы, исправления и заблокированные попытки вставить текст). Мы не записываем, какие " +
      "клавиши ты нажимаешь, и не видим, что ты хотел(а) вставить. Если нажать клавишу " +
      "снимка экрана (например, PrintScreen), мы запишем только сам факт, а не картинку " +
      "экрана. Когда ты выбираешь своё имя и начинаешь тест, ты соглашаешься с этим.",
    pinHello: (name) => `Привет, ${name}!`,
    pinPrompt: "Введи PIN-код, который тебе дал учитель.",
    pinResume: "Введи PIN-код ещё раз, чтобы продолжить тест.",
    pinLabel: "Твой PIN-код",
    pinStart: "Начать тест",
    pinContinue: "Продолжить тест",
    pinBack: "Это не я",
    pinTimer: "Время пойдёт, когда ты нажмёшь кнопку. Таймер не останавливается.",
    pinEmpty: "Введи PIN-код из 4 знаков.",
    pinShape: "PIN-код — это 4 буквы или цифры. Посмотри на свою карточку.",
    pinWrong: (left) =>
      "Неверный PIN-код. Проверь свою карточку и попробуй ещё раз." +
      (left > 0 ? ` Осталось попыток: ${left}.` : ""),
    pinLocked: (min) =>
      `Слишком много неверных попыток. Подожди ${min} мин и попробуй ещё раз или спроси учителя.`,
    nameGone: "Твоего имени уже нет в списке. Спроси учителя.",
    startErr: "Не получилось начать тест. Попробуй ещё раз.",
    skill_reading: "Чтение",
    skill_writing: "Письмо",
    skill_listening: "Аудирование",
    skill_other: "Раздел",
    partOf: (i, n) => `Часть ${i} из ${n}`,
    partN: (i) => `Часть ${i}`,
    back: "Назад",
    next: "Дальше",
    reviewSubmit: "Проверить и сдать",
    timeLeftAria: "Осталось времени",
    hideWarn: "Скрыть предупреждение о времени",
    ok: "OK",
    warn2: "Осталось всего 2 минуты! Когда время закончится, тест сдастся сам.",
    warn10: "Осталось 10 минут. Таймер не останавливается — проверь ответы и сдай тест.",
    audioWarn: (o) =>
      (o.multi
        ? "🎧 У каждой части своя запись, они звучат по очереди. Эта запись звучит "
        : "🎧 Это одна длинная запись. Она звучит ") +
      (o.plays === 1 ? "один раз" : `${o.plays} раза`) +
      (o.locked ? ", и **её нельзя остановить, перемотать или начать заново**" : "") +
      ". Когда ты переходишь к другому вопросу, запись **не** останавливается. " +
      (o.resuming
        ? "Если страница перезагрузилась, запись идёт дальше с того же места. Начать сначала нельзя."
        : o.hasPreview
          ? "Сначала проверь звук — это можно делать сколько угодно раз. Потом начни тест."
          : "Перед началом проверь, что звук работает."),
    soundCheck: "▶ Проверка звука",
    replayFreely: "Можно слушать сколько угодно",
    startListening: "▶ Начать аудирование",
    resumeListening: "▶ Слушать дальше",
    previewFirst: "Сначала послушай проверку звука",
    continuesWhere: "Запись продолжится с того же места",
    plays: (n) => `Прослушиваний: ${n}`,
    playsLeft: (n) => `Осталось прослушиваний: ${n}`,
    continuing: "Слушаем дальше…",
    playing: "Звучит…",
    finished: "Закончено",
    trackLabel: (i, n) => `Запись ${i} из ${n}`,
    startTrack: "▶ Включить запись",
    trackDone: "✓ Запись к этой части уже закончилась.",
    trackNextHint: "Следующая запись начнётся, когда ты перейдёшь к следующей части.",
    trackWaits: (part) =>
      `🎧 Запись к этой части зазвучит после того, как закончится запись к «${part}». Записи звучат по очереди.`,
    trackNowPlaying: (part) => `Сейчас звучит запись к «${part}».`,
    goToPart: (part) => `Перейти к «${part}»`,
    replayTrack: "↻ Послушать ещё раз",
    goToNextTrack: "Перейти к следующей записи",
    replayOrNext: "Эту запись можно послушать ещё раз или перейти к следующей.",
    prevReplayLeft: (part, n) =>
      `Запись к «${part}» ещё можно послушать снова (осталось: ${n}). Если включить эту запись, это уже будет нельзя.`,
    trackInterrupted: "⚠ Запись остановилась. Нажми кнопку, чтобы слушать дальше с того же места.",
    trackFailed: "⚠ Запись не удалось воспроизвести — скажи учителю. Можно продолжать тест.",
    picFor: (title) => `Картинка к: ${title}`,
    picForPart: "Картинка к этой части",
    picOf: (i, n) => ` (${i} из ${n})`,
    chooseFrom: "Выбери из:",
    question: "Вопрос ",
    optPicture: (prompt, i) => `${prompt} — картинка ${i}`,
    colourAlt: (prompt) => `Картинка, которую нужно раскрасить: ${prompt}`,
    rubber: "ластик",
    words: (n, min) => `${n} / ${min} слов`,
    usedIn: (n) => `уже в вопросе ${n}`,
    wbBankLabel: "Слова, из которых нужно выбрать",
    wbHint: "Нажми на окошко, потом на слово. Нажми на слово ещё раз, чтобы убрать его.",
    wbHintDrag: " Слово можно также перетащить в окошко.",
    wbEmpty: "нажми сюда",
    wbPickBox: "Сначала нажми на окошко вопроса.",
    wbAllDone: "Все окошки заполнены. Нажми на окошко, чтобы поменять слово.",
    wbActive: (n) => `Вопрос ${n}: теперь нажми на слово.`,
    wbPlaced: (w, n) => `«${w}» теперь в вопросе ${n}.`,
    wbCleared: (n) => `Вопрос ${n} теперь пустой.`,
    wbSlot: (n, w) =>
      w ? `Вопрос ${n}: ${w}. Нажми, чтобы поменять.` : `Вопрос ${n}: пусто. Нажми и выбери слово.`,
    clearAnswer: (n) => `Очистить вопрос ${n}`,
    gapGo: (n, w) =>
      w ? `Пропуск ${n}: ${w}. Перейти к вопросу.` : `Пропуск ${n}: нет ответа. Перейти к вопросу.`,
    toText: "↑ В тексте",
    toTextAria: (n) => `Показать пропуск ${n} в тексте`,
    keyWord: "Ключевое слово",
    kwtRubric: (min, max) =>
      `Используй от ${min} до ${max} слов, включая данное слово.`,
    kwtCount: (n) => `Слов: ${n}`,
    oneWord: "Только одно слово.",
    maxWords: (n) => `Не больше ${n} слов.`,
    onceWarn: (k, n) =>
      `${k} — также твой ответ в вопросе ${n}. Каждый вариант подходит только к одному пропуску — проверь, где он верный.`,
    navTitle: "Твои ответы",
    answered: "есть ответ",
    notAnswered: "нет ответа",
    answerRejected: "ответ не принят",
    navLabel: (n, state) => `Вопрос ${n}, ${state}`,
    reviewTitle: "Проверь свои ответы",
    timeLeftLabel: "Осталось времени:",
    backToTest: "Назад к тесту",
    submitTest: "Сдать тест",
    savingSuffix: " (сохраняем…)",
    goToState: (n, state) => `К вопросу ${n} — ${state}`,
    goTo: (n) => `К вопросу ${n}`,
    notAnsweredList: "Нет ответа: ",
    partCount: (a, n) => `Ответы: ${a} из ${n}`,
    reviewCount: (a, n, todo) => `Ответы: ${a} из ${n} · без ответа: ${todo}`,
    allAnswered: (n) => `Ответы есть на все вопросы (${n})`,
    unsavedLine: (n, qs, failing) =>
      `⚠ Ещё не сохранено ответов: ${n} (вопрос ${qs}) — ` +
      (failing ? "проверь интернет, пробуем ещё раз…" : "сохраняем…"),
    rejectedLine: (n, qs) =>
      `⚠ Сервер не принял ответов: ${n} (вопрос ${qs}). Вернись и измени их.`,
    allSaved: "✓ Все твои ответы сохранены.",
    saved: "Сохранено",
    savingNow: "Сохраняем…",
    notSavedRetry: (n) => `⚠ Не сохранено: ${n} — пробуем ещё раз`,
    rejectedCount: (n) => `⚠ Сервер не принял: ${n}`,
    sumUnsaved: (n, qs) => `Не получилось сохранить ответов: ${n} (вопрос ${qs}).`,
    sumRejected: (n, qs) => `Сервер не принял ответов: ${n} (вопрос ${qs}).`,
    confirmUnanswered: (n) => `Вопросов без ответа: ${n}. Всё равно сдать тест?`,
    confirmUnsavedNet:
      "Проверь интернет. Нажми «Отмена», чтобы попробовать ещё, или «OK», чтобы сдать без них.",
    confirmRejected: "Нажми «Отмена», чтобы изменить эти ответы, или «OK», чтобы сдать без них.",
    submitFailed: "Не получилось сдать тест. Попробуй ещё раз.",
    doneTitle: "Тест сдан",
    doneMsg: "Твои ответы сохранены. Можешь закрыть эту вкладку.",
    timeUp: "Время вышло",
    timeUpMsg: "Время теста закончилось. Все твои ответы сохранены.",
    already: "Тест уже сдан",
    alreadyMsg: "Этот тест уже сдали раньше. Ответы сохранены. Можешь закрыть эту вкладку.",
    tellTeacher: "Скажи об этом учителю.",
    windowNotYet: (iso) => `Тест откроется: ${fmtWhen(iso)}. Подожди учителя.`,
    windowOpenUntil: (iso) => `Начать тест можно до ${fmtWhen(iso)}.`,
    windowClosed: "Тест закрыт. Можно только продолжить тест, который ты уже начал.",
    pinWindowNotYet: (iso) => `Тест ещё не открыт. Он откроется: ${fmtWhen(iso)}.`,
    pinWindowClosed: "Тест закрыт — начать его сейчас нельзя. Обратись к учителю.",
  },
};

// A moment (ISO from the server) in the runner's language and the device's local
// time, e.g. "18 Nov, 09:00" / "18 лист., 09:00".
const DATE_LOCALES = { en: "en-GB", uk: "uk-UA", ru: "ru-RU" };
function fmtWhen(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  return d.toLocaleString(DATE_LOCALES[lang] || "en-GB", {
    day: "numeric", month: "short", hour: "2-digit", minute: "2-digit",
  });
}

// Look up a UI string in the current language (EN fallback); functions get `args`.
function t(key, ...args) {
  const table = I18N[lang] || I18N.en;
  let v = key in table ? table[key] : I18N.en[key];
  if (typeof v === "function") v = v(...args);
  return v === undefined ? key : v;
}

// Static markup: [data-i18n] text, [data-i18n-aria] aria-label; plus <html lang>.
function applyStaticI18n() {
  document.documentElement.lang = lang;
  document.querySelectorAll("[data-i18n]").forEach((el) => {
    el.textContent = t(el.dataset.i18n);
  });
  document.querySelectorAll("[data-i18n-aria]").forEach((el) => {
    el.setAttribute("aria-label", t(el.dataset.i18nAria));
  });
  document.querySelectorAll("#langSwitch button").forEach((b) => {
    b.setAttribute("aria-pressed", String(b.dataset.lang === lang));
  });
}

function storedLang() {
  try {
    const v = localStorage.getItem(LANG_KEY);
    return v && I18N[v] ? v : "en";
  } catch {
    return "en";
  }
}

// Decide the UI language for a test level: YLE -> the remembered choice (default
// EN) + the switcher; anything else -> English only, no switcher.
function configureLanguage(level) {
  yle = YLE_LEVELS.has(level);
  lang = yle ? storedLang() : "en";
  $("#langSwitch").classList.toggle("hidden", !yle || !startVisible());
  applyStaticI18n();
}

function setLang(next) {
  if (!yle || !I18N[next]) return;
  lang = next;
  try {
    localStorage.setItem(LANG_KEY, next);
  } catch {
    /* storage unavailable — the choice lasts for this page only */
  }
  applyStaticI18n();
  renderPickerList();
  renderPinTexts();
  renderWindowNote();
}

// -------------------------------------------------------------------------- //
// Exam window (the teacher's schedule, from /info on the SERVER clock).
// -------------------------------------------------------------------------- //
let testInfo = null; // GET /tests/{id}/info: window_state, opens_at, starts_until, server_now
let serverOffsetMs = 0; // server - device clock, from `server_now`
let windowRecheck = null;

// A notice above the name list: "opens at …" / "closed" / "start until …". The
// list stays: after closing a student may still CONTINUE a started test (a resume
// is never window-checked); a new start answers 403 with the reason.
function renderWindowNote() {
  const el = $("#windowNote");
  if (!el) return;
  const st = testInfo && testInfo.window_state;
  let text = "";
  if (st === "not_yet_open" && testInfo.opens_at) text = t("windowNotYet", testInfo.opens_at);
  else if (st === "closed") text = t("windowClosed");
  else if (st === "open" && testInfo.starts_until) text = t("windowOpenUntil", testInfo.starts_until);
  el.textContent = text;
  el.classList.toggle("hidden", !text);
  el.classList.toggle("closed", st !== "open");
}

function applyTestInfo(info) {
  testInfo = info;
  if (info && info.server_now) serverOffsetMs = new Date(info.server_now).getTime() - Date.now();
  renderWindowNote();
  // Re-check /info when the window should open / close (server clock), so the
  // notice flips without a reload. One pending timer at a time.
  if (windowRecheck) clearTimeout(windowRecheck);
  windowRecheck = null;
  if (!info || !linkTestId) return;
  const at = info.window_state === "not_yet_open" ? info.opens_at
    : info.window_state === "open" ? info.starts_until : null;
  if (!at) return;
  const wait = new Date(at).getTime() - (Date.now() + serverOffsetMs) + 1500;
  if (wait > 0 && wait < 2 ** 31 - 1) {
    windowRecheck = setTimeout(async () => {
      try {
        applyTestInfo(await api(`/tests/${encodeURIComponent(linkTestId)}/info`));
      } catch {
        /* offline — the next start attempt reports the window anyway */
      }
    }, wait);
  }
}

// -------------------------------------------------------------------------- //
// Session token (per attempt, from /start) + API helper.
// -------------------------------------------------------------------------- //
let curToken = null; // in-memory copy (localStorage may be unavailable)
let reauthing = false;

function sessionToken() {
  const a = getActive();
  return (a && a.token) || curToken;
}

async function api(path, { method = "GET", body, token } = {}) {
  // `ngrok-skip-browser-warning` makes ngrok's free tier skip its interstitial
  // page for these XHR calls (otherwise it returns that HTML instead of JSON and
  // the runner can't load the roster/test). Harmless on non-ngrok hosts.
  const headers = { "ngrok-skip-browser-warning": "true" };
  if (body) headers["Content-Type"] = "application/json";
  const isAttemptCall = path.startsWith("/attempts/");
  const tok = token !== undefined ? token : isAttemptCall ? sessionToken() : null;
  if (tok) headers[TOKEN_HEADER] = tok;
  const resp = await fetch(`/exam${path}`, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!resp.ok) {
    const err = new Error(`${method} ${path} -> ${resp.status}`);
    err.status = resp.status;
    err.headers = resp.headers;
    if (resp.status === 401 && isAttemptCall) sessionLost();
    throw err;
  }
  return resp.status === 204 ? null : resp.json();
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// Escape, then turn the translation's own **bold** marks into <b> (no raw HTML in I18N).
function escBold(s) {
  return esc(s).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>");
}

function getActive() {
  try {
    return JSON.parse(localStorage.getItem(ACTIVE_KEY) || "null");
  } catch {
    return null;
  }
}

function setActive(active) {
  try {
    if (active) localStorage.setItem(ACTIVE_KEY, JSON.stringify(active));
    else localStorage.removeItem(ACTIVE_KEY);
  } catch {
    /* storage unavailable — curToken / curAttempt carry this page */
  }
}

// The server refused our session token (401): it is gone or not valid here. Drop
// it (the dirty answers are already mirrored to localStorage) and ask for the PIN
// again — mid-exam via a reload, so the resumed runner resends them after it.
function sessionLost() {
  if (reauthing || finished) return;
  const a = getActive();
  if (a) setActive({ ...a, token: null });
  curToken = null;
  if (curAttempt) {
    reauthing = true;
    stopTimers();
    location.reload();
  }
}

// -------------------------------------------------------------------------- //
// Start screens: pick-your-name, then the PIN.
// -------------------------------------------------------------------------- //
let rosterCache = null; // the last roster list, re-rendered on a language switch
let pinTarget = null; // {roster_entry_id, display_name} being signed in
let pinResumeMode = false; // re-entering the PIN for an attempt already started
let pinErr = null; // {key, args} of the PIN screen's current error (re-translated)
let pinBusy = false;

function startVisible() {
  return !$("#pick").classList.contains("hidden") || !$("#pinScreen").classList.contains("hidden");
}

function showStart(which) {
  $("#pick").classList.toggle("hidden", which !== "pick");
  $("#pinScreen").classList.toggle("hidden", which !== "pin");
  $("#disclosure").classList.remove("hidden");
  $("#langSwitch").classList.toggle("hidden", !yle);
}

function hideStart() {
  for (const id of ["#pick", "#pinScreen", "#disclosure", "#langSwitch"]) {
    $(id).classList.add("hidden");
  }
}

// An error line that follows the language switch: remember its key.
function setKeyedText(sel, key) {
  const el = $(sel);
  if (key) {
    el.dataset.i18n = key;
    el.textContent = t(key);
  } else {
    delete el.dataset.i18n;
    el.textContent = "";
  }
}

async function showPicker() {
  pinTarget = null;
  showStart("pick");
  if (!linkTestId) {
    setKeyedText("#pickEmpty", "noTestId");
    return;
  }
  setKeyedText("#pickErr", null);
  try {
    rosterCache = await api(`/tests/${encodeURIComponent(linkTestId)}/roster`);
  } catch (e) {
    setKeyedText("#pickErr", "rosterLoadErr");
    return;
  }
  renderPickerList();
}

function renderPickerList() {
  if (!rosterCache) return;
  const list = $("#nameList");
  list.innerHTML = "";
  $("#pickEmpty").classList.toggle("hidden", rosterCache.length > 0);
  for (const r of rosterCache) {
    // `expired` (time ran out, no submit) is finished exactly like `submitted`.
    const done = r.status === "submitted" || r.status === "expired";
    const btn = document.createElement("button");
    btn.className = "name-btn" + (done ? " done" : "");
    btn.textContent = r.display_name + (done ? "  " + t("done") : "");
    btn.disabled = done;
    btn.onclick = () => showPinScreen(r);
    list.appendChild(btn);
  }
}

// The PIN replaces the old "are you really <name>?" confirm: only the student
// holding the card can start (or resume) this name's attempt.
function showPinScreen(entry, { resume = false } = {}) {
  pinTarget = entry;
  pinResumeMode = resume;
  pinErr = null;
  showStart("pin");
  const input = $("#pinInput");
  input.value = "";
  renderPinTexts();
  input.focus();
}

function renderPinTexts() {
  if (!pinTarget) return;
  $("#pinHello").textContent = t("pinHello", pinTarget.display_name);
  $("#pinPrompt").textContent = t(pinResumeMode ? "pinResume" : "pinPrompt");
  $("#pinStartBtn").textContent = t(pinResumeMode ? "pinContinue" : "pinStart");
  $("#pinErr").textContent = pinErr ? t(pinErr.key, ...pinErr.args) : "";
}

function setPinError(key, ...args) {
  pinErr = key ? { key, args } : null;
  renderPinTexts();
}

// Upper-case as you type; only letters / digits; 4 characters.
function tidyPinInput() {
  const el = $("#pinInput");
  const clean = el.value.toUpperCase().replace(/[^A-Z0-9]/g, "").slice(0, 4);
  if (clean !== el.value) el.value = clean;
}

async function submitPin(ev) {
  if (ev) ev.preventDefault();
  if (!pinTarget || pinBusy) return;
  tidyPinInput();
  const pin = $("#pinInput").value;
  if (!pin) return setPinError("pinEmpty");
  if (pin.length !== 4) return setPinError("pinShape");
  pinBusy = true;
  $("#pinStartBtn").disabled = true;
  setPinError(null);
  let started;
  try {
    started = await api(`/roster/${encodeURIComponent(pinTarget.roster_entry_id)}/start`, {
      method: "POST",
      body: { pin },
    });
  } catch (e) {
    const hdr = (name) => (e.headers ? e.headers.get(name) : null);
    const win = e.status === 403 ? hdr("X-Exam-Window") : null;
    if (win === "not_yet_open") setPinError("pinWindowNotYet", hdr("X-Exam-Opens-At") || "");
    else if (win === "closed") setPinError("pinWindowClosed");
    else if (e.status === 403) setPinError("pinWrong", Number(hdr("X-Pin-Tries-Left") || 0));
    else if (e.status === 429) setPinError("pinLocked", Math.max(1, Math.ceil(Number(hdr("Retry-After") || 300) / 60)));
    else if (e.status === 400) setPinError("pinShape");
    else if (e.status === 404) setPinError("nameGone");
    else setPinError("startErr");
    $("#pinInput").select();
    return;
  } finally {
    pinBusy = false;
    $("#pinStartBtn").disabled = false;
  }
  curToken = started.token;
  setActive({
    testId: started.test_id,
    attemptId: started.attempt_id,
    rosterEntryId: pinTarget.roster_entry_id,
    name: pinTarget.display_name,
    token: started.token,
  });
  await enterExam(started.attempt_id, started.state);
}

function notMe() {
  setActive(null);
  curToken = null;
  showPicker();
}

// -------------------------------------------------------------------------- //
// Build the navigation model: sections grouped into skill blocks.
// -------------------------------------------------------------------------- //
function skillLabel(skill) {
  return ["reading", "writing", "listening"].includes(skill) ? t(`skill_${skill}`) : t("skill_other");
}

// The recording a section plays: an audio_asset stimulus, or a matching pool's own
// `audio`; null = the part continues the current track.
function sectionAudio(section) {
  const st = section.stimulus;
  if (!st) return null;
  if (st.kind === "audio_asset") return st;
  if (st.kind === "matching_pool" && st.audio && st.audio.asset_id) return st.audio;
  return null;
}

function buildModel(test) {
  units = [];
  blocks = [];
  stopAllAudio(); // never leave an old model's track playing
  tracks = [];
  itemNo = {};
  let cur = null;
  let curTrack = null; // the open listening track of the current block
  let blockSeq = 0; // questions numbered 1..N within each skill block (Reading/Listening)
  for (const section of test.sections) {
    const skill = section.skill || "other";
    if (!cur || cur.skill !== skill) {
      cur = { skill, label: skillLabel(skill), unitIdxs: [] };
      blocks.push(cur);
      blockSeq = 0; // restart numbering for the new block
      curTrack = null; // a track never continues across blocks
    }
    const blockIdx = blocks.length - 1;
    // Per-part audio: a new file starts a track, the same file / no audio continues it.
    // A matching pool may carry its own recording (`audio`, C1/C2 Listening Part 4):
    // exactly like an audio_asset stimulus (same rule as app/delivery/audio.py).
    const aud = sectionAudio(section);
    if (skill === "listening" && aud &&
        (!curTrack || curTrack.asset_id !== aud.asset_id)) {
      curTrack = {
        k: tracks.length,
        blockIdx,
        asset_id: aud.asset_id,
        preview_asset_id: aud.preview_asset_id || null,
        plays: aud.plays || 1,
        locked: aud.locked !== false,
        sectionId: section.id,
        unitIdxs: [],
        el: null,
        pvEl: null,
        used: 0,
        started: false,
        playing: false,
        ended: false,
      };
      tracks.push(curTrack);
    }
    const trackIdx = skill === "listening" && curTrack ? curTrack.k : null;
    units.push({ blockIdx, skill, section, trackIdx });
    cur.unitIdxs.push(units.length - 1);
    if (trackIdx != null) curTrack.unitIdxs.push(units.length - 1);
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
    if (e.status === 401) reauthFromActive();
    return;
  }

  configureLanguage(test.level); // the served test knows its level (YLE -> uk/ru UI)
  buildModel(test);
  idx = 0;
  curAttempt = attemptId;

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

  hideStart();
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
  setActive(null);
  location.search = linkTestId ? `?test=${encodeURIComponent(linkTestId)}` : "";
}

// The stored attempt has no (valid) token any more: ask its student for the PIN.
function reauthFromActive() {
  const a = getActive();
  if (!a || !a.rosterEntryId) return showPicker();
  setActive({ ...a, token: null });
  curToken = null;
  showPinScreen({ roster_entry_id: a.rosterEntryId, display_name: a.name || "" }, { resume: true });
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
  rec = window.ExamRecorder.start({ attemptId, token: sessionToken() });

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
    // The telemetry contract's field kinds are open_writing | gap_fill, so a key
    // word transformation box (a one-line text field too) reports "gap_fill".
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
  if (state) mergeAudioProgress(state.audio_progress, state.audio_progress_seconds);
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
  const text = t(hit.key);
  $("#timeWarnText").textContent = "⏰ " + text;
  $("#timeWarn").className = "time-warn" + (hit.crit ? " crit" : "");
  $("#timeWarnLive").textContent = text; // assertive live region -> read out
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
  $("#progress").textContent = t("partOf", idx + 1, units.length);

  // Listening audio bar: the state of THIS part's track (the <audio> elements
  // themselves live in #audioPool, so navigation never stops a playing track).
  if (block.skill === "listening" && u.trackIdx != null) {
    renderAudioBar(u.trackIdx);
    $("#audioBar").classList.remove("hidden");
    maybeAutoStart(u.trackIdx);
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
// Per-part listening audio.
//
// Tracks (built in buildModel, same rule as app/delivery/audio.py): inside a
// listening block, a part whose audio file differs from the current track starts
// a NEW track; the same file (every older test repeats one long mp3 on each part)
// or a non-audio part continues it. Playback model (Cambridge plays parts in
// order):
//   * tracks play strictly in sequence — a later track cannot start before the
//     current one has ended; reading / answering any part stays possible;
//   * a locked track is never stopped by navigation: moving to another part (back
//     or forward) leaves it playing to the end — no pause / seek / restart;
//   * the next track starts by itself when the student is on (or moves to) one of
//     its parts after the previous track ended in this page AND used all its plays
//     (a track with a sound-check clip, the first track of a block, a resume after
//     a reload, or a previous track with replays left needs the Start button — the
//     student chooses Replay or the next recording; a browser also only plays
//     sound after a gesture);
//   * starting the next track forfeits any replays left on the previous one;
//   * a play the browser stops and will not continue (refused after a pause — iOS
//     screen lock, audio interruption — or a network / decode `error`) shows
//     Resume (continues from where it stopped, same play); a second consecutive
//     load error skips the track ("could not be played — tell your teacher") so
//     the later recordings stay reachable. Each case is noted in telemetry
//     (`audio_play` with `payload.note`) and on the console.
// The <audio> elements live in a hidden pool (#audioPool), never in the re-rendered
// #audioBar (removing a media element from the document pauses it).
//
// Progress (anti-abuse): the furthest point per track lives on the SERVER
// ({asset_id: seconds} in /state, POST /audio-progress {asset_id, seconds}). A
// refresh — even with storage cleared, or on another device — resumes the right
// track there and can never restart it or re-run its sound-check. The legacy
// single value (`audio_progress_seconds`, written only by an old runner) applies
// to the first track.
// -------------------------------------------------------------------------- //
let audioProg = {}; // asset_id -> furthest position known locally (>= server)
let audioProgSent = {}; // asset_id -> furthest position the server confirmed
let legacyAudio = 0; // pre-per-part single-track progress (first track)
let audioPushInflight = false;
let audioRetryTimer = null;

function mergeAudioProgress(map, legacy) {
  for (const [assetId, sec] of Object.entries(map || {})) {
    const s = Number(sec) || 0;
    audioProg[assetId] = Math.max(audioProg[assetId] || 0, s);
    audioProgSent[assetId] = Math.max(audioProgSent[assetId] || 0, s);
  }
  if (typeof legacy === "number") legacyAudio = Math.max(legacyAudio, legacy);
}

function trackPos(tr) {
  const p = audioProg[tr.asset_id] || 0;
  return tr.k === 0 ? Math.max(p, legacyAudio) : p;
}

function reportTrackProgress(tr, sec) {
  const s = Math.floor(sec);
  if (!Number.isFinite(s) || s <= (audioProg[tr.asset_id] || 0)) return;
  audioProg[tr.asset_id] = s; // local furthest point; pushed below (server takes the max)
  pushAudioProgress();
}

// Push every track whose local point is ahead of the server, one request at a
// time; a failed POST is retried (3 s), so a network blip never loses a resume point.
function pushAudioProgress() {
  const id = attemptId();
  if (!id || finished || audioPushInflight) return;
  const assetId = Object.keys(audioProg).find((a) => (audioProgSent[a] || 0) < audioProg[a]);
  if (!assetId) return;
  const s = audioProg[assetId];
  audioPushInflight = true;
  clearTimeout(audioRetryTimer);
  api(`/attempts/${encodeURIComponent(id)}/audio-progress`, {
    method: "POST",
    body: { asset_id: assetId, seconds: s },
  })
    .then((res) => {
      audioPushInflight = false;
      audioProgSent[assetId] = Math.max(audioProgSent[assetId] || 0, s);
      if (res && res.audio_progress) mergeAudioProgress(res.audio_progress);
      pushAudioProgress();
    })
    .catch((e) => {
      audioPushInflight = false;
      if (e.status === 409 || e.status === 404 || e.status === 400) {
        audioProgSent[assetId] = s; // attempt over / gone, or not a track — stop
        pushAudioProgress();
        return;
      }
      console.warn("[exam] audio progress not saved, retrying in 3 s", e);
      audioRetryTimer = setTimeout(pushAudioProgress, 3000);
    });
}

// A later track was started (now or before a reload) => every earlier one is over.
function laterStarted(k) {
  for (let j = k + 1; j < tracks.length; j++) {
    if (tracks[j].started || trackPos(tracks[j]) > 0) return true;
  }
  return false;
}

function trackDone(k) {
  return tracks[k].ended || laterStarted(k);
}

// The track that may play now: the first one not finished (tracks.length = none).
function activeTrack() {
  for (let k = 0; k < tracks.length; k++) if (!trackDone(k)) return k;
  return tracks.length;
}

function partName(unitIdx) {
  const u = units[unitIdx];
  return (u && u.section.title) || t("partN", unitIdx + 1);
}

function audioPool() {
  let pool = document.getElementById("audioPool");
  if (!pool) {
    pool = document.createElement("div");
    pool.id = "audioPool";
    pool.hidden = true;
    document.body.appendChild(pool);
  }
  return pool;
}

// The track's persistent <audio> element (created once, listeners wired once).
function trackEl(tr) {
  if (tr.el) return tr.el;
  const el = document.createElement("audio");
  el.className = "track";
  el.dataset.asset = tr.asset_id;
  el.dataset.track = String(tr.k);
  el.preload = "none";
  el.src = `/assets/${encodeURIComponent(tr.asset_id)}`;
  audioPool().appendChild(el);
  tr.el = el;
  if (rec) rec.attachAudio(el, tr.sectionId);
  // Report the furthest position to the server every ~5 s (it stores the max).
  tr.lastBucket = Math.floor(trackPos(tr) / 5);
  el.addEventListener("timeupdate", () => {
    const pos = el.currentTime;
    if (tr.playing) tr.lastTime = pos; // where an interruption resumes
    if (Math.floor(pos / 5) > tr.lastBucket) {
      tr.lastBucket = Math.floor(pos / 5);
      reportTrackProgress(tr, pos);
    }
  });
  el.addEventListener("playing", () => {
    tr.errors = 0; // "consecutive" load errors: a real playback resets the count
  });
  el.addEventListener("ended", () => {
    reportTrackProgress(tr, el.duration || el.currentTime);
    tr.playing = false;
    tr.interrupted = false;
    tr.ended = true;
    refreshAudioBar();
    // Recordings run in order: the student already on the next track's part hears it.
    autoStartAfter(tr);
  });
  // Network drop / 404 / decode error while this track plays (or is being
  // (re)loaded to play). A failed metadata probe of an idle track is ignored.
  el.addEventListener("error", () => {
    if (tr.playing && !finished) trackLoadError(tr, el.error);
  });
  // Locked: a pause attempt (or background throttling) resumes the single play.
  // If the browser refuses (iOS screen lock, audio interruption) the play is
  // NOT left "playing" forever: the bar offers Resume (from where it stopped).
  if (tr.locked) {
    el.addEventListener("pause", () => {
      if (!tr.playing || el.ended || finished) return;
      const seq = tr.seq;
      el.play().catch((e) => {
        if (seq === tr.seq && tr.playing) trackInterrupted(tr, "play_refused", e);
      });
    });
  }
  return el;
}

// Two consecutive load errors on one track => it is skipped (marked done with a
// visible note) so the later parts' recordings stay reachable.
const TRACK_MAX_ERRORS = 2;

// A telemetry + console note about a recording that stopped / failed (capture only).
function noteAudio(tr, note, err) {
  const position = Math.round((tr.lastTime || 0) * 10) / 10;
  console.warn(`[exam] recording ${tr.asset_id}: ${note} at ${position}s`, err || "");
  if (!rec) return;
  try {
    rec.record("audio_play", {
      item_id: tr.sectionId,
      payload: { position, note, asset_id: tr.asset_id },
    });
    rec.flush(false);
  } catch {
    /* telemetry must never break the runner */
  }
}

// The play in progress stopped and the browser would not continue it: hand it to
// the Resume button (the play is not spent; Resume continues where it stopped).
function trackInterrupted(tr, note, err) {
  let at = tr.lastTime || 0;
  try {
    if (tr.el && !tr.el.error) at = Math.max(at, tr.el.currentTime || 0);
  } catch {
    /* ignore */
  }
  tr.stoppedAt = Math.max(tr.stoppedAt || 0, at);
  tr.playing = false;
  tr.interrupted = true;
  tr.blocked = true;
  noteAudio(tr, note, err);
  refreshAudioBar();
}

// One load / decode error of the current play (counted once per start / resume).
function trackLoadError(tr, err) {
  if (tr.erroredSeq === tr.seq) return;
  tr.erroredSeq = tr.seq;
  tr.errors = (tr.errors || 0) + 1;
  if (tr.errors >= TRACK_MAX_ERRORS) return trackFailed(tr, err);
  trackInterrupted(tr, "load_error", err);
}

// The recording cannot be played: mark it done (no replays) so the order moves on.
function trackFailed(tr, err) {
  tr.playing = false;
  tr.interrupted = false;
  tr.blocked = false;
  tr.failed = true;
  tr.ended = true;
  tr.used = tr.plays;
  noteAudio(tr, "skipped", err);
  refreshAudioBar();
  autoStartAfter(tr);
}

function autoStartAfter(tr) {
  const u = units[idx];
  if (u && u.trackIdx === tr.k + 1) maybeAutoStart(tr.k + 1);
}

function previewEl(tr) {
  if (tr.pvEl) return tr.pvEl;
  const pv = document.createElement("audio");
  pv.className = "preview";
  pv.dataset.asset = tr.preview_asset_id;
  pv.preload = "none";
  pv.src = `/assets/${encodeURIComponent(tr.preview_asset_id)}`;
  audioPool().appendChild(pv);
  pv.addEventListener("ended", () => {
    tr.previewHeard = true;
    refreshAudioBar();
  });
  tr.pvEl = pv;
  return pv;
}

// After a reload the track may already be fully heard: check its length once
// (metadata only) so the next track is not blocked behind a finished one.
function probeFinished(tr) {
  if (tr.probed || tr.started || trackPos(tr) <= 0) return;
  tr.probed = true;
  const el = trackEl(tr);
  const check = () => {
    if (!tr.started && Number.isFinite(el.duration) && trackPos(tr) >= el.duration - 1) {
      tr.ended = true;
      tr.used = tr.plays; // the single play is spent
      refreshAudioBar();
    }
  };
  if (el.readyState >= 1) return check();
  el.preload = "metadata";
  el.addEventListener("loadedmetadata", check, { once: true });
  el.load();
}

// Start (or resume, or replay) track k. Never jumps the order (rule above).
function startTrack(k) {
  const tr = tracks[k];
  if (!tr || tr.playing || finished) return;
  if (tr.interrupted) return resumeTrack(tr);
  const replay = tr.ended && tr.used < tr.plays && !laterStarted(k);
  if (!replay && activeTrack() !== k) return;
  if (tr.used >= tr.plays) return;
  for (let j = 0; j < k; j++) tracks[j].used = tracks[j].plays; // replays forfeited
  const resumePos = tr.used === 0 ? trackPos(tr) : 0; // a replay starts at 0
  tr.used += 1;
  tr.started = true;
  tr.playing = true;
  tr.ended = false;
  tr.lastTime = resumePos;
  tr.stoppedAt = 0;
  const seq = (tr.seq = (tr.seq || 0) + 1);
  if (tr.pvEl) tr.pvEl.pause(); // the sound-check closes once the recording begins
  const el = trackEl(tr);
  const go = () => {
    if (seq !== tr.seq || !tr.playing || finished) return;
    // Resuming: jump forward to the furthest point already heard (never earlier).
    if (resumePos > 0 && el.currentTime < resumePos - 0.5) {
      try {
        el.currentTime = resumePos;
      } catch (e) {
        /* not seekable yet — will retry on the next ready event */
      }
    } else if (replay) {
      el.currentTime = 0;
    }
    el.play().catch((e) => {
      if (seq !== tr.seq || !tr.playing) return; // already handled (error event)
      if (!e || e.name !== "NotAllowedError") return trackLoadError(tr, e);
      // Autoplay refused (no gesture yet): hand the start back to the button.
      tr.playing = false;
      tr.used -= 1;
      tr.started = trackPos(tr) > 0 || tr.used > 0;
      if (replay) tr.ended = true;
      tr.blocked = true;
      refreshAudioBar();
    });
  };
  // preload="none" -> readyState 0; load metadata first so the seek can land,
  // then play from `resumePos` (HTTP Range on /assets makes the forward seek work).
  // An element whose earlier load failed (`el.error`) needs a fresh load() first.
  playWhenReady(el, resumePos, go);
  refreshAudioBar();
}

function playWhenReady(el, at, go) {
  if (at > 0 && (el.error || el.readyState < 1)) {
    el.preload = "metadata";
    el.addEventListener("loadedmetadata", go, { once: true });
    el.load();
  } else {
    if (el.error) el.load();
    go();
  }
}

// Continue an interrupted play (refused after a pause, or a load error) from where
// it stopped. Not a new play: `used` is unchanged and nothing is forfeited. On the
// first play the server's furthest point counts too (never earlier than heard).
function resumeTrack(tr) {
  const el = trackEl(tr);
  const at = Math.max(tr.stoppedAt || 0, tr.used <= 1 ? trackPos(tr) : 0);
  const seq = (tr.seq = (tr.seq || 0) + 1);
  tr.playing = true;
  tr.interrupted = false;
  tr.blocked = false;
  tr.resumed = at > 0;
  tr.lastTime = at;
  const go = () => {
    if (seq !== tr.seq || !tr.playing || finished) return;
    if (at > 0 && Math.abs(el.currentTime - at) > 0.5) {
      try {
        el.currentTime = at;
      } catch {
        /* not seekable yet */
      }
    }
    el.play().catch((e) => {
      if (seq !== tr.seq || !tr.playing) return; // already handled (error event)
      if (e && e.name === "NotAllowedError") trackInterrupted(tr, "play_refused", e);
      else trackLoadError(tr, e);
    });
  };
  playWhenReady(el, at, go);
  refreshAudioBar();
}

// The next track starts by itself only right after the previous one ended in this
// page AND used all its plays (see the model above); otherwise the student presses
// Start.
function maybeAutoStart(k) {
  const tr = tracks[k];
  const prev = tracks[k - 1];
  if (!tr || !prev || prev.blockIdx !== tr.blockIdx || !prev.ended) return;
  // Replays left on the previous recording: the student chooses (Replay, or go on
  // to the next recording) — starting this one by itself would forfeit them.
  if (prev.used < prev.plays) return;
  if (tr.started || tr.blocked || tr.preview_asset_id || trackPos(tr) > 0) return;
  if (activeTrack() !== k) return;
  startTrack(k);
}

function refreshAudioBar() {
  const u = units[idx];
  if (!u || finished || u.trackIdx == null || blocks[u.blockIdx].skill !== "listening") return;
  renderAudioBar(u.trackIdx);
}

// The bar for the part on screen: its own track's state + what plays now.
function renderAudioBar(k) {
  const tr = tracks[k];
  const bar = $("#audioBar");
  const inBlock = tracks.filter((x) => x.blockIdx === tr.blockIdx);
  const nInBlock = inBlock.indexOf(tr) + 1;
  const head = inBlock.length > 1
    ? `<div class="audio-track">${esc(t("trackLabel", nInBlock, inBlock.length))}</div>`
    : "";
  const active = activeTrack();
  const replay = tr.ended && tr.used < tr.plays && !laterStarted(k);
  let state;
  let body;
  if (tr.playing) {
    state = "playing";
    body = `<div class="audio-row">
        <button id="mainBtn" class="primary" type="button" disabled>${esc(t("startTrack"))}</button>
        <span class="muted" id="mainStatus">${esc(t(tr.resumed ? "continuing" : "playing"))}</span>
      </div>`;
  } else if (tr.interrupted) {
    // Stopped mid-play (refused after a pause / load error): Resume continues it.
    state = "interrupted";
    body = `<div class="audio-row">
        <button id="mainBtn" class="primary" type="button">${esc(t("resumeListening"))}</button>
        <span class="muted" id="mainStatus">${esc(t("trackInterrupted"))}</span>
      </div>`;
  } else if (replay) {
    // Replays left: play again, or go on to the next recording (which forfeits them).
    state = "replay";
    const next = tracks[k + 1];
    const goNext = next && next.blockIdx === tr.blockIdx
      ? `<div class="audio-row"><span class="muted">${esc(t("replayOrNext"))}</span>
          <button id="trackNextBtn" type="button">${esc(t("goToNextTrack"))}</button></div>`
      : "";
    body = `<div class="audio-row">
        <button id="mainBtn" class="primary" type="button">${esc(t("replayTrack"))}</button>
        <span class="muted" id="mainStatus">${esc(t("playsLeft", tr.plays - tr.used))}</span>
      </div>${goNext}`;
  } else if (trackDone(k)) {
    state = tr.failed ? "failed" : "done";
    const next = tracks[k + 1];
    const hint = next && next.blockIdx === tr.blockIdx && !next.started && trackPos(next) === 0
      ? ` ${t("trackNextHint")}`
      : "";
    body = `<div class="audio-row"><span id="mainStatus">${esc(t(tr.failed ? "trackFailed" : "trackDone") + hint)}</span></div>`;
  } else if (k === active) {
    state = "ready";
    const resuming = trackPos(tr) > 0;
    const hasPreview = !!tr.preview_asset_id && !resuming && !tr.started;
    const gated = hasPreview && !tr.previewHeard;
    const first = !tracks[k - 1] || tracks[k - 1].blockIdx !== tr.blockIdx;
    const label = resuming ? "resumeListening" : first ? "startListening" : "startTrack";
    const status = gated ? t("previewFirst") : resuming ? t("continuesWhere") : t("plays", tr.plays);
    // audioWarn is escaped like every t() string; its **…** marks become <b> AFTER esc().
    body = `<div class="audio-warn">${escBold(t("audioWarn", { plays: Number(tr.plays), locked: tr.locked, resuming, hasPreview, multi: inBlock.length > 1 }))}</div>
      ${hasPreview
        ? `<div class="audio-row">
             <button id="previewBtn" type="button">${esc(t("soundCheck"))}</button>
             <span class="muted">${esc(t("replayFreely"))}</span>
           </div>`
        : ""}
      <div class="audio-row">
        <button id="mainBtn" class="primary" type="button" ${gated ? "disabled" : ""}>${esc(t(label))}</button>
        <span class="muted" id="mainStatus">${esc(status)}</span>
      </div>`;
    // The previous recording still has replays: offer them here too.
    const prev = tracks[k - 1];
    if (prev && prev.blockIdx === tr.blockIdx && prev.ended && !prev.failed &&
        prev.used < prev.plays && !laterStarted(prev.k)) {
      body += `<div class="audio-row" id="prevReplayRow">
          <span class="muted">${esc(t("prevReplayLeft", partName(prev.unitIdxs[0]), prev.plays - prev.used))}</span>
          <button id="replayPrevBtn" type="button">${esc(t("replayTrack"))}</button></div>`;
    }
    if (resuming) probeFinished(tr);
  } else {
    state = "waiting";
    const cur = tracks[active];
    const curPart = partName(cur.unitIdxs[0]);
    const now = cur.playing ? ` ${t("trackNowPlaying", curPart)}` : "";
    body = `<div class="audio-row"><span id="mainStatus">${esc(t("trackWaits", curPart) + now)}</span>
        <button id="trackGoBtn" type="button">${esc(t("goToPart", curPart))}</button></div>`;
  }
  bar.innerHTML = `<div class="audiobar" data-track="${esc(k)}" data-state="${esc(state)}">${head}${body}</div>`;

  const mainBtn = $("#mainBtn");
  if (mainBtn && !mainBtn.disabled) {
    mainBtn.onclick = () => {
      tr.blocked = false;
      tr.resumed = trackPos(tr) > 0 && tr.used === 0;
      startTrack(k);
    };
  }
  const pvBtn = $("#previewBtn");
  if (pvBtn) {
    pvBtn.onclick = () => {
      const pv = previewEl(tr);
      pv.currentTime = 0;
      pv.play().catch(() => {});
    };
  }
  const prevBtn = $("#replayPrevBtn");
  if (prevBtn) {
    prevBtn.onclick = () => startTrack(k - 1); // a replay: the order rule allows it
  }
  const nextBtn = $("#trackNextBtn");
  if (nextBtn) {
    nextBtn.onclick = () => {
      flushTextSaves();
      idx = tracks[k + 1].unitIdxs[0];
      renderCurrent(attemptId());
      window.scrollTo({ top: 0, behavior: "smooth" });
    };
  }
  const goBtn = $("#trackGoBtn");
  if (goBtn) {
    goBtn.onclick = () => {
      flushTextSaves();
      idx = tracks[active].unitIdxs[0];
      renderCurrent(attemptId());
      window.scrollTo({ top: 0, behavior: "smooth" });
    };
  }
}

function stopAllAudio() {
  for (const tr of tracks) {
    tr.playing = false;
    for (const el of [tr.el, tr.pvEl]) {
      if (!el) continue;
      try {
        el.pause();
      } catch {
        /* ignore */
      }
    }
  }
}

// -------------------------------------------------------------------------- //
// Section body: shared stimulus + every question in the section.
// -------------------------------------------------------------------------- //
function renderSectionBody(attemptId, section) {
  const body = $("#sectionBody");
  const bank = isWordBank(section);
  body.innerHTML =
    stimulusHTML(section, bank) +
    section.items.map((it) => itemHTML(section, it, bank)).join("") +
    (bank ? wordBankHTML(section) : "");
  for (const item of section.items) wireItem(attemptId, section, item);
  wireGapSlots();
  if (bank) wireWordBank(attemptId, section);
  else wbCtl = null;
  if (poolOf(section).length) refreshPoolUse(section);
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
  const base = title ? t("picFor", title) : t("picForPart");
  return n > 1 ? base + t("picOf", i + 1, n) : base;
}

function stimulusHTML(section, bank = false) {
  const st = { ...section.stimulus, __title: section.title || "" };
  if (st.kind === "passage_text" || st.kind === "gapped_text") {
    // Numbered gaps become inline slots mirroring the answers (see passageHTML);
    // a passage without linked gaps renders exactly as before. A blank passage
    // (a part of standalone questions, e.g. key word transformations) shows no box.
    if (!String(st.text || "").trim()) return contextImagesHTML(st);
    return passageHTML(section, st.text) + contextImagesHTML(st);
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
    // question's buttons repeat letter + text. A word-bank part shows its words
    // as the bank of chips instead of the list (wordBankHTML).
    // A pool with its own recording plays it in the #audioBar; its pictures show here.
    const passage =
      (st.text ? passageHTML(section, st.text) : "") +
      (st.audio ? contextImagesHTML({ ...st.audio, __title: st.__title }) : "");
    if (bank) {
      const hint = t("wbHint") + (finePointer() ? t("wbHintDrag") : "");
      return passage + `<p class="wb-intro">${esc(hint)}</p>`;
    }
    const list =
      `<div class="pool-ref"><div class="pool-title">${esc(t("chooseFrom"))}</div>` +
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

function itemHTML(section, item, bank = false) {
  const prior = localValue(item.id);
  const num = itemNo[item.id] || "";
  // Every control is labelled by "Question N" + its prompt (screen readers).
  const qnId = `qn-${esc(item.id)}`;
  const promptId = `pr-${esc(item.id)}`;
  const labelledBy = `aria-labelledby="${qnId} ${promptId}"`;
  const promptImg = item.image
    ? `<img class="prompt-img" src="/assets/${esc(item.image)}" alt="${esc(item.prompt)}" />`
    : "";
  // A question whose gap sits in the passage gets a "↑ In the text" jump back.
  const links = gapLinks(section);
  const gapNo = links ? links.byItem[item.id] : undefined;
  const toGap =
    gapNo !== undefined
      ? `<button type="button" class="to-gap" data-gap-for="${esc(item.id)}" aria-label="${esc(t("toTextAria", gapNo))}">${esc(t("toText"))}</button>`
      : "";
  const head =
    `<div class="qtop"><div class="qnum" id="${qnId}">${num ? `<span class="sr-only">${esc(t("question"))}</span>${esc(num)}` : ""}</div>${toGap}</div>` +
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
          const alt = o.alt || t("optPicture", item.prompt, i + 1);
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
    // Word formation: the stem "(COMPETE)" beside the box; a word limit as a hint.
    const hint = wordLimitHint(item.max_words);
    const hintId = `wl-${esc(item.id)}`;
    const stemId = `st-${esc(item.id)}`;
    const described = [item.stem_word ? stemId : "", hint ? hintId : ""].filter(Boolean).join(" ");
    const input =
      `<input type="text" id="ctl-${esc(item.id)}" ${labelledBy} autocomplete="off" ` +
      `${described ? `aria-describedby="${described}" ` : ""}value="${esc(prior || "")}" />`;
    control = item.stem_word
      ? `<div class="gf-row">${input}${stemChipHTML(item.stem_word, stemId)}</div>`
      : input;
    if (hint) control += `<div class="word-hint" id="${hintId}">${esc(hint)}</div>`;
  } else if (item.item_type === "key_word_transformation") {
    // Lead-in sentence = the prompt above; then the KEY WORD, the second sentence
    // with ONE text box in its gap, the rubric and an informational word count.
    const [before, after] = splitKwt(item.gapped_sentence || "");
    const id = esc(item.id);
    control =
      `<div class="kwt">` +
      `<div class="kwt-key"><span class="sr-only">${esc(t("keyWord"))}: </span>` +
      `<span class="kw-chip" lang="en">${esc(String(item.key_word || "").toUpperCase())}</span></div>` +
      `<p class="kwt-sentence">${esc(before)}<input type="text" class="kwt-input" id="ctl-${id}" ` +
      `${labelledBy} aria-describedby="kwr-${id} kwc-${id}" autocomplete="off" autocapitalize="off" ` +
      `spellcheck="false" value="${esc(prior || "")}" />${esc(after)}</p>` +
      `<div class="kwt-rubric" id="kwr-${id}">${esc(t("kwtRubric", item.min_words, item.max_words))}</div>` +
      `<div class="wordcount" id="kwc-${id}"></div></div>`;
  } else if (item.item_type === "matching" && bank) {
    // Word bank: an answer box the chips are placed into (wireWordBank). The box
    // is display + target only — the value still goes through save() as the key.
    control =
      `<div class="wb-row">` +
      `<button type="button" class="wb-slot" id="slot-${esc(item.id)}" data-item="${esc(item.id)}" ` +
      `aria-pressed="false" aria-describedby="${promptId}"></button>` +
      `<button type="button" class="wb-clear" data-item="${esc(item.id)}" ` +
      `aria-label="${esc(t("clearAnswer", num))}">✕</button></div>` +
      `<div class="once-warn hidden" role="status"></div>`;
  } else if (item.item_type === "matching") {
    const pool = poolOf(section);
    // Letter + text on every button (short pools in a wrapping grid, longer ones
    // as a list; very long texts are clipped — the full text is in the list above).
    const grid = pool.every((p) => isShortOpt(p.text));
    control =
      `<div class="${grid ? "match-grid" : "match-list"}" role="radiogroup" ${labelledBy}>` +
      pool
        .map(
          (p) =>
            `<label class="opt-letter${prior === p.key ? " sel" : ""}" data-key="${esc(p.key)}">` +
            `<input type="radio" name="opt-${esc(item.id)}" value="${esc(p.key)}" ${prior === p.key ? "checked" : ""}/>` +
            `<span class="ol-key">${esc(p.key)}</span><span class="ol-text">${esc(clipText(p.text))}</span>` +
            `<span class="ol-used"></span></label>`
        )
        .join("") +
      `</div><div class="once-warn hidden" role="status"></div>`;
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
      `<button type="button" class="swatch eraser" data-colour="eraser" title="${esc(t("rubber"))}">⌫</button></div>` +
      `<div class="colour-stage" id="stage-${esc(item.id)}">` +
      `<canvas class="colour-paint" id="paint-${esc(item.id)}"></canvas>` +
      `<img class="colour-line" id="line-${esc(item.id)}" src="/assets/${esc(item.asset_id)}" alt="${esc(t("colourAlt", item.prompt))}" />` +
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
        refreshAnswerViews(section, item.id);
      });
    });
  } else if (item.item_type === "gap_fill") {
    const el = byCtl(item.id);
    el.addEventListener("focus", focusItem);
    wireText(attemptId, item.id, el);
    // The inline gap in the passage mirrors what is typed, keystroke by keystroke.
    el.addEventListener("input", () => refreshAnswerViews(section, item.id, el.value));
  } else if (item.item_type === "key_word_transformation") {
    // Same save / focus / paste-block path as gap_fill (the input is `ctl-<id>`).
    const el = byCtl(item.id);
    const wc = document.getElementById(`kwc-${item.id}`);
    const tally = () => {
      const n = countWords(el.value);
      wc.textContent = t("kwtCount", n);
      wc.classList.toggle("ok", n >= item.min_words && n <= item.max_words);
      wc.classList.toggle("over", n > item.max_words);
    };
    tally();
    el.addEventListener("focus", focusItem);
    el.addEventListener("input", tally);
    wireText(attemptId, item.id, el);
  } else if (item.item_type === "matching") {
    // (A word-bank part has no radios here: its boxes are wired by wireWordBank.)
    const radios = document.querySelectorAll(`input[name="opt-${cssId(item.id)}"]`);
    radios.forEach((el) => {
      el.addEventListener("focus", focusItem);
      el.addEventListener("change", () => {
        radios.forEach((r) => {
          const lab = r.closest(".opt-letter");
          if (lab) lab.classList.toggle("sel", r.checked);
        });
        save(attemptId, item.id, el.value);
        refreshAnswerViews(section, item.id);
      });
    });
  } else if (item.item_type === "open_writing") {
    const ta = byCtl(item.id);
    const wc = document.getElementById(`wc-${item.id}`);
    const tally = () => {
      const n = ta.value.trim() ? ta.value.trim().split(/\s+/).length : 0;
      wc.textContent = t("words", n, item.word_min);
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

// -------------------------------------------------------------------------- //
// Answering aids (view only — the answer model and the PUT payloads are the
// same as ever: a matching answer is the option key, gap_fill a string,
// single_choice the displayed index; every change goes through save()):
//   * matching buttons show letter + text, and "→ 3" when that option is
//     already the answer of question 3 in this part (never blocked: Cambridge
//     tasks may reuse options);
//   * a word bank for parts whose options are all short words (YLE "choose the
//     word" parts): tap a box (or it is picked for you), then tap a word chip;
//     desktop can also drag a chip onto a question (HTML5 DnD, fine pointers
//     only — touch uses taps);
//   * numbered gaps in a passage ("(19) ......") become inline slots that
//     mirror the current answer; tapping one jumps to its question. Display
//     only: the question control stays the single input. A passage without
//     recognisable markers renders exactly as before.
// -------------------------------------------------------------------------- //
const OPT_FULL_MAX = 90; // a matching option this long or shorter shows in full on its button
const OPT_CLIP = 56; // longer ones are clipped to about this (the full text is in the list above)
const BANK_MAX_WORDS = 3; // a word bank needs every option to be a short word / phrase
const BANK_MAX_CHARS = 24;
const DND_TYPE = "application/x-exam-option"; // never text/plain: a drop is not a paste
// "(19) ......", "(16) .........", "…(1)…", "(3) ____" — the number is the gap's.
const GAP_RE = /(?:(?:\.{2,}|_{2,}|…+)(?=\())?\((\d{1,3})\)(?:[ \t ]*(?:\.{2,}|_{2,}|…+))?/g;
const GAP_TYPES = new Set(["gap_fill", "single_choice", "matching"]);
const GAP_KINDS = new Set(["gapped_text", "matching_pool", "passage_text"]); // may hold gaps

let wbCtl = null; // the visible word bank's controller {has, activate}
let wbActive = null; // item id whose box the next chip goes into

function scrollBehavior() {
  const reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  return reduce ? "auto" : "smooth";
}

function finePointer() {
  return !!(window.matchMedia && window.matchMedia("(hover: hover) and (pointer: fine)").matches);
}

function poolOf(section) {
  const st = section.stimulus;
  return st && st.kind === "matching_pool" ? st.options || [] : [];
}

function isShortOpt(text) {
  const s = String(text || "").trim();
  return s.length > 0 && s.length <= BANK_MAX_CHARS && s.split(/\s+/).length <= BANK_MAX_WORDS;
}

// A pool whose options fit at most one question each (B2-C2 gapped texts).
function isOncePool(section) {
  return !!(section.stimulus && section.stimulus.kind === "matching_pool" && section.stimulus.each_option_once);
}

// A word bank: three or more matching questions sharing a pool of short words
// (<= 3 words, <= 24 characters each). The chips show every option's FULL text,
// so the bank replaces the "Choose from" list without losing anything.
function isWordBank(section) {
  const pool = poolOf(section);
  return (
    pool.length >= 2 &&
    section.items.length >= 3 &&
    section.items.every((it) => it.item_type === "matching") &&
    pool.every((p) => isShortOpt(p.text))
  );
}

function clipText(text) {
  const s = String(text || "").trim();
  if (s.length <= OPT_FULL_MAX) return s;
  const cut = s.slice(0, OPT_CLIP);
  const sp = cut.lastIndexOf(" ");
  return (sp > OPT_CLIP * 0.6 ? cut.slice(0, sp) : cut).replace(/[\s.,;:!?–—-]+$/, "") + "…";
}

// Key word transformation: split the second sentence at its gap (the server's
// KWT_GAP_PATTERN). No gap found -> the box goes after the sentence.
const KWT_GAP_RE = /_{3,}|\.{3,}|…+/;
function splitKwt(sentence) {
  const m = KWT_GAP_RE.exec(sentence);
  if (!m) return [sentence + " ", ""];
  return [sentence.slice(0, m.index), sentence.slice(m.index + m[0].length)];
}

// A plain whitespace count — informational only (the grader counts contractions
// its own way and the box never blocks typing).
function countWords(s) {
  const v = String(s || "").trim();
  return v ? v.split(/\s+/).length : 0;
}

function wordLimitHint(max) {
  const n = Number(max);
  if (!n) return "";
  return n === 1 ? t("oneWord") : t("maxWords", n);
}

function stemChipHTML(stem, id = "") {
  const idAttr = id ? ` id="${id}"` : "";
  return `<span class="stem-chip" lang="en"${idAttr}>(${esc(String(stem).toUpperCase())})</span>`;
}

function hasValue(v) {
  return v !== undefined && v !== null && String(v).trim() !== "";
}

// What a value LOOKS like to the student (for mirrors / boxes / announcements).
function answerText(section, itemId, v) {
  if (!hasValue(v)) return "";
  const item = section.items.find((it) => it.id === itemId);
  if (!item) return "";
  if (item.item_type === "gap_fill") return String(v);
  if (item.item_type === "single_choice") {
    const o = (item.options || [])[Number(v)];
    if (!o) return "";
    return o.kind === "image" ? String.fromCharCode(65 + Number(v)) : o.text;
  }
  if (item.item_type === "matching") {
    const p = poolOf(section).find((o) => o.key === v);
    return p ? p.text : String(v);
  }
  return "";
}

// What an inline passage slot shows: typed text as is, a short option in full,
// otherwise "C — The trip had…" (letter + a short clip; the full text is in the
// question below), so a sentence / paragraph answer never floods the passage.
const SLOT_CLIP = 28;
function shortClip(text, max) {
  const s = String(text || "").trim();
  if (s.length <= max) return s;
  const cut = s.slice(0, max);
  const sp = cut.lastIndexOf(" ");
  return (sp > max * 0.5 ? cut.slice(0, sp) : cut).replace(/[\s.,;:!?–—-]+$/, "") + "…";
}

function mirrorText(section, itemId, v) {
  const w = answerText(section, itemId, v);
  if (!w) return "";
  const item = section.items.find((it) => it.id === itemId);
  if (!item || item.item_type === "gap_fill" || isShortOpt(w)) return w;
  const key = item.item_type === "matching" ? String(v) : String.fromCharCode(65 + Number(v));
  return `${key} — ${shortClip(w, SLOT_CLIP)}`;
}

// Gap number in the passage -> item id. Tried in order, first that works wins:
//   1. the displayed question numbers (A2 Key / B1 number gaps like the paper:
//      "(19)" is question 19) — every numbered gap must match, else a chance
//      overlap could point a gap at the wrong question;
//   2. "gap N" in the prompts ("Write the word for gap 1."), or a prompt that
//      starts with the gap's number ("51 Who to phone tonight:", "(17)") — again
//      ALL numbered gaps must match (a prompt like "5 people went…" alone must
//      not claim gap 5);
//   3. gaps numbered 1..k inside the part (YLE): the i-th gap = the i-th
//      question (item order inside a passage is never shuffled — rule #7).
//      Never for a plain `passage_text` (a notice may say "(1)" by chance):
//      there only the explicit numbering of 1. / 2. links a gap.
// "(0)" / "(example)" never match a question and stay plain text.
const gapCache = new WeakMap();
function gapLinks(section) {
  if (gapCache.has(section)) return gapCache.get(section);
  const st = section.stimulus || {};
  const text = GAP_KINDS.has(st.kind) ? st.text || "" : "";
  let byNo = new Map();
  if (text) {
    const nums = [...new Set([...text.matchAll(GAP_RE)].map((m) => Number(m[1])))].filter((n) => n > 0);
    // A plain passage (a notice, a note form) only links boxes to write in.
    const items = section.items.filter((it) =>
      st.kind === "passage_text" ? it.item_type === "gap_fill" : GAP_TYPES.has(it.item_type)
    );
    const mapBy = (keyOf) => {
      const m = new Map();
      for (const it of items) {
        const k = keyOf(it);
        if (k !== null && nums.includes(k) && !m.has(k)) m.set(k, it.id);
      }
      return m;
    };
    if (nums.length && items.length) {
      byNo = mapBy((it) => (itemNo[it.id] ? Number(itemNo[it.id]) : null));
      if (byNo.size !== nums.length) {
        byNo = mapBy((it) => {
          const p = it.prompt || "";
          const m = /\bgap\s*(\d{1,3})\b/i.exec(p) || /^\s*\(?(\d{1,3})\)?(?=[\s.:)]|$)/.exec(p);
          return m ? Number(m[1]) : null;
        });
        if (byNo.size !== nums.length) byNo = new Map();
      }
      if (!byNo.size && st.kind !== "passage_text") {
        const seq = [...nums].sort((a, b) => a - b);
        if (seq.every((n, i) => n === i + 1) && seq.length <= items.length) {
          byNo = new Map(seq.map((n) => [n, items[n - 1].id]));
        }
      }
    }
  }
  let res = null;
  if (byNo.size) {
    res = { byNo, byItem: {} };
    for (const [n, id] of byNo) res.byItem[id] = n;
  }
  gapCache.set(section, res);
  return res;
}

// The passage with each linked gap marker replaced by its mirror slot. Text is
// escaped piecewise and never reordered; unlinked markers stay as written. A gap
// number that occurs twice gets ONE slot (its first occurrence) — later ones stay
// text, so every question has exactly one live mirror.
function passageHTML(section, text) {
  const links = gapLinks(section);
  if (!links) return `<div class="stimulus">${esc(text)}</div>`;
  let out = "";
  let last = 0;
  const seen = new Set();
  for (const m of text.matchAll(GAP_RE)) {
    const id = links.byNo.get(Number(m[1]));
    if (!id || seen.has(id)) continue;
    seen.add(id);
    out += esc(text.slice(last, m.index)) + gapSlotHTML(section, id, m[1]);
    last = m.index + m[0].length;
  }
  return `<div class="stimulus gapped">${out}${esc(text.slice(last))}</div>`;
}

function gapSlotHTML(section, itemId, n) {
  const w = mirrorText(section, itemId, localValue(itemId));
  const item = section.items.find((it) => it.id === itemId);
  const stem = item && item.stem_word ? stemChipHTML(item.stem_word) : "";
  return (
    `<button type="button" class="gap-slot${w ? " filled" : ""}" data-gap-item="${esc(itemId)}" ` +
    `data-gap-no="${esc(n)}" aria-label="${esc(t("gapGo", n, w))}">` +
    `<span class="gap-no" aria-hidden="true">${esc(n)}</span><span class="gap-val">${esc(w)}</span>` +
    `${stem}</button>`
  );
}

function wireGapSlots() {
  const body = $("#sectionBody");
  body.querySelectorAll(".gap-slot").forEach((b) => {
    b.onclick = () => focusQuestion(b.dataset.gapItem);
  });
  body.querySelectorAll(".to-gap").forEach((b) => {
    b.onclick = () => {
      const slot = body.querySelector(`.gap-slot[data-gap-item="${cssId(b.dataset.gapFor)}"]`);
      if (!slot) return;
      slot.scrollIntoView({ behavior: scrollBehavior(), block: "center" });
      slot.focus({ preventScroll: true });
      slot.classList.add("flash");
      setTimeout(() => slot.classList.remove("flash"), 1200);
    };
  });
}

// After a change to `itemId` (value = what the control holds right now, for
// typed text before its debounced save): update its gap mirror + pool badges.
function refreshAnswerViews(section, itemId, value) {
  const v = value !== undefined ? value : localValue(itemId);
  const slot = document.querySelector(`#sectionBody .gap-slot[data-gap-item="${cssId(itemId)}"]`);
  if (slot) {
    const w = mirrorText(section, itemId, v);
    slot.querySelector(".gap-val").textContent = w;
    slot.classList.toggle("filled", !!w);
    slot.setAttribute("aria-label", t("gapGo", slot.dataset.gapNo, w));
  }
  if (poolOf(section).length) refreshPoolUse(section);
}

// option key -> item ids (question order) currently holding it, in this part.
function poolUse(section) {
  const use = {};
  for (const it of section.items) {
    if (it.item_type !== "matching") continue;
    const v = localValue(it.id);
    if (hasValue(v)) (use[v] = use[v] || []).push(it.id);
  }
  return use;
}

function usedBadgeHTML(ids) {
  if (!ids.length) return "";
  const ns = ids.map((id) => itemNo[id] || "").join(", ");
  return `<span aria-hidden="true">→ ${esc(ns)}</span><span class="sr-only">, ${esc(t("usedIn", ns))}</span>`;
}

// Re-draw every pool-derived bit of the visible part: "used" badges on the
// letter buttons; for a word bank also the boxes, chips and the target line.
function refreshPoolUse(section) {
  const body = $("#sectionBody");
  const use = poolUse(section);
  const once = isOncePool(section);
  for (const it of section.items) {
    if (it.item_type !== "matching") continue;
    const card = document.getElementById("q-" + it.id);
    if (!card) continue;
    card.querySelectorAll(".opt-letter").forEach((lab) => {
      const others = (use[lab.dataset.key] || []).filter((id) => id !== it.id);
      lab.classList.toggle("used", others.length > 0);
      lab.classList.toggle("once", once && others.length > 0);
      const badge = lab.querySelector(".ol-used");
      if (badge) badge.innerHTML = usedBadgeHTML(others);
    });
    // each_option_once (a gapped text): a gentle note when this answer is also
    // another question's — never blocking, the student decides which to change.
    const warn = card.querySelector(".once-warn");
    if (warn) {
      const v = localValue(it.id);
      const clash = once && hasValue(v) ? (use[v] || []).filter((id) => id !== it.id) : [];
      const text = clash.length
        ? t("onceWarn", v, clash.map((id) => itemNo[id] || "").join(", "))
        : "";
      if (warn.textContent !== text) warn.textContent = text;
      warn.classList.toggle("hidden", !text);
    }
    const slot = card.querySelector(".wb-slot");
    if (slot) {
      const w = answerText(section, it.id, localValue(it.id));
      const num = itemNo[it.id] || "";
      slot.innerHTML = w ? esc(w) : `<span class="wb-ph">${esc(t("wbEmpty"))}</span>`;
      slot.classList.toggle("filled", !!w);
      slot.classList.toggle("active", it.id === wbActive);
      slot.setAttribute("aria-pressed", String(it.id === wbActive));
      slot.setAttribute("aria-label", t("wbSlot", num, w));
      card.classList.toggle("wb-active", it.id === wbActive);
      const clr = card.querySelector(".wb-clear");
      if (clr) clr.classList.toggle("hidden", !w);
    }
  }
  const bank = body.querySelector(".wb-bank");
  if (!bank) return;
  const activeVal = wbActive ? localValue(wbActive) : undefined;
  bank.querySelectorAll(".wb-chip").forEach((chip) => {
    const ids = use[chip.dataset.key] || [];
    chip.classList.toggle("used", ids.length > 0);
    chip.classList.toggle("once", once && ids.length > 0);
    chip.setAttribute("aria-pressed", String(!!wbActive && activeVal === chip.dataset.key));
    chip.querySelector(".wb-used").innerHTML = usedBadgeHTML(ids);
  });
  const target = bank.querySelector(".wb-target");
  if (target && !target.classList.contains("nudge")) {
    target.textContent = wbActive
      ? t("wbActive", itemNo[wbActive] || "")
      : section.items.every((it) => hasValue(localValue(it.id)))
        ? t("wbAllDone")
        : t("wbPickBox");
  }
}

function wordBankHTML(section) {
  const drag = finePointer();
  const pool = poolOf(section);
  const chips = pool
    .map(
      (p) =>
        `<button type="button" class="wb-chip" data-key="${esc(p.key)}" aria-pressed="false"` +
        `${drag ? ' draggable="true"' : ""}><span class="wb-key">${esc(p.key)}</span>` +
        `<span class="wb-word">${esc(p.text)}</span><span class="wb-used"></span></button>`
    )
    .join("");
  return (
    // Many words: on a phone they become two rows in A..Z order (row 1 = first
    // half) that scroll sideways together (`--wb-cols`, see exam.html).
    `<div class="wb-bank${pool.length > 6 ? " wb-many" : ""}" style="--wb-cols:${Math.ceil(pool.length / 2)}" ` +
    `role="group" aria-label="${esc(t("wbBankLabel"))}">` +
    `<div class="wb-target" aria-hidden="true"></div><div class="wb-chips">${chips}</div></div>`
  );
}

// Polite screen-reader note ("“jam” is now in question 3.").
function announce(msg) {
  const el = $("#answerLive");
  if (!el) return;
  el.textContent = "";
  setTimeout(() => {
    el.textContent = msg;
  }, 30);
}

// Scroll just enough that `el` is visible between the sticky header and the
// sticky word bank (the next box after a placement).
function ensureVisible(el) {
  if (!el) return;
  const r = el.getBoundingClientRect();
  const bank = document.querySelector("#sectionBody .wb-bank");
  const bottom = Math.min(window.innerHeight, bank ? bank.getBoundingClientRect().top : Infinity) - 12;
  const header = $("#examHeader");
  const top = (header ? header.getBoundingClientRect().bottom : 0) + 12;
  let dy = 0;
  if (r.top < top || r.height > bottom - top) dy = r.top - top;
  else if (r.bottom > bottom) dy = r.bottom - bottom;
  if (dy) window.scrollBy({ top: dy, behavior: scrollBehavior() });
}

function wireWordBank(attemptId, section) {
  const body = $("#sectionBody");
  const ids = section.items.map((it) => it.id);
  const filled = (id) => hasValue(localValue(id));
  const nextEmpty = (after) => {
    const start = after ? ids.indexOf(after) + 1 : 0;
    for (let k = 0; k < ids.length; k += 1) {
      const id = ids[(start + k) % ids.length];
      if (id !== after && !filled(id)) return id;
    }
    return null;
  };
  if (!ids.includes(wbActive)) wbActive = nextEmpty(null);
  const target = body.querySelector(".wb-target");
  let lastPlaced = null; // Escape in the bank returns here when no box is picked

  const activate = (id) => {
    wbActive = id;
    if (target) target.classList.remove("nudge");
    if (id && rec) rec.setItem(id);
    refreshPoolUse(section);
  };

  // Put `key` into `id`'s box ("" clears it) — the same save path as a radio.
  const place = (id, key, { advance = true } = {}) => {
    if (rec) rec.setItem(id);
    save(attemptId, id, key);
    lastPlaced = id;
    const num = itemNo[id] || "";
    let msg = key ? t("wbPlaced", answerText(section, id, key), num) : t("wbCleared", num);
    if (key && advance) {
      wbActive = nextEmpty(id);
      if (wbActive) {
        msg += " " + t("wbActive", itemNo[wbActive] || "");
        ensureVisible(document.getElementById("q-" + wbActive));
      }
    } else {
      wbActive = id;
    }
    if (target) target.classList.remove("nudge");
    refreshAnswerViews(section, id);
    announce(msg);
  };

  body.querySelectorAll(".wb-slot").forEach((slot) => {
    const id = slot.dataset.item;
    slot.addEventListener("focus", () => {
      if (rec) rec.setItem(id);
    });
    slot.addEventListener("click", (e) => {
      activate(id);
      announce(t("wbActive", itemNo[id] || ""));
      // Keyboard (Enter / Space -> click with detail 0): continue in the bank.
      if (e.detail === 0) {
        const chips = [...body.querySelectorAll(".wb-chip")];
        const cur = chips.find((c) => c.dataset.key === localValue(id)) || chips[0];
        if (cur) cur.focus();
      }
    });
  });
  body.querySelectorAll(".wb-clear").forEach((btn) => {
    btn.addEventListener("click", () => place(btn.dataset.item, "", { advance: false }));
  });
  // Tapping anywhere on a question card (its sentence / picture) picks its box.
  for (const id of ids) {
    const card = document.getElementById("q-" + id);
    if (!card) continue;
    card.addEventListener("click", (e) => {
      if (e.target.closest("button")) return;
      activate(id);
    });
    // Desktop drag-and-drop: drop a chip anywhere on the question.
    card.addEventListener("dragover", (e) => {
      if (!e.dataTransfer || ![...e.dataTransfer.types].includes(DND_TYPE)) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = "copy";
      card.classList.add("drop-over");
    });
    card.addEventListener("dragleave", (e) => {
      if (!card.contains(e.relatedTarget)) card.classList.remove("drop-over");
    });
    card.addEventListener("drop", (e) => {
      card.classList.remove("drop-over");
      const key = e.dataTransfer ? e.dataTransfer.getData(DND_TYPE) : "";
      if (!key) return;
      e.preventDefault();
      place(id, key);
    });
  }
  body.querySelectorAll(".wb-chip").forEach((chip) => {
    const key = chip.dataset.key;
    chip.addEventListener("click", () => {
      if (!wbActive) {
        if (target) {
          target.textContent = t("wbPickBox");
          target.classList.add("nudge");
        }
        announce(t("wbPickBox"));
        return;
      }
      // Tapping the word already in the chosen box takes it out again.
      if (localValue(wbActive) === key) place(wbActive, "", { advance: false });
      else place(wbActive, key);
    });
    chip.addEventListener("keydown", (e) => {
      if (e.key !== "Escape") return;
      const back = wbActive || lastPlaced;
      const slot = back && document.getElementById("slot-" + back);
      if (slot) slot.focus();
    });
    chip.addEventListener("dragstart", (e) => {
      if (!e.dataTransfer) return;
      e.dataTransfer.setData(DND_TYPE, key);
      e.dataTransfer.effectAllowed = "copy";
      chip.classList.add("dragging");
    });
    chip.addEventListener("dragend", () => {
      chip.classList.remove("dragging");
      body.querySelectorAll(".drop-over").forEach((c) => c.classList.remove("drop-over"));
    });
  });

  wbCtl = { has: (id) => ids.includes(id), activate };
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
  let html = `<div class="navtitle">${esc(t("navTitle"))}</div>`;
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
    `<div class="navlegend"><span><span class="legend-box done"></span>${esc(t("answered"))}</span>` +
    `<span><span class="legend-box"></span>${esc(t("notAnswered"))}</span></div>` +
    `<div class="navreview"><button type="button" id="navReviewBtn">${esc(t("reviewSubmit"))}</button></div>`;
  nav.innerHTML = html;
  nav.querySelectorAll(".navsq").forEach((b) => {
    b.onclick = () => jumpTo(Number(b.dataset.unit), b.dataset.item);
  });
  $("#navReviewBtn").onclick = showReview;
}

function navState(itemId) {
  return itemId in rejected && !(itemId in pending)
    ? t("answerRejected")
    : isAnswered(itemId)
      ? t("answered")
      : t("notAnswered");
}

function navLabel(itemId) {
  return t("navLabel", itemNo[itemId] || "", navState(itemId));
}

// Show the part holding `itemId` and bring the question into view + focus.
function jumpTo(unit, itemId, { focus = false } = {}) {
  if (reviewOpen) hideReview();
  if (unit !== idx || !document.getElementById("q-" + itemId)) {
    flushTextSaves();
    idx = unit;
    renderCurrent(attemptId());
  }
  focusQuestion(itemId, { focus });
}

// Bring a question card into view (+ flash) and optionally focus its control:
// the chosen radio (else the first), the text field, or the word-bank box.
function focusQuestion(itemId, { focus = true } = {}) {
  const el = document.getElementById("q-" + itemId);
  if (!el) return;
  el.scrollIntoView({ behavior: scrollBehavior(), block: "center" });
  el.classList.add("flash");
  setTimeout(() => el.classList.remove("flash"), 1200);
  if (!focus) return;
  if (wbCtl && wbCtl.has(itemId)) {
    wbCtl.activate(itemId);
    const slot = document.getElementById("slot-" + itemId);
    if (slot) slot.focus({ preventScroll: true });
    return;
  }
  const ctl =
    el.querySelector("input[type=radio]:checked") ||
    el.querySelector("input:not([type=hidden]), textarea, button:not(.to-gap), canvas");
  if (ctl) ctl.focus({ preventScroll: true });
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
    const title = `${block.label} · ${u.section.title || t("partN", ui + 1)}`;
    const squares = items
      .map((it) => {
        const bad = it.id in rejected && !(it.id in pending);
        const cls = bad ? "bad" : isAnswered(it.id) ? "done" : "todo";
        const saving = it.id in pending ? t("savingSuffix") : "";
        return (
          `<button type="button" class="review-q ${cls}" data-unit="${ui}" data-item="${esc(it.id)}" ` +
          `aria-label="${esc(t("goToState", itemNo[it.id] || "", navState(it.id)) + saving)}">` +
          `${esc(itemNo[it.id] || "")}</button>`
        );
      })
      .join("");
    const links = missing.length
      ? `<div class="review-todo">${esc(t("notAnsweredList"))}` +
        missing
          .map(
            (it) =>
              `<button type="button" class="review-go" data-unit="${ui}" data-item="${esc(it.id)}">${esc(t("goTo", itemNo[it.id] || ""))}</button>`
          )
          .join("") +
        `</div>`
      : "";
    html +=
      `<div class="review-part" data-unit="${ui}">` +
      `<div class="review-part-head"><span class="title">${esc(title)}</span>` +
      `<span class="count${missing.length ? " warn" : ""}">${esc(t("partCount", items.length - missing.length, items.length))}</span></div>` +
      `<div class="navgrid">${squares}</div>${links}</div>`;
  });
  const rebuilt = setReviewParts(html);
  const count = $("#reviewCount");
  count.textContent = todo ? t("reviewCount", total - todo, total, todo) : t("allAnswered", total);
  count.className = "review-count" + (todo ? " warn" : "");
  count.dataset.unanswered = String(todo);

  const save = $("#reviewSave");
  const lines = [];
  if (unsavedIds.length) {
    lines.push(t("unsavedLine", unsavedIds.length, questionNumbers(unsavedIds), !!saveFailures));
  }
  if (rejectedList.length) {
    lines.push(t("rejectedLine", rejectedList.length, questionNumbers(rejectedList)));
  }
  save.textContent = lines.length ? lines.join(" ") : t("allSaved");
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
    parts.push(t("saved"));
  } else if (saveFailures === 0 && !saveHalted) {
    parts.push(t("savingNow"));
    cls = "busy";
  } else {
    parts.push(t("notSavedRetry", n));
    cls = "warn";
  }
  if (r > 0) {
    parts.push(t("rejectedCount", r));
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
    lines.push(t("sumUnsaved", unsaved.length, questionNumbers(unsaved)));
  }
  if (rejectedList.length) {
    lines.push(t("sumRejected", rejectedList.length, questionNumbers(rejectedList)));
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
  if (unanswered > 0 && !confirm(t("confirmUnanswered", unanswered))) {
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
          (unsaved.length ? t("confirmUnsavedNet") : t("confirmRejected"))
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
        $("#reviewErr").textContent = t("submitFailed");
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
  setActive(null);
  curToken = null;
  try {
    localStorage.removeItem(pendingKey(attemptId() || curAttempt));
  } catch (e) {
    /* ignore */
  }
  if (rec) rec.stop();
  stopAllAudio();
  reviewOpen = false;
  hideStart();
  $("#runner").classList.add("hidden");
  $("#navigator").classList.add("hidden");
  $("#review").classList.add("hidden");
  $("#timeWarn").classList.add("hidden");
  $("#examHeader").classList.add("hidden");
  $("#done").classList.remove("hidden");
  // Keyed so a later language switch cannot happen here, but [data-i18n] keeps the
  // markup honest: each line names the string it shows.
  if (!submitted) {
    $("#doneIcon").textContent = "⏱";
    setKeyedText("#doneTitle", "timeUp");
    setKeyedText("#doneMsg", "timeUpMsg");
  } else if (opts.already) {
    setKeyedText("#doneTitle", "already");
    setKeyedText("#doneMsg", "alreadyMsg");
  }
  const unsaved = opts.unsaved || [];
  const rejectedList = opts.rejected || [];
  if (unsaved.length || rejectedList.length) {
    $("#doneUnsaved").textContent =
      "⚠ " + unsavedSummary(unsaved, rejectedList).replace("\n", " ") + " " + t("tellTeacher");
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
$("#pinForm").addEventListener("submit", submitPin);
$("#pinInput").addEventListener("input", tidyPinInput);
$("#pinBackBtn").onclick = notMe;
document.querySelectorAll("#langSwitch button").forEach((b) => {
  b.onclick = () => setLang(b.dataset.lang);
});

async function boot() {
  const active = getActive();
  const resumable = active && (!linkTestId || active.testId === linkTestId);
  // The test's level decides the UI language (YLE -> EN / УКР / РУС switch).
  const infoId = linkTestId || (resumable ? active.testId : null);
  let level = null;
  let info = null;
  if (infoId) {
    try {
      info = await api(`/tests/${encodeURIComponent(infoId)}/info`);
      level = info.level;
    } catch (e) {
      /* unknown test / offline — English, and the picker reports the problem */
    }
  }
  configureLanguage(level);
  applyTestInfo(info);
  if (resumable) {
    if (!active.token) return reauthFromActive(); // no session on this device: PIN
    try {
      const state = await api(`/attempts/${encodeURIComponent(active.attemptId)}/state`, {
        token: active.token,
      });
      curToken = active.token;
      return enterExam(active.attemptId, state);
    } catch (e) {
      if (e.status === 401) return reauthFromActive(); // token no longer valid: PIN
      setActive(null); // dangling — fall back to the picker
    }
  }
  showPicker();
}

boot();
