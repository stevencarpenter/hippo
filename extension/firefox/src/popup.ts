/**
 * Hippo Browser Capture -- popup script.
 *
 * Manages the enable/disable toggle, capture count display, and domain
 * allowlist editing. All state persists in browser.storage.local.
 */

import { DEFAULT_ALLOWLIST } from "./config";

const enabledCheckbox = document.getElementById("enabled") as HTMLInputElement;
const countDisplay = document.getElementById("count") as HTMLElement;
const allowlistTextarea = document.getElementById(
  "allowlist",
) as HTMLTextAreaElement;
const saveButton = document.getElementById("save") as HTMLButtonElement;
const savedIndicator = document.getElementById("saved") as HTMLElement;

/** Format a millisecond age into a human-readable string like "4m ago". */
function formatAge(ageMs: number): string {
  const secs = Math.floor(ageMs / 1000);
  if (secs < 60) return `${secs}s ago`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 48) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

/** Update the heartbeat badge based on `lastHeartbeatTs` from storage. */
function renderHeartbeatBadge(lastHeartbeatTs: number | null | undefined): void {
  const badge = document.getElementById("heartbeat-badge");
  if (!badge) return;

  if (lastHeartbeatTs == null) {
    badge.textContent = "never";
    badge.className = "hb-badge hb-unknown";
    return;
  }

  // Clamp to 0 in case of clock skew or storage corruption (future timestamp).
  const ageMs = Math.max(0, Date.now() - lastHeartbeatTs);
  const label = formatAge(ageMs);

  if (ageMs < 2 * 60 * 1000) {
    badge.textContent = label;
    badge.className = "hb-badge hb-green";
  } else if (ageMs < 10 * 60 * 1000) {
    badge.textContent = label;
    badge.className = "hb-badge hb-yellow";
  } else {
    badge.textContent = label;
    badge.className = "hb-badge hb-red";
  }
}

// --- Load current settings ---
browser.storage.local
  .get(["enabled", "allowlist", "captureCount", "lastSendOk", "lastSendAt", "lastHeartbeatTs"])
  .then((result) => {
    enabledCheckbox.checked =
      typeof result.enabled === "boolean" ? result.enabled : true;

    const stored = result.allowlist;
    const domains =
      Array.isArray(stored) && stored.length > 0
        ? (stored as string[])
        : DEFAULT_ALLOWLIST;
    allowlistTextarea.value = domains.join("\n");

    countDisplay.textContent = String(result.captureCount ?? 0);

    // Show daemon connection status
    const statusEl = document.getElementById("status");
    if (statusEl) {
      if (result.lastSendAt == null) {
        statusEl.textContent = "No data sent yet";
        statusEl.style.color = "#859289";
      } else if (result.lastSendOk) {
        statusEl.textContent = "Connected";
        statusEl.style.color = "#a7c080";
      } else {
        statusEl.textContent = "Daemon unreachable";
        statusEl.style.color = "#e67e80";
      }
    }

    // Show heartbeat badge
    renderHeartbeatBadge(
      typeof result.lastHeartbeatTs === "number" ? result.lastHeartbeatTs : null
    );
  });

// --- Toggle enabled state immediately on change ---
enabledCheckbox.addEventListener("change", () => {
  browser.storage.local.set({ enabled: enabledCheckbox.checked });
});

// --- Save allowlist ---
saveButton.addEventListener("click", () => {
  const lines = allowlistTextarea.value
    .split("\n")
    .map((line) => line.trim().toLowerCase())
    .map((line) => line.replace(/^https?:\/\//, ""))  // strip protocol
    .map((line) => line.replace(/^\*\./, ""))          // strip wildcard prefix
    .map((line) => line.replace(/\/.*$/, ""))          // strip path
    .filter((line) => line.length > 0 && line.includes(".")); // must look like a domain

  // Deduplicate
  const unique = [...new Set(lines)];

  browser.storage.local.set({ allowlist: unique }).then(() => {
    // Update textarea with cleaned list
    allowlistTextarea.value = unique.join("\n");

    // Flash saved indicator
    savedIndicator.classList.add("show");
    setTimeout(() => {
      savedIndicator.classList.remove("show");
    }, 1500);
  });
});
