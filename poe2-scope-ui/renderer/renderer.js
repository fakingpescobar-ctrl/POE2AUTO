// poe2-scope-ui / renderer.js
// Логика wizard-шагов: запуск команд poe2_scope, лог, инлайн-подтверждения, отчёты.
"use strict";

/* ── Состояние ─────────────────────────────────────────────────────── */
const state = {
  python: localStorage.getItem("poe2scope.python") || "",
  running: false,
  step: "check",              // активный шаг в панели
  checkDone: null,            // true/false — вердикт check
  findings: null,             // отчёт --find (json)
  promptPending: false,
  stageIdx: 0,                // текущая стадия шкалы
};

/* ── Шаги: конфиг команд и подсказок ────────────────────────────────── */
const STEPS = {
  check: {
    title: "Шаг 1 · Проверить цепочку",
    desc: "Резолвит GSO → GameState → InGameState → Area → Player и прогоняет валидаторы V1-V6. " +
          "Если score/verdict зелёные — патча нет, останавливайся.",
    btn: "Запустить проверку",
    args: ["--check"],
    need: () => true,
  },
  find: {
    title: "Шаг 2 · Найти оффсеты",
    desc: "Перебирает кандидатов (vec + 12×States), ищет игрока/камеру/дерево/зону. " +
          "Результат — новые оффсеты в отчёте. Без --force запустится только если check сломан.",
    btn: "Найти новые оффсеты",
    args: () => (document.getElementById("tog-force").checked ? ["--find", "--force", "--dump-md"] : ["--find", "--dump-md"]),
    // строго после шага 1: check не запускался → заблокирован;
    // check зелёный → заблокирован до включения --force; check сломан → доступен сам
    need: () => state.checkDone !== null && (state.checkDone === false || document.getElementById("tog-force").checked),
    lockedBtn: "⛔ Сначала выполни шаг 1 — Проверить цепочку",
    advice: "Check показал поломку — ищем новые оффсеты.",
  },
  verify: {
    title: "Шаг 3 · Проверить легаси",
    desc: "Прогоняет offset_scanner и mem_detect (ctypes-путь). OK — фолбэк жив, bot может падать только по C#-ридеру. " +
          "BROKEN после патча — нормально до шага 4.",
    btn: "Проверить легаси",
    args: ["--verify", "--dump-md"],
    need: () => state.checkDone !== null,
    lockedBtn: "⛔ Сначала выполни шаг 1 (check)",
    advice: "Сверь ctypes-путь с новыми оффсетами.",
  },
  apply: {
    title: "Шаг 4 · Применить",
    desc: "Записывает найденные оффсеты в game_offsets.py / Program.cs / легаси, затем пересобирает DLL. " +
          "Спросит подтверждение перед каждой правкой — отвечай кнопками ниже. Перед сборкой лучше закрыть бота (DLL занята).",
    btn: "Применить и собрать DLL",
    args: ["--apply", "--dump-md"],
    need: () => !!state.findings,
    lockedBtn: "⛔ Сначала найди оффсеты (шаг 2)",
    advice: "Найдены кандидаты — применяем.",
  },
  tests: {
    title: "Прогон юнит-тестов",
    desc: "pytest по модулям бота (без интеграционных — им нужна живая игра). " +
          "Показывает здоровье entity_reader / mem_detect / HP / лутера и т.д. " +
          "АВТО-ЧИНКА гоняет их сама — до и после чинки.",
    btn: "Запустить юнит-тесты",
    args: ["--tests", "--dump-md"],
    need: () => true,
  },
};

const STEP_KEYS = ["check", "find", "verify", "apply"];

