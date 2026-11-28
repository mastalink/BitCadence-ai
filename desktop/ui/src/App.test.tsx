import { renderToStaticMarkup } from "react-dom/server";
import { expect, test } from "vitest";
import { App } from "./App";

test("the disconnected shell exposes an offline status and no runtime controls", () => {
  const markup = renderToStaticMarkup(<App />);
  expect(markup).toContain('role="status"');
  expect(markup).toContain("Runtime offline");
  expect(markup).not.toMatch(/<(button|a|form)\b/);
});
