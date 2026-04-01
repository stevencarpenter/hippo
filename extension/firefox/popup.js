/**
 * Hippo Browser Capture — popup script.
 *
 * Manages the enable/disable toggle, capture count display, and domain
 * allowlist editing. All state persists in browser.storage.local.
 */
(function () {
  "use strict";

  const DEFAULT_ALLOWLIST = [
    "github.com",
    "stackoverflow.com",
    "developer.mozilla.org",
    "docs.rs",
    "doc.rust-lang.org",
    "crates.io",
    "npmjs.com",
    "pypi.org",
    "docs.python.org",
    "man7.org",
    "wiki.archlinux.org",
  ];

  const enabledCheckbox = document.getElementById("enabled");
  const countDisplay = document.getElementById("count");
  const allowlistTextarea = document.getElementById("allowlist");
  const saveButton = document.getElementById("save");
  const savedIndicator = document.getElementById("saved");

  // --- Load current settings ---
  browser.storage.local
    .get(["enabled", "allowlist", "captureCount"])
    .then((result) => {
      enabledCheckbox.checked =
        typeof result.enabled === "boolean" ? result.enabled : true;

      const domains =
        Array.isArray(result.allowlist) && result.allowlist.length > 0
          ? result.allowlist
          : DEFAULT_ALLOWLIST;
      allowlistTextarea.value = domains.join("\n");

      countDisplay.textContent = result.captureCount || 0;
    });

  // --- Toggle enabled state immediately on change ---
  enabledCheckbox.addEventListener("change", function () {
    browser.storage.local.set({ enabled: enabledCheckbox.checked });
  });

  // --- Save allowlist ---
  saveButton.addEventListener("click", function () {
    const lines = allowlistTextarea.value
      .split("\n")
      .map((line) => line.trim().toLowerCase())
      .filter((line) => line.length > 0);

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
})();