/* ── Стадии для сегментной шкалы (по шагу) ──────────────────────────── */
const STAGES = {
  check: ["Подключение к процессу", "Резолв GameState (AOB)", "Резолв InGameState", "Чтение AreaInstance", "Игрок", "Камера", "Дерево сущностей", "Валидаторы V1–V6", "Формирование отчёта"],
  find: ["Подключение к процессу", "AOB-паттерн и слоты", "Кандидаты States[0..11]", "AreaInstance", "Игрок → offset", "Камера → offset", "Awake-дерево → offset", "Area-инфо → offset", "Отчёт + score"],
  verify: ["offset_scanner (AOB chain)", "Валидация цепочки", "mem_detect", "Игровое состояние", "Vitals", "Подсчёт вердикта", "Отчёт"],
  apply: ["Загрузка findings", "game_offsets.py", "Program.cs", "offset_scanner — легаси", "mem_detect — легаси", "entity_reader — легаси", "scope.json", "Сборка DLL", "Отчёт"],
  tests: ["pytest: сбор набора", "entity_reader / mem_detect", "оффсеты / core_map", "HP / фласки / лутер", "Сводка"],
};

// маркеры из вывода python → стадия (индекс)
const STAGE_MARKERS = [
  [/pid=|OpenProcess|подключ|connect/i, 0],
  [/aob|pattern|slot|chain/i, 1],
  [/states|igs|InGameState|GameState/i, 2],
  [/area(?!\w)/i, 3],
  [/player|игрок/i, 4],
  [/camera|камер/i, 5],
  [/entit|сущн|awake/i, 6],
  [/V[1-6]\b|validator|score/i, 7],
  [/отчёт|report|dump/i, 8],
];

let stagesTimer = null;

/* ── Сегментная шкала прогресса ─────────────────────────────────────── */
function initStages() {
  const names = STAGES[state.step] || [];
  const bar = $("stages-bar");
  bar.innerHTML = "";
  for (let i = 0; i < names.length; i++) {
    const s = document.createElement("div");
    s.className = "seg";
    s.title = names[i];
    bar.appendChild(s);
  }
  state.stageIdx = 0;
  $("stages").hidden = false;
  renderStages();
  if (stagesTimer) clearInterval(stagesTimer);
  stagesTimer = setInterval(advanceStage, 1600);
}

function renderStages() {
  const segs = document.querySelectorAll(".seg");
  const names = STAGES[state.step] || [];
  const idx = state.stageIdx;
  segs.forEach((s, i) => {
    s.classList.toggle("on", i <= idx);
    s.classList.toggle("current", i === idx);
  });
  const label = $("stages-label-text");
  if (names.length && idx >= names.length - 1) label.textContent = "Выполняется…";
  else label.textContent = names[idx] || "Выполняется…";
  $("stages-info").textContent = `${Math.min(idx + 1, names.length)}/${names.length}`;
}

function advanceStage() {
  const n = (STAGES[state.step] || []).length;
  if (state.stageIdx < n - 1) { state.stageIdx++; renderStages(); }
  else renderStages(); // последний сегмент продолжает пульсировать
}

// прыжок вперёд по реальным маркерам вывода python
function jumpStageFromText(text) {
  let target = -1;
  for (const [re, i] of STAGE_MARKERS) {
    if (re.test(text)) target = Math.max(target, i);
  }
  const n = (STAGES[state.step] || []).length;
  if (target >= 0 && target > state.stageIdx) {
    state.stageIdx = Math.min(target, n - 1);
    renderStages();
  }
}

function finalizeStages(ok, stopped) {
  if (stagesTimer) { clearInterval(stagesTimer); stagesTimer = null; }
  const segs = document.querySelectorAll(".seg");
  const n = segs.length;
  segs.forEach((s, i) => {
    s.classList.remove("current");
    if (ok && !stopped) {
      // успех: ВСЕ сегменты зелёные, счётчик N/N
      s.classList.remove("on", "bad");
      s.classList.add("done");
    } else if (stopped) {
      // прервано: достигнутые янтарные, дальше серые
      if (i > state.stageIdx) { s.classList.remove("on", "done", "bad"); }
    } else {
      // ошибка: достигнутые янтарные, дальше красные
      if (i > state.stageIdx) { s.classList.remove("on", "done"); s.classList.add("bad"); }
    }
  });
  const label = $("stages-label-text");
  const info = $("stages-info");
  if (stopped) { label.textContent = "⏹ Прервано"; info.textContent = `${Math.min(state.stageIdx + 1, n)}/${n}`; }
  else if (ok) { label.textContent = "✓ Завершено"; info.textContent = `${n}/${n}`; }
  else { label.textContent = "✕ Ошибка"; info.textContent = `${Math.min(state.stageIdx + 1, n)}/${n}`; }
}

