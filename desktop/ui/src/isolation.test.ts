import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import { expect, test } from "vitest";

test("isolation accepts only the native paint report and its expected payload", () => {
  const window = { __TAURI_ISOLATION_HOOK__: (message: unknown) => message };
  const script = readFileSync(new URL("../../app/isolation/validate.js", import.meta.url), "utf8");
  runInNewContext(script, { window });

  const validate = window.__TAURI_ISOLATION_HOOK__;
  const report = {
    cmd: "report_first_paint",
    payload: { firstPaintMs: 10, firstContentfulPaintMs: 12, offlineVisible: true },
  };
  expect(validate(report)).toBe(report);
  expect(() => validate({ ...report, cmd: "plugin:event|emit" })).toThrow("Desktop IPC request denied");
  expect(() => validate({ ...report, payload: { ...report.payload, extra: true } })).toThrow("Desktop IPC request denied");
  expect(() => validate({ ...report, payload: { ...report.payload, firstPaintMs: 15 } })).toThrow("Desktop IPC request denied");
});
