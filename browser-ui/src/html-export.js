import { SNAPSHOT_COPY_SCRIPT } from "./copy-source.js";

function bytesToBase64(buffer) {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  const chunkSize = 0x8000;
  for (let start = 0; start < bytes.length; start += chunkSize) {
    binary += String.fromCharCode(...bytes.subarray(start, start + chunkSize));
  }
  return btoa(binary);
}

export function snapshotFilename(now = new Date()) {
  const stamp = now.toISOString().replace(/[-:]/g, "").replace(/\.\d{3}Z$/, "Z");
  return `anytex-transcript-${stamp}.html`;
}

export async function inlineCssAssets(css, baseUrl, fetchAsset = fetch) {
  const pattern = /url\(\s*(["']?)(.*?)\1\s*\)/g;
  const urls = [...css.matchAll(pattern)]
    .map((match) => match[2])
    .filter((value) => value && !/^(?:data:|blob:|#)/i.test(value));
  const replacements = new Map();

  await Promise.all([...new Set(urls)].map(async (value) => {
    const absolute = new URL(value, baseUrl).href;
    try {
      const response = await fetchAsset(absolute);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const mimeType = response.headers.get("content-type")?.split(";", 1)[0]
        || "application/octet-stream";
      replacements.set(
        value,
        `data:${mimeType};base64,${bytesToBase64(await response.arrayBuffer())}`,
      );
    } catch {
      // Keep an absolute fallback when an optional asset cannot be embedded.
      replacements.set(value, absolute);
    }
  }));

  return css.replace(pattern, (source, _quote, value) => (
    replacements.has(value) ? `url("${replacements.get(value)}")` : source
  ));
}

async function snapshotCss(document, fetchAsset) {
  const styles = [];
  for (const sheet of document.styleSheets) {
    let css;
    try {
      css = [...sheet.cssRules].map((rule) => rule.cssText).join("\n");
    } catch {
      if (!sheet.href) continue;
      const response = await fetchAsset(sheet.href);
      if (!response.ok) continue;
      css = await response.text();
    }
    styles.push(await inlineCssAssets(css, sheet.href || document.baseURI, fetchAsset));
  }
  return styles.join("\n");
}

function addRecordDiagnostics(clone, records) {
  const blocks = new Map(
    [...clone.querySelectorAll("#transcript > .transcript-block")]
      .map((node) => [node.dataset.key, node]),
  );
  for (const record of records) {
    const block = blocks.get(record.key);
    if (!block) continue;
    block.dataset.kind = record.kind || "";
    block.dataset.role = record.messageRole || "";
    block.dataset.frozen = String(Boolean(record.frozen));
    block.dataset.authoritative = String(Boolean(record.authoritative));
    block.dataset.renderSignature = record.signature || "";
    if (record.apiSessionId) block.dataset.apiSessionId = record.apiSessionId;
    if (record.apiTurnId) block.dataset.apiTurnId = record.apiTurnId;
    if (record.apiCallId) block.dataset.apiCallId = record.apiCallId;
    if (record.apiProvider) block.dataset.apiProvider = record.apiProvider;
    if (record.source !== undefined) block.dataset.source = record.source;
  }
}

export function serializeApiDiagnostics(diagnostics) {
  return JSON.stringify(diagnostics || { version: 1, events: [], turns: [] })
    .replace(/[<>&\u2028\u2029]/g, (character) => (
      `\\u${character.charCodeAt(0).toString(16).padStart(4, "0")}`
    ));
}

function addApiDiagnostics(document, clone, diagnostics) {
  const script = document.createElement("script");
  script.id = "anytex-api-transcript";
  script.type = "application/json";
  script.textContent = serializeApiDiagnostics(diagnostics);
  clone.querySelector("body")?.append(script);
}

function addCopyRuntime(document, clone) {
  const script = document.createElement("script");
  script.dataset.anytexCopyRuntime = "";
  script.textContent = SNAPSHOT_COPY_SCRIPT;
  clone.querySelector("body")?.append(script);
}

export async function createSnapshotHtml(
  document,
  records,
  now = new Date(),
  fetchAsset = fetch,
  apiDiagnostics,
) {
  const css = await snapshotCss(document, fetchAsset);
  const clone = document.documentElement.cloneNode(true);
  clone.dataset.anytexSnapshot = now.toISOString();
  clone.querySelectorAll("script, link[rel='stylesheet'], meta[name='anytex-config']")
    .forEach((node) => node.remove());
  clone.querySelector("#download-html")?.remove();

  const style = document.createElement("style");
  style.dataset.anytexSnapshotStyles = "";
  style.textContent = css;
  clone.querySelector("head")?.append(style);

  const title = clone.querySelector("title");
  if (title) title.textContent = "AnyTeX Transcript Snapshot";
  const status = clone.querySelector("#status");
  if (status) {
    status.textContent = "Snapshot";
    status.className = "";
  }
  const mode = clone.querySelector(".mode");
  if (mode) mode.textContent = `saved ${now.toISOString()}`;
  addRecordDiagnostics(clone, records);
  addCopyRuntime(document, clone);
  addApiDiagnostics(document, clone, apiDiagnostics);

  return `<!doctype html>\n${clone.outerHTML}\n`;
}

export async function downloadSnapshot(
  document,
  records,
  now = new Date(),
  apiDiagnostics,
) {
  const html = await createSnapshotHtml(document, records, now, fetch, apiDiagnostics);
  const blob = new Blob([html], { type: "text/html;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = snapshotFilename(now);
  link.hidden = true;
  document.body.append(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1_000);
}