/* ── DOM-хелперы ────────────────────────────────────────────────────── */
const $ = (id) => document.getElementById(id);
const term = $("term");

function log(text, cls) {
  if (state.running || cls) {
    const span = document.createElement("span");
    span.className = cls || "";
    span.textContent = text + "\n";
    term.appendChild(span);
    term.scrollTop = term.scrollHeight;
  }
}

/* ── ANSI-очистка ───────────────────────────────────────────────────── */
function stripAnsi(s) {
  return s.replace(/\x1b\[[0-9;]*m/g, "").replace(/\x1b\[[0-9;]*[A-Za-z]/g, "");
}

/* ── Статус шагов в левой панели ────────────────────────────────────── */
function setStepState(key, cls) {
  const el = $(`step-${key}`);
  el.classList.remove("done", "error", "active");
  if (cls) el.classList.add(cls);
  const st = $(`st-${key}`);
  if (cls === "done") st.textContent = "✓";
  else if (cls === "error") st.textContent = "✕";
  else st.textContent = "○";
}

function setStepLock(key, locked) {
  // шаг всегда кликабелен (можно открыть карточку и посмотреть, почему недоступен),
  // но визуально показываем замок/приглушение
  const el = $(`step-${key}`);
  el.disabled = false; // страховка: снимаем нативный disabled из разметки
  el.classList.toggle("locked", locked);
}

function refreshSteps() {
  const cur = STEPS[state.step];
  const ok = cur ? cur.need() : true;

  // активный шаг
  STEP_KEYS.forEach((k) => $(`step-${k}`).classList.toggle("active", k === state.step));

  // доступность
  STEP_KEYS.forEach((k) => setStepLock(k, !STEPS[k].need()));

  // подсказка снизу
  const adv = !ok
    ? (cur.lockedAdvice || "Сначала выполни предыдущий шаг — порядок сверху вниз.")
    : (cur.advice || "Нажми кнопку, чтобы выполнить шаг.");
  $("advice-text").textContent = adv;

  // кнопка действия: недоступна — показываем, как разблокировать
  const btn = $("btn-run");
  $("btn-run-text").textContent = ok ? cur.btn : (cur.lockedBtn || "⛔ Сначала предыдущий шаг");
  btn.disabled = !ok || state.running;

  // чекбокс --force виден на шаге 1 (удобно поставить заранее) и на шаге 2
  const fw = $("force-row");
  fw.hidden = !(state.step === "check" || state.step === "find");
  const tog = $("tog-force");
  tog.parentElement.classList.toggle("warn-on", state.step === "find" && tog.checked);
  if (fw.hidden) $("help-pop").hidden = true; // прячем подсказку вместе с чекбоксом

  // заголовки карточки
  $("action-title").textContent = cur.title;
  $("action-desc").textContent = cur.desc;
}

/* ── Статус игры в шапке ────────────────────────────────────────────── */
function updateGameChip(v, extra) { // v: null(нет проверки) | checking | ok | bad
  const dot = $("chip-game").querySelector(".chip-dot");
  const txt = $("chip-game-text");
  if (v === "ok") { dot.className = "chip-dot good"; txt.textContent = extra || "игра: проверена — цепочка жива"; }
  else if (v === "bad") { dot.className = "chip-dot bad"; txt.textContent = extra || "игра: поломка — найди оффсеты (шаг 2)"; }
  else if (v === "checking") { dot.className = "chip-dot dim"; txt.textContent = "игра: проверяется…"; }
  else { dot.className = "chip-dot dim"; txt.textContent = "игра: не проверялась"; }
}

/* ── Парсинг значимых строк из вывода ───────────────────────────────── */
function onData(p) {
  const text = stripAnsi(p.text || "");
  if (!text) return;

  log(text, p.stream === "err" ? "err-line" : "");

  // маркеры вывода scope_main.py (ищем в последней порции)
  if (/verdict=OK/i.test(text)) updateGameChip("ok");
  else if (/verdict=FAIL/i.test(text)) updateGameChip("bad");

  const pidM = text.match(/pid=(\d+)/i);
  const baseM = text.match(/base=(0x[0-9A-Fa-f]+)/i);
  const zoneM = text.match(/code=['"]?([A-Za-z0-9_]+)['"]?/i);
  if (pidM && baseM) {
    const zone = zoneM ? ` · ${zoneM[1]}` : "";
    $("chip-game-text").textContent = `игра: pid ${pidM[1]} @ ${baseM[2].toUpperCase()}${zone}`;
  }

  const scoreM = text.match(/score=(\d+)/i);
  if (scoreM) {
    $("result-meta").textContent = `score = ${scoreM[1]}`;
  }

  // сегментная шкала: прыжок по маркерам вывода
  if (state.running) jumpStageFromText(text);

  // ── интерактивный промпт [Y/n] (--apply) ──
  // python печатает input()-промпт БЕЗ newline в конце порции — проверяем именно её
  if (/\[Y\/n\]:\s*$/.test(text.trim())) {
    showPrompt(lastPromptText(text));
  }
}

function lastPromptText(text) {
  // последняя строка порции, заканчивающаяся на '[Y/n]:'
  const lines = (text || "").split("\n");
  for (let i = lines.length - 1; i >= 0; i--) {
    const t = lines[i].trim();
    if (/\[Y\/n\]:\s*$/.test(t)) return t;
  }
  return "Применить правку?";
}

function onExit(p) {
  state.running = false;
  log(`\n» процесс завершён (exit=${p.code}, signal=${p.signal || "—"})`, p.code === 0 ? "ok-line" : "err-line");

  finishRunning(p);
}

/* ── Запуск команды ─────────────────────────────────────────────────── */
async function runCurrent(forceArgs = null) {
  const S = STEPS[state.step];

  // для --check сбрасываем прошлый результат
  if (state.step === "check") {
    state.checkDone = null;
    updateGameChip("checking");
    $("result-strip").hidden = true;
  }
  const args = forceArgs || (S.args instanceof Function ? S.args() : S.args);

  state.running = true;
  state.promptPending = false;
  $("prompt-box").hidden = true;

  $("btn-run").classList.add("running");
  $("btn-run").disabled = true;
  $("btn-stop").hidden = false;
  const isTests = (args || []).includes("--tests");
  $("action-badge").textContent = isTests ? "ТЕСТЫ…" : (forceArgs ? "АВТО ЧИНКА…" : "ВЫПОЛНЯЕТСЯ…");
  $("action-badge").className = "badge run";
  initStages();

  term.innerHTML = "";
  log(`» ${state.python} -X utf8 poe2_scope\\scope_main.py ${args.join(" ")}`, "ok-line");
  log(`» cwd: C:\\Projects\\POE2AUTO\\poe2_scope\n`, "warn-line");

  const r = await scope.start(state.python, args);
  if (!r.ok) {
    state.running = false;
    if (stagesTimer) { clearInterval(stagesTimer); stagesTimer = null; }
    $("stages").hidden = true;
    $("btn-run").classList.remove("running");
    $("btn-stop").hidden = true;
    log(`» не удалось запустить python: ${r.error || "?"}`, "err-line");
  }
}

// глобальная страховка: если runCurrent/прочее упало — сбрасываем UI, чтобы
// не оставался вечный «ВЫПОЛНЯЕТСЯ…» с пустым логом
window.addEventListener("error", (e) => {
  if (state.running) {
    state.running = false;
    if (stagesTimer) { clearInterval(stagesTimer); stagesTimer = null; }
    $("stages").hidden = true;
    $("btn-run").classList.remove("running");
    $("btn-stop").hidden = true;
    $("action-badge").textContent = "ОШИБКА UI";
    $("action-badge").className = "badge bad";
    log(`» ОШИБКА UI: ${e.message || "?"}`, "err-line");
  }
});

/* ── Завершение ─────────────────────────────────────────────────────── */
async function finishRunning(p) {
  $("btn-run").classList.remove("running");
  $("btn-stop").hidden = true;
  $("prompt-box").hidden = true;
  state.promptPending = false;

  const stopped = p.error === "stopped by user";
  const ok = p.code === 0;
  $("action-badge").textContent = stopped ? "ПРЕРВАНО" : (ok ? "ГОТОВО" : "ОШИБКА / ВНИМАНИЕ");
  $("action-badge").className = `badge ${stopped ? "warn" : (ok ? "good" : "bad")}`;

  finalizeStages(ok, stopped);

  const rst = $("result-strip");
  rst.hidden = false;
  const v = $("result-verdict");

  // парсим последний результат для мета-строки
  const meta = [];
  const mScore = term.textContent.match(/score=(\d+)(?:\/(\d+))?/);
  const mVerdict = term.textContent.match(/verdict=(\w+)/i);
  const mEntities = term.textContent.match(/(?:entities|сущностей)=(\d+)/i);
  const mNew = term.textContent.match(/(?:new|найдено)[^\n]*?(\d+)/i);

  if (state.step === "check") {
    const verdict = mVerdict ? mVerdict[1].toUpperCase() : (ok ? "OK" : "FAIL");
    state.checkDone = ok && /OK/.test(verdict);
    updateGameChip(state.checkDone ? "ok" : "bad");
    v.className = `result-verdict ${state.checkDone ? "ok" : "bad"}`;
    v.textContent = verdict;
    meta.push(mScore ? `score ${mScore[1]}/${mScore[2] || "—"}` : null);
    meta.push(mEntities ? `entities ${mEntities[1]}` : null);
    setStepState("check", state.checkDone ? "done" : "error");
  } else if (state.step === "find") {
    const found = mNew ? mNew[1] : "?";
    v.className = `result-verdict ${ok ? "ok" : "warn"}`;
    v.textContent = ok ? "НАЙДЕНЫ КАНДИДАТЫ" : "НЕ ПОЛНОСТЬЮ / ПРОВЕРЬ";
    meta.push(`кандидаты: ${found}`);
    setStepState("find", ok ? "done" : "error");
    // findings загружаем всегда — apply доступен при любых находках
    try {
      const rep = await scope.readReport("json");
      if (rep.ok) {
        const j = JSON.parse(rep.text);
        state.findings = Array.isArray(j) && j.length > 0 ? j : (j && j.findings ? j.findings : null);
      }
    } catch (_) { state.findings = null; }
  } else if (state.step === "verify") {
    // вердикт verify несёт exit-код: 0 = легаси живо, 1 = BROKEN
    const okV = ok;
    v.className = `result-verdict ${okV ? "ok" : "warn"}`;
    v.textContent = okV ? "ЛЕГАСИ ЖИВ" : "BROKEN (см. лог)";
    setStepState("verify", okV ? "done" : "error");
    const mScan = term.textContent.match(/offset_scanner:\s*([^\n]+)/i);
    const mMd = term.textContent.match(/mem_detect:\s*([^\n]+)/i);
    if (mScan) meta.push(mScan[1].trim());
    if (mMd) meta.push(mMd[1].trim());
  } else if (state.step === "apply") {
    // ok (exit=0, build собран) + что-то применено ИЛИ все оффсеты актуальны
    const applied = (/применен|applied|patched|актуальн/i.test(term.textContent) || /все оффсеты актуальны/i.test(term.textContent)) && ok;
    v.className = `result-verdict ${applied ? "ok" : "warn"}`;
    v.textContent = applied ? "ПРИМЕНЕНО" : "ЧАСТИЧНО / ПРОВЕРЬ";
    setStepState("apply", applied ? "done" : "error");
    const mBuild = term.textContent.match(/build[^\n]*(success|ok|успеш|FAIL)/i);
    if (mBuild) meta.push(`сборка DLL ${mBuild[1] === "FAIL" ? "ПРОВАЛЕНА" : "успешна"}`);
  } else if (state.step === "tests") {
    // прогон юнит-тестов (кнопка 🩺 или авто-чинка)
    const mT = /Тесты: (.+)/.exec(term.textContent);
    const mF = /(\d+) failed/.exec(term.textContent);
    v.className = `result-verdict ${ok ? "ok" : "bad"}`;
    v.textContent = ok ? "ТЕСТЫ ЗЕЛЁНЫЕ" : "ЕСТЬ ПАДЕНИЯ";
    if (mT) meta.push(mT[1].trim());
    if (mF) meta.push(`failed ${mF[1]}`);
  }

  $("result-meta").textContent = meta.filter(Boolean).join(" · ") || "см. лог";

  // тесты — виртуальный шаг: возвращаем карточку, где был юзер до запуска
  if (state.step === "tests" && state.prevStep) {
    state.step = state.prevStep;
    state.prevStep = null;
  }
  refreshSteps();
}

/* ── Промпт-кнопки (--apply [Y/n]) ──────────────────────────────────── */
function showPrompt(textMsg) {
  const box = $("prompt-box");
  if (state.promptPending) return;
  state.promptPending = true;
  $("prompt-text").textContent = textMsg;
  box.hidden = false;
  log("» ожидание ответа — кнопки выше ⬆", "prompt-line");
  term.scrollTop = term.scrollHeight;
}

async function answerPrompt(yes) {
  state.promptPending = false;
  $("prompt-box").hidden = true;
  if (yes === null) {
    await scope.kill();
    log("» прервано пользователем", "err-line");
    return;
  }
  await scope.sendInput(yes ? "y\n" : "n\n");
  log(yes ? "→ y (применить)" : "→ n (пропустить)", "warn-line");
}

/* ── Отчёт ──────────────────────────────────────────────────────────── */
function renderMd(md) {
  const body = $("report-body");
  const lines = md.replace(/\r\n/g, "\n").split("\n");
  let html = "";
  let inTable = false;
  let tableRows = [];
  let inCode = false;
  let codeBuf = [];

  const flushTable = () => {
    if (!tableRows.length) return;
    const head = tableRows[0].split("|").map((c) => c.trim()).filter(Boolean);
    let rows = tableRows.slice(1).filter((r) => !/^\s*[-|:\s]+\s*$/.test(r));
    html += "<table><thead><tr>" + head.map((h) => `<th>${esc(h)}</th>`).join("") + "</tr></thead><tbody>";
    for (const r of rows) {
      const cells = r.split("|").map((c) => c.trim()).filter(Boolean);
      html += "<tr>" + cells.map((c) => `<td>${fmt(esc(c))}</td>`).join("") + "</tr>";
    }
    html += "</tbody></table>";
    tableRows = [];
  };

  for (const raw of lines) {
    const line = raw;
    if (line.trim().startsWith("```")) {
      if (!inCode) { inCode = true; codeBuf = []; }
      else { inCode = false; html += "<pre><code>" + esc(codeBuf.join("\n")) + "</code></pre>"; }
      continue;
    }
    if (inCode) { codeBuf.push(line); continue; }
    if (line.trim().startsWith("|")) {
      inTable = true;
      if (!/^\s*\|[\s\-:|]+\|\s*$/.test(line)) tableRows.push(line);
      continue;
    }
    if (inTable) { flushTable(); inTable = false; }

    const t = line.trim();
    if (!t) { html += "<br/>"; continue; }
    if (/^#{1,3}\s/.test(t)) {
      const lvl = t.match(/^(#+)\s/)[1].length;
      html += `<h${lvl}>${fmt(esc(t.replace(/^#+\s/, "")))}</h${lvl}>`;
    } else if (/^[-*]\s/.test(t)) {
      html += `<li>${fmt(esc(t.replace(/^[-*]\s/, "")))}</li>`;
    } else if (/^\d+\.\s/.test(t)) {
      html += `<li>${fmt(esc(t.replace(/^\d+\.\s/, "")))}</li>`;
    } else if (/^\[.*\]\(.*\)\s*$/.test(t)) {
      // ссылки без действия — просто текст
      html += `<p>${fmt(esc(t))}</p>`;
    } else {
      html += `<p>${fmt(esc(t))}</p>`;
    }
  }
  if (inTable) flushTable();
  if (inCode) html += "<pre><code>" + esc(codeBuf.join("\n")) + "</code></pre>";

  body.innerHTML = html;
}

function esc(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
function fmt(s) {
  return s
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/✓/g, '<span class="md-ok">✓</span>')
    .replace(/✗/g, '<span class="md-bad">✗</span>');
}

async function openReport(kind) {
  const r = await scope.readReport(kind);
  if (!r.ok) {
    $("report-body").innerHTML = `<p class="muted">${esc(r.error)}</p>`;
    return;
  }
  if (kind === "json") {
    try {
      const j = JSON.parse(r.text);
      $("report-body").innerHTML = `<pre><code>${esc(JSON.stringify(j, null, 2))}</code></pre>`;
    } catch (_) {
      $("report-body").innerHTML = `<pre><code>${esc(r.text)}</code></pre>`;
    }
  } else {
    renderMd(r.text);
  }
}

/* ── Python ──────────────────────────────────────────────────────────── */
async function initPython() {
  const list = await scope.listPythons();
  const input = $("py-path");
  if (state.python && list.some((p) => p === state.python)) {
    input.value = state.python;
  } else {
    // первый существующий, иначе 'python'
    const pick = list.find((p) => p !== "python") || list[0] || "python";
    state.python = pick;
    input.value = pick;
    localStorage.setItem("poe2scope.python", pick);
  }
  $("chip-python-text").textContent = `python: ${input.value.split("\\").pop()}`;
  $("chip-python").querySelector(".chip-dot").className = input.value !== "python" ? "chip-dot good" : "chip-dot";
}

/* ── События ────────────────────────────────────────────────────────── */
document.addEventListener("DOMContentLoaded", () => {
  initPython();

  // подсказка для заблокированного шага
function getLockedTip(key) {
  if (key === "find") {
    if (state.checkDone === null) return { title: "Сначала шаг 1", text: "Поиск оффсетов запускается после проверки цепочки. Если она сломана — этот шаг загорится сам.", action: null };
    if (state.checkDone === true) return {
      title: "Проверка пройдена",
      text: "Бот жив — новые оффсеты не нужны. Если подозреваешь сдвиг (например, косит часть функций) — можно поискать принудительно.",
      action: { label: "Всё равно искать (--force)", run: () => {
        $("tog-force").checked = true;
        hideStepTip();
        state.step = "find";
        refreshSteps();
      } }
    };
  }
  if (key === "verify") return { title: "Сначала шаг 1", text: "Проверка легаси доступна после проверки цепочки. Выполни шаг 1 — и шаг 3 загорится.", action: null };
  if (key === "apply") return { title: "Сначала шаги 1–2", text: "Применение оффсетов доступно после поиска (шаг 2). Как только появятся находки — этот шаг загорится сам.", action: null };
  return { title: "Шаг недоступен", text: "Сначала выполни предыдущий шаг — порядок сверху вниз.", action: null };
}

function showStepTip(key, el) {
  const tip = $("step-tip");
  const info = getLockedTip(key);
  $("step-tip-title").textContent = info.title;
  $("step-tip-text").textContent = info.text;
  const act = $("step-tip-action");
  if (info.action) { act.hidden = false; act.textContent = info.action.label; act.onclick = info.action.run; }
  else { act.hidden = true; act.onclick = null; }
  const r = el.getBoundingClientRect();
  let left = r.right + 12;
  if (left + 320 > window.innerWidth) left = window.innerWidth - 332;
  tip.style.left = left + "px";
  tip.style.top = Math.max(8, r.top) + "px";
  tip.hidden = false;
}

function hideStepTip() {
  const tip = $("step-tip");
  tip.hidden = true;
  $("step-tip-action").onclick = null;
}

/* ── Подсказка про --force (fixed поверх карточки) ─────────────────── */
function positionHelpPop() {
  const pop = $("help-pop");
  const b = $("help-force").getBoundingClientRect();
  const popW = 380;
  const popH = pop.offsetHeight || 340;
  let left = b.left - 120;
  if (left + popW > window.innerWidth - 12) left = window.innerWidth - popW - 12;
  if (left < 12) left = 12;
  let top = b.bottom + 10;
  if (top + popH > window.innerHeight - 12) top = Math.max(12, b.top - popH - 10);
  pop.style.left = left + "px";
  pop.style.top = top + "px";
}

function toggleHelpPop() {
  const pop = $("help-pop");
  pop.hidden = !pop.hidden;
  if (!pop.hidden) positionHelpPop();
}

// выбор шага — ВСЕ шаги кликабельны: доступный переключает карточку,
// заблокированный показывает подсказку, как разблокировать
  document.querySelectorAll(".step").forEach((el) => {
    el.addEventListener("click", () => {
      const key = el.dataset.step;
      if (key === "auto") {
        if (state.running) return;      // занят — игнорируем повторный клик
        hideStepTip();
        runCurrent(["--auto", "--dump-md"]);
        return;
      }
      if (key === "tests") {
        if (state.running) return;      // занят — игнорируем повторный клик
        hideStepTip();
        state.prevStep = state.step;   // вернёмся сюда после прогона
        state.step = "tests";
        refreshSteps();
        runCurrent(["--tests", "--dump-md"]);
        return;
      }
      if (key === "report") { openReport("md"); switchPane("report"); return; }
      hideStepTip();
      if (!STEPS[key].need()) { showStepTip(key, el); return; }
      state.step = key;
      refreshSteps();
    });
  });

  // закрытие подсказки шага: ✕, клик мимо
  $("step-tip-close").addEventListener("click", hideStepTip);
  document.addEventListener("click", (e) => {
    const tip = $("step-tip");
    if (tip.hidden) return;
    if (tip.contains(e.target) || e.target.closest(".step")) return;
    hideStepTip();
  });

  // запуск/стоп
  $("btn-run").addEventListener("click", () => runCurrent());
  $("btn-auto").addEventListener("click", async () => {
    if (state.running) return;
    hideStepTip();
    await runCurrent(["--auto", "--dump-md"]);
  });
  $("btn-stop").addEventListener("click", async () => {
    await scope.kill();
    log("» остановлено пользователем", "err-line");
    // watchdog: если cmd:exit потерялся по любой причине — скидываем UI сами
    setTimeout(() => {
      if (state.running) {
        state.running = false;
        finishRunning({ code: null, signal: "SIGKILL", error: "stopped by user" });
      }
    }, 1200);
  });

  // промпт
  $("prompt-yes").addEventListener("click", () => answerPrompt(true));
  $("prompt-no").addEventListener("click", () => answerPrompt(false));
  $("prompt-abort").addEventListener("click", () => answerPrompt(null));

  // force-чекбокс
  $("tog-force").addEventListener("change", () => refreshSteps());

  // клик по строке --force (текст или чекбокс) тоже открывает подсказку
  $("force-row").addEventListener("click", (e) => {
    if (e.target.closest("#help-force")) return; // у «?» свой обработчик
    e.stopPropagation();
    toggleHelpPop();
  });

  // подсказка про --force
  $("help-force").addEventListener("click", (e) => {
    e.stopPropagation();
    toggleHelpPop();
  });
  $("help-close").addEventListener("click", () => { $("help-pop").hidden = true; });
  document.addEventListener("click", (e) => {
    const pop = $("help-pop");
    if (!pop.hidden && !e.target.closest("#help-pop") && !e.target.closest("#force-row")) {
      pop.hidden = true;
    }
  });

  // python
  $("btn-py-pick").addEventListener("click", async () => {
    const r = await scope.pickPython();
    if (r.ok) {
      state.python = r.path;
      $("py-path").value = r.path;
      localStorage.setItem("poe2scope.python", r.path);
      $("chip-python-text").textContent = `python: ${r.path.split("\\").pop()}`;
    }
  });
  $("py-path").addEventListener("change", (e) => {
    state.python = e.target.value.trim();
    localStorage.setItem("poe2scope.python", state.python);
    $("chip-python-text").textContent = `python: ${state.python.split("\\").pop()}`;
  });

  // вкладки пан
  document.querySelectorAll(".pane-tab").forEach((t) => {
    t.addEventListener("click", () => switchPane(t.dataset.pane));
  });

  // отчёты (sidebar: кнопка «Отчёт» в левом меню)
  $("btn-report-md").addEventListener("click", () => openReport("md"));
  $("btn-report-json").addEventListener("click", () => openReport("json"));

  scope.onData(onData);
  scope.onExit(onExit);

  refreshSteps();
});

function switchPane(name) {
  document.querySelectorAll(".pane-tab").forEach((t) => t.classList.toggle("active", t.dataset.pane === name));
  $("pane-log").classList.toggle("active", name === "log");
  $("pane-report").classList.toggle("active", name === "report");
}