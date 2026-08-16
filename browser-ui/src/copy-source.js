export async function copyText(
  text,
  navigatorObject = globalThis.navigator,
  documentObject = globalThis.document,
) {
  if (navigatorObject?.clipboard?.writeText) {
    try {
      await navigatorObject.clipboard.writeText(text);
      return;
    } catch {
      // Local snapshot files may not have Clipboard API permission.
    }
  }

  if (!documentObject?.body) throw new Error("clipboard is unavailable");
  const textarea = documentObject.createElement("textarea");
  textarea.value = text;
  textarea.readOnly = true;
  textarea.style.position = "fixed";
  textarea.style.left = "-10000px";
  textarea.style.opacity = "0";
  documentObject.body.append(textarea);
  textarea.select();
  textarea.setSelectionRange(0, text.length);
  const copied = documentObject.execCommand?.("copy");
  textarea.remove();
  if (!copied) throw new Error("clipboard copy failed");
}

async function activate(region) {
  try {
    await copyText(region.dataset.copySource || "");
    region.classList.remove("copy-failed");
  } catch (error) {
    region.classList.add("copy-failed");
    console.warn("Could not copy AnyTeX source", error);
    window.setTimeout(() => region.classList.remove("copy-failed"), 1_400);
  }
}

export function installCopyInteractions(root) {
  root.addEventListener("click", (event) => {
    const region = event.target.closest?.(".copy-region");
    if (region && root.contains(region)) void activate(region);
  });
  root.addEventListener("keydown", (event) => {
    if (!["Enter", " "].includes(event.key)) return;
    const region = event.target.closest?.(".copy-region");
    if (!region || !root.contains(region)) return;
    event.preventDefault();
    region.classList.add("copy-pressed");
    void activate(region);
  });
  root.addEventListener("keyup", (event) => {
    if (!["Enter", " "].includes(event.key)) return;
    event.target.closest?.(".copy-region")?.classList.remove("copy-pressed");
  });
  root.addEventListener("focusout", (event) => {
    event.target.closest?.(".copy-region")?.classList.remove("copy-pressed");
  });
}

function snapshotCopyRuntime() {
  async function write(text) {
    if (navigator.clipboard?.writeText) {
      try {
        await navigator.clipboard.writeText(text);
        return;
      } catch {
        // Fall through for file:// snapshots and denied permissions.
      }
    }
    const textarea = document.createElement("textarea");
    textarea.value = text;
    textarea.readOnly = true;
    textarea.style.cssText = "position:fixed;left:-10000px;opacity:0";
    document.body.append(textarea);
    textarea.select();
    textarea.setSelectionRange(0, text.length);
    const copied = document.execCommand("copy");
    textarea.remove();
    if (!copied) throw new Error("clipboard copy failed");
  }

  async function activateSnapshot(region) {
    try {
      await write(region.dataset.copySource || "");
      region.classList.remove("copy-failed");
    } catch {
      region.classList.add("copy-failed");
      window.setTimeout(() => region.classList.remove("copy-failed"), 1400);
    }
  }

  document.addEventListener("click", (event) => {
    const region = event.target.closest?.(".copy-region");
    if (region) void activateSnapshot(region);
  });
  document.addEventListener("keydown", (event) => {
    if (!["Enter", " "].includes(event.key)) return;
    const region = event.target.closest?.(".copy-region");
    if (!region) return;
    event.preventDefault();
    region.classList.add("copy-pressed");
    void activateSnapshot(region);
  });
  document.addEventListener("keyup", (event) => {
    if (!["Enter", " "].includes(event.key)) return;
    event.target.closest?.(".copy-region")?.classList.remove("copy-pressed");
  });
  document.addEventListener("focusout", (event) => {
    event.target.closest?.(".copy-region")?.classList.remove("copy-pressed");
  });
}

export const SNAPSHOT_COPY_SCRIPT = `(${snapshotCopyRuntime.toString()})();`;
