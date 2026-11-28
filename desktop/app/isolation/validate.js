window.__TAURI_ISOLATION_HOOK__ = (message) => {
  const { cmd, payload } = message;
  if (
    cmd !== "report_first_paint" ||
    payload == null ||
    typeof payload !== "object" ||
    Object.keys(payload).length !== 3 ||
    !Number.isFinite(payload.firstPaintMs) ||
    payload.firstPaintMs < 0 ||
    !Number.isFinite(payload.firstContentfulPaintMs) ||
    payload.firstContentfulPaintMs < payload.firstPaintMs ||
    typeof payload.offlineVisible !== "boolean"
  ) {
    throw new Error("Desktop IPC request denied");
  }
  return message;
};
