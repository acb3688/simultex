import { Terminal } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";
import katex from "katex";
import "katex/dist/katex.css";
import "./style.css";

const configElement = document.querySelector('meta[name="anytex-config"]');
const config = configElement ? JSON.parse(configElement.content) : {};
const params = new URLSearchParams(window.location.search);
const token = params.get("token");
const status = document.querySelector("#status");
const host = document.querySelector("#terminal-host");
const terminalElement = document.querySelector("#terminal");
const overlays = document.querySelector("#math-overlays");

const terminal = new Terminal({
  cols: 80,
  rows: 24,
  convertEol: false,
  cursorBlink: false,
  disableStdin: true,
  scrollback: 20_000,
  fontFamily: "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace",
  fontSize: 15,
  lineHeight: 1.15,
  theme: {
    background: "#242830",
    foreground: "#e7e9ee",
    cursor: "#e7e9ee",
    selectionBackground: "#53607888",
  },
});
terminal.open(terminalElement);

let scanTimer;
function scheduleScan() {
  window.clearTimeout(scanTimer);
  scanTimer = window.setTimeout(renderVisibleMath, 25);
}

function lineText(buffer, row) {
  return buffer.getLine(row)?.translateToString(true) ?? "";
}

function indent(text) {
  return text.length - text.trimStart().length;
}

function findRegions(buffer, first, last) {
  const lines = [];
  for (let row = first; row <= last; row += 1) lines.push(lineText(buffer, row));
  const regions = [];
  const claimed = new Set();
  const pairs = new Map([["\\[", "\\]"], ["[", "]"], ["$$", "$$"]]);

  for (let local = 0; local < lines.length; local += 1) {
    const opener = lines[local].trim();
    const closer = pairs.get(opener);
    if (!closer) continue;
    let end = local + 1;
    while (end < lines.length && lines[end].trim() !== closer) end += 1;
    if (end >= lines.length) continue;
    const math = lines.slice(local + 1, end).map((line) => line.trim()).join("\n").trim();
    if (!math || (opener === "[" && !math.includes("\\"))) continue;
    const source = lines.slice(local, end + 1);
    const nonempty = source.filter((line) => line.trim());
    const column = Math.min(...nonempty.map(indent));
    const width = Math.max(...source.map((line) => Math.max(1, line.trimEnd().length - column)));
    regions.push({ math, block: true, row: first + local, endRow: first + end, column, width });
    for (let row = local; row <= end; row += 1) claimed.add(row);
    local = end;
  }

  const patterns = [
    { regex: /\\\((.+?)\\\)/g, dollars: false },
    { regex: /\((\\[A-Za-z]+(?:[^()]|\\[()])*)\)/g, dollars: false },
  ];
  if (config.parseDollars !== false) {
    patterns.push({ regex: /(?<!\\)\$(?!\$)(\S(?:.*?\S)?)\$(?!\$)/g, dollars: true });
  }
  lines.forEach((line, local) => {
    if (claimed.has(local)) return;
    for (const pattern of patterns) {
      pattern.regex.lastIndex = 0;
      for (const match of line.matchAll(pattern.regex)) {
        const math = match[1].trim();
        if (!math || (pattern.dollars && /^[\d.,]+$/.test(math))) continue;
        regions.push({
          math,
          block: false,
          row: first + local,
          endRow: first + local,
          column: match.index,
          width: match[0].length,
        });
      }
    }
  });
  return regions;
}

function renderVisibleMath() {
  const buffer = terminal.buffer.active;
  const viewport = buffer.viewportY;
  const first = Math.max(0, viewport - 40);
  const last = Math.min(buffer.length - 1, viewport + terminal.rows + 40);
  const regions = findRegions(buffer, first, last);
  overlays.replaceChildren();

  const screen = terminalElement.querySelector(".xterm-screen");
  if (!screen || terminal.cols === 0 || terminal.rows === 0) return;
  const screenRect = screen.getBoundingClientRect();
  const hostRect = host.getBoundingClientRect();
  const cellWidth = screenRect.width / terminal.cols;
  const cellHeight = screenRect.height / terminal.rows;

  for (const region of regions) {
    if (region.endRow < viewport || region.row >= viewport + terminal.rows) continue;
    const visibleStart = Math.max(region.row, viewport);
    const visibleEnd = Math.min(region.endRow, viewport + terminal.rows - 1);
    const element = document.createElement("div");
    element.className = region.block ? "math-region block" : "math-region inline";
    element.style.left = `${screenRect.left - hostRect.left + region.column * cellWidth}px`;
    element.style.top = `${screenRect.top - hostRect.top + (visibleStart - viewport) * cellHeight}px`;
    element.style.width = `${Math.max(cellWidth, region.width * cellWidth)}px`;
    element.style.height = `${Math.max(cellHeight, (visibleEnd - visibleStart + 1) * cellHeight)}px`;
    try {
      katex.render(region.math, element, {
        displayMode: region.block,
        throwOnError: false,
        strict: "ignore",
        trust: false,
        output: "htmlAndMathml",
      });
    } catch {
      continue;
    }
    overlays.append(element);
  }
}

terminal.onScroll(scheduleScan);
new ResizeObserver(scheduleScan).observe(host);

if (!token) {
  status.textContent = "Missing access token";
  status.className = "error";
} else {
  const events = new EventSource(`/events?token=${encodeURIComponent(token)}`);
  events.addEventListener("open", () => {
    status.textContent = "Live";
    status.className = "live";
  });
  events.addEventListener("error", () => {
    status.textContent = "Disconnected";
    status.className = "error";
  });
  events.addEventListener("resize", (event) => {
    const { columns, rows } = JSON.parse(event.data);
    if (columns > 0 && rows > 0) terminal.resize(columns, rows);
    scheduleScan();
  });
  events.addEventListener("output", (event) => {
    const binary = atob(event.data);
    const bytes = Uint8Array.from(binary, (character) => character.charCodeAt(0));
    terminal.write(bytes, scheduleScan);
  });
}
