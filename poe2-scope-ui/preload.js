// poe2-scope-ui / preload.js
// Безопасный мост renderer ↔ main: только нужные функции, без node-доступа в DOM.
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("scope", {
  // python
  listPythons: () => ipcRenderer.invoke("py:list"),
  pickPython: () => ipcRenderer.invoke("py:pick"),

  // команды
  start: (python, args) => ipcRenderer.invoke("cmd:start", { python, args }),
  sendInput: (text) => ipcRenderer.invoke("cmd:stdin", text),
  kill: () => ipcRenderer.invoke("cmd:kill"),

  // события процесса
  onData: (cb) => ipcRenderer.on("cmd:data", (_e, p) => cb(p)),
  onExit: (cb) => ipcRenderer.on("cmd:exit", (_e, p) => cb(p)),

  // отчёты
  readReport: (kind) => ipcRenderer.invoke("file:report", kind),
});