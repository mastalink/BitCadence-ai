import { invoke, isTauri } from "@tauri-apps/api/core";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import "./style.css";

if (isTauri() && PerformanceObserver.supportedEntryTypes.includes("paint")) {
  // The root is empty until React renders. Observe the browser's first contentful paint.
  const observer = new PerformanceObserver((entries) => {
    const contentfulPaint = entries.getEntriesByName("first-contentful-paint")[0];
    if (!contentfulPaint) return;
    observer.disconnect();

    const status = document.querySelector<HTMLElement>('[role="status"] h2');
    const offlineVisible = Boolean(
      status?.textContent?.trim() === "Runtime offline" &&
        status.checkVisibility({ visibilityProperty: true, opacityProperty: true }),
    );
    const firstPaint = performance.getEntriesByName("first-paint", "paint")[0];
    if (!firstPaint) return;
    invoke("report_first_paint", {
      firstPaintMs: firstPaint.startTime,
      firstContentfulPaintMs: contentfulPaint.startTime,
      offlineVisible,
    }).catch((error: unknown) => console.error("Desktop paint report failed", error));
  });
  observer.observe({ type: "paint", buffered: true });
}

createRoot(document.getElementById("root")!).render(<App />);
