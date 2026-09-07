// poe2-scope-ui / main.js
// Главный процесс Electron: запуск python-команд poe2_scope, стриминг вывода,
// stdin для интерактивных промптов (--apply), чтение отчётов, детект python.
const { app, BrowserWindow, ipcMain, dialog } = require("electron");
const { spawn } = require("child_process");
const fs = require("fs");
const path = require("path");

const ROOT = "C:\\Projects\\POE2AUTO";                     // рабочая папка проекта
const SCOPE = path.join(ROOT, "poe2_scope");
const SCOPE_MAIN = path.join(SCOPE, "scope_main.py");
const REPORT_JSON = path.join(SCOPE, "report.json");
const REPORT_MD = path.join(SCOPE, "report.md");

// Кандидаты python-интерпретаторов (проверяем по порядку существования)
const PY_CANDIDATES = [
  path.join(process.env.USERPROFILE || "", "anaconda3", "python.exe"),
  path.join(process.env.USERPROFILE || "", "miniconda3", "python.exe"),
  path.join(process.env.LOCALAPPDATA || "", "Programs", "Python", "Python313", "python.exe"),
  path.join(process.env.LOCALAPPDATA || "", "Programs", "Python", "Python312", "python.exe"),
  "C:\\Python313\\python.exe",
  "C:\\Python312\\python.exe",
  "python",
];

let win = null;
let proc = null; // текущий дочерний python-процесс

function createWindow() {
  win = new BrowserWindow({
    width: 1280,
    height: 840,
    minWidth: 1000,
    minHeight: 680,
    backgroundColor: "#0d0f14",
    title: "poe2_scope — offset suite",
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
  });
  win.loadFile(path.join(__dirname, "renderer", "index.html"));
}

// ── IPC: найди доступные python ─────────────────────────────────────────
function findPythons() {
  const found = [];
  for (const cand of PY_CANDIDATES) {
    const ok = cand === "python"
      ? true // 'python' — берём только как fallback без проверки файла
      : (cand && fs.existsSync(cand));
    if (ok) found.push(cand);
  }
  return found;
}

// ── IPC: запуск команды scope_main.py ───────────────────────────────────
function runCommand(payload, event) {
  const { python, args } = payload || {};
  const py = python && fs.existsSync(python) ? python : "python";

  if (proc) {
    try { proc.kill(); } catch (_) { /* noop */ }
    proc = null;
  }

  const fullArgs = ["-X", "utf8", SCOPE_MAIN, ...(args || [])];
  let child;
  try {
    child = spawn(py, fullArgs, {
      cwd: ROOT,
      windowsHide: true,
      stdio: ["pipe", "pipe", "pipe"],
      env: { ...process.env, PYTHONIOENCODING: "utf-8", PYTHONUTF8: "1" },
    });
  } catch (e) {
    event.sender.send("cmd:exit", { code: 1, signal: null, error: String(e.message || e) });
    return { ok: false, pid: null };
  }
  proc = child;

  child.stdout.on("data", (d) => event.sender.send("cmd:data", { stream: "out", text: d.toString("utf8") }));
  child.stderr.on("data", (d) => event.sender.send("cmd:data", { stream: "err", text: d.toString("utf8") }));
  child.on("error", (e) => event.sender.send("cmd:exit", { code: 1, signal: null, error: String(e.message || e) }));
  child.on("exit", (code, signal) => {
    // игнорируем exit устаревшего процесса (был убит/заменён новым стартом или Стоп)
    if (proc !== child) return;
    proc = null;
    event.sender.send("cmd:exit", { code, signal, error: null });
  });

  return { ok: true, pid: child.pid };
}

// ── IPC: отправить строку в stdin работающего процесса (для [Y/n]) ──────
function writeStdin(text) {
  if (!proc || !proc.stdin) return { ok: false };
  try {
    proc.stdin.write(text);
    return { ok: true };
  } catch (e) {
    return { ok: false, error: String(e.message || e) };
  }
}

// ── IPC: терминация текущего процесса ───────────────────────────────────
function killProc() {
  if (proc) {
    const pid = proc.pid;
    try {
      // tree-kill: python может порождать tasklist / dotnet build — убиваем всё дерево
      spawn("taskkill", ["/PID", String(pid), "/T", "/F"], { windowsHide: true });
    } catch (_) { /* noop */ }
    try { proc.kill(); } catch (_) { /* noop */ }
    proc = null;
  }
  // ВСЕГДА уведомляем UI (даже если proc уже null) — чтобы кнопка гарантированно ожила
  if (win && !win.isDestroyed()) {
    win.webContents.send("cmd:exit", { code: null, signal: "SIGKILL", error: "stopped by user" });
  }
  return { ok: true };
}

// ── IPC: чтение файлов отчётов ──────────────────────────────────────────
function readReport(kind) {
  const p = kind === "md" ? REPORT_MD : REPORT_JSON;
  try {
    if (!fs.existsSync(p)) return { ok: false, error: "отчёт ещё не создан — сначала выполни шаг" };
    return { ok: true, text: fs.readFileSync(p, "utf8") };
  } catch (e) {
    return { ok: false, error: String(e.message || e) };
  }
}

// ── IPC: выбрать python вручную ─────────────────────────────────────────
async function pickPython() {
  const r = await dialog.showOpenDialog(win, {
    title: "Выбери python.exe (желательно тот же, что в PATH проекта)",
    properties: ["openFile"],
    defaultPath: path.join(process.env.USERPROFILE || "", "anaconda3", "python.exe"),
  });
  if (r.canceled || !r.filePaths.length) return { ok: false };
  return { ok: true, path: r.filePaths[0] };
}

// ── IPC-регистрация ─────────────────────────────────────────────────────
ipcMain.handle("py:list", () => findPythons());
ipcMain.handle("py:pick", () => pickPython());
ipcMain.handle("cmd:start", (ev, payload) => runCommand(payload, ev));
ipcMain.handle("cmd:stdin", (_ev, text) => writeStdin(text));
ipcMain.handle("cmd:kill", () => killProc());
ipcMain.handle("file:report", (_ev, kind) => readReport(kind));

// ── запуск ──────────────────────────────────────────────────────────────
app.whenReady().then(() => {
  createWindow();
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});

// Smoke-тест: env SMOKE=1 → приложение стартует и выходит через 3с
if (process.env.SMOKE) {
  setTimeout(() => app.quit(), 3000);
}