import { Terminal } from "@xterm/xterm";
import katex from "katex";
import "katex/dist/katex.css";
import "./style.css";

const DEFAULT_FOREGROUND = "var(--terminal-foreground)";
const DEFAULT_BACKGROUND = "var(--terminal-background)";
const MAX_BLOCK_ROWS = 48;
const MATH_STABILITY_MS = 140;

const ANSI_COLORS = [
  "#1d2027", "#d16969", "#69c07b", "#d7ba7d",
  "#6f9fd8", "#c586c0", "#65c3c8", "#d4d4d4",
  "#6b7280", "#f48771", "#89d185", "#e5c07b",
  "#8ab4e8", "#d8a0df", "#7ad7dc", "#f3f4f6",
];

const configElement = document.querySelector('meta[name="anytex-config"]');
const config = configElement ? JSON.parse(configElement.content) : {};
const params = new URLSearchParams(window.location.search);
const token = params.get("token");
const status = document.querySelector("#status");
const scroller = document.querySelector("#transcript-host");
const transcript = document.querySelector("#transcript");

// xterm is deliberately never opened. It is our VT parser and buffer, not the
// browser's renderer. The visible document below is ordinary, scrollable HTML.
const terminal = new Terminal({
  cols: 80,
  rows: 24,
  convertEol: false,
  disableStdin: true,
  scrollback: 20_000,
});

let snapshots = [];
let bufferType;
let previousColumns = terminal.cols;
let previousLength = 0;
let previousBaseY = 0;
let renderFrame;
let stabilityTimer;
const nodes = new Map();
const candidates = new Map();
const committedMath = new Map();
let seenMathIds;

function paletteColor(index) {
  if (index < ANSI_COLORS.length) return ANSI_COLORS[index];
  if (index >= 232) {
    const level = 8 + (index - 232) * 10;
    return `rgb(${level} ${level} ${level})`;
  }
  const offset = index - 16;
  const red = Math.floor(offset / 36);
  const green = Math.floor((offset % 36) / 6);
  const blue = offset % 6;
  const channel = (value) => value === 0 ? 0 : 55 + value * 40;
  return `rgb(${channel(red)} ${channel(green)} ${channel(blue)})`;
}

function cellColor(cell, foreground) {
  const isDefault = foreground ? cell.isFgDefault() : cell.isBgDefault();
  if (isDefault) return foreground ? DEFAULT_FOREGROUND : DEFAULT_BACKGROUND;
  const isRgb = foreground ? cell.isFgRGB() : cell.isBgRGB();
  const value = foreground ? cell.getFgColor() : cell.getBgColor();
  if (!isRgb) return paletteColor(value);
  return `#${value.toString(16).padStart(6, "0")}`;
}

function cellStyle(cell) {
  let foreground = cellColor(cell, true);
  let background = cellColor(cell, false);
  if (cell.isInverse()) [foreground, background] = [background, foreground];
  const decorations = [];
  if (cell.isUnderline()) decorations.push("underline");
  if (cell.isStrikethrough()) decorations.push("line-through");
  if (cell.isOverline()) decorations.push("overline");
  const style = {
    foreground,
    background,
    bold: Boolean(cell.isBold()),
    italic: Boolean(cell.isItalic()),
    dim: Boolean(cell.isDim()),
    decorations: decorations.join(" "),
  };
  style.key = [
    style.foreground,
    style.background,
    Number(style.bold),
    Number(style.italic),
    Number(style.dim),
    style.decorations,
  ].join("|");
  return style;
}

function captureLine(buffer, row) {
  const line = buffer.getLine(row);
  if (!line) {
    return {
      text: "",
      fragments: [],
      signature: "",
      wrapped: false,
      rowBackground: DEFAULT_BACKGROUND,
    };
  }
  const workCell = buffer.getNullCell();
  const fragments = [];
  let current;
  let offset = 0;
  let meaningfulEnd = 0;

  for (let column = 0; column < terminal.cols; column += 1) {
    const cell = line.getCell(column, workCell);
    if (!cell || cell.getWidth() === 0) continue;
    const style = cellStyle(cell);
    const chars = cell.isInvisible() ? " " : (cell.getChars() || " ");
    if (!current || current.style.key !== style.key) {
      current = { text: "", start: offset, end: offset, style };
      fragments.push(current);
    }
    current.text += chars;
    offset += chars.length;
    current.end = offset;
    if (chars.trim() || !cell.isAttributeDefault()) meaningfulEnd = offset;
  }

  const kept = [];
  for (const fragment of fragments) {
    if (fragment.start >= meaningfulEnd) break;
    if (fragment.end > meaningfulEnd) {
      fragment.text = fragment.text.slice(0, meaningfulEnd - fragment.start);
      fragment.end = meaningfulEnd;
    }
    kept.push(fragment);
  }
  const text = kept.map((fragment) => fragment.text).join("");
  const backgroundCounts = new Map();
  for (const fragment of kept) {
    const previous = backgroundCounts.get(fragment.style.background) || 0;
    backgroundCounts.set(fragment.style.background, previous + fragment.text.length);
  }
  let rowBackground = DEFAULT_BACKGROUND;
  let backgroundCoverage = 0;
  for (const [background, coverage] of backgroundCounts) {
    if (background !== DEFAULT_BACKGROUND && coverage > backgroundCoverage) {
      rowBackground = background;
      backgroundCoverage = coverage;
    }
  }
  // Full-width TUI panels commonly paint each terminal cell. Put that color
  // on the row box itself so CSS line leading cannot expose seams between
  // adjacent rows. Partial highlights remain scoped to their text spans.
  if (backgroundCoverage < terminal.cols / 2) rowBackground = DEFAULT_BACKGROUND;
  const signature = `${Number(line.isWrapped)}:${kept.map(
    (fragment) => `${fragment.style.key}:${fragment.text}`,
  ).join("\u0001")}`;
  return { text, fragments: kept, signature, wrapped: line.isWrapped, rowBackground };
}

function captureBuffer() {
  const buffer = terminal.buffer.active;
  const reset = buffer.type !== bufferType
    || terminal.cols !== previousColumns
    || buffer.length < previousLength
    || buffer.baseY < previousBaseY;
  let changedStart = reset ? 0 : Math.min(previousBaseY, buffer.baseY);

  // Once scrollback is full, xterm trims from the front without changing its
  // length. Detect that rare case and rebuild row identities coherently.
  if (!reset && snapshots.length === buffer.length && snapshots[0]) {
    const firstText = buffer.getLine(0)?.translateToString(true) ?? "";
    if (firstText !== snapshots[0].text) changedStart = 0;
  }

  if (changedStart === 0) snapshots = [];
  snapshots.length = buffer.length;
  for (let row = changedStart; row < buffer.length; row += 1) {
    snapshots[row] = captureLine(buffer, row);
  }

  // A terminal buffer always contains a full screen of rows, even when the
  // lower rows have never held content. They are implementation capacity, not
  // transcript, and would otherwise create a large empty tail in the page.
  let contentLength = snapshots.length;
  while (contentLength > 0 && snapshots[contentLength - 1].text === "") {
    contentLength -= 1;
  }
  snapshots.length = contentLength;

  bufferType = buffer.type;
  previousColumns = terminal.cols;
  previousLength = buffer.length;
  previousBaseY = buffer.baseY;
  return {
    buffer,
    changedStart,
    cursorRow: buffer.baseY + buffer.cursorY,
  };
}

function scheduleRender() {
  window.cancelAnimationFrame(renderFrame);
  renderFrame = window.requestAnimationFrame(renderTranscript);
}

function scheduleStabilityPass(delay) {
  if (stabilityTimer !== undefined) return;
  stabilityTimer = window.setTimeout(() => {
    stabilityTimer = undefined;
    scheduleRender();
  }, Math.max(1, delay));
}

function stableMath(id, signature, start, end, cursorRow, now) {
  seenMathIds?.add(id);
  if (cursorRow >= start && cursorRow <= end) {
    candidates.delete(id);
    committedMath.delete(id);
    return false;
  }
  if (committedMath.get(id)?.signature === signature) return true;
  const candidate = candidates.get(id);
  if (!candidate || candidate.signature !== signature) {
    candidates.set(id, { signature, since: now, start });
    scheduleStabilityPass(MATH_STABILITY_MS);
    return false;
  }
  const age = now - candidate.since;
  if (age < MATH_STABILITY_MS) {
    scheduleStabilityPass(MATH_STABILITY_MS - age);
    return false;
  }
  committedMath.set(id, { signature, start });
  candidates.delete(id);
  return true;
}

function findBlock(snapshotsToScan, start, cursorRow, now) {
  const opener = snapshotsToScan[start]?.text.trim();
  const pairs = new Map([["\\[", "\\]"], ["[", "]"], ["$$", "$$"]]);
  const closer = pairs.get(opener);
  if (!closer) return undefined;
  const limit = Math.min(snapshotsToScan.length, start + MAX_BLOCK_ROWS);
  let end = start + 1;
  while (end < limit && snapshotsToScan[end]?.text.trim() !== closer) end += 1;
  if (end >= limit) return undefined;
  const math = snapshotsToScan.slice(start + 1, end)
    .map((line) => line.text.trim())
    .join("\n")
    .trim();
  if (!math || (opener === "[" && !math.includes("\\"))) return undefined;
  const id = `${bufferType}:${start}:block`;
  const signature = `${end}:${math}`;
  if (!stableMath(id, signature, start, end, cursorRow, now)) return undefined;
  return { id, start, end, math, signature };
}

function inlineRegions(snapshot, row, cursorRow, now) {
  const patterns = [
    { regex: /\\\[(.+?)\\\]/g, block: true, dollars: false },
    { regex: /\$\$(.+?)\$\$/g, block: true, dollars: true },
    { regex: /\\\((.+?)\\\)/g, block: false, dollars: false },
    { regex: /\((\\[A-Za-z]+(?:[^()]|\\[()])*)\)/g, block: false, dollars: false },
  ];
  if (config.parseDollars !== false) {
    patterns.push({
      regex: /(?<!\\)\$(?!\$)(\S(?:.*?\S)?)\$(?!\$)/g,
      block: false,
      dollars: true,
    });
  }
  const regions = [];
  for (const pattern of patterns) {
    pattern.regex.lastIndex = 0;
    for (const match of snapshot.text.matchAll(pattern.regex)) {
      const math = match[1].trim();
      const start = match.index;
      const end = start + match[0].length;
      if (!math || (pattern.dollars && !pattern.block && /^[\d.,]+$/.test(math))) continue;
      if (regions.some((region) => start < region.end && end > region.start)) continue;
      const id = `${bufferType}:${row}:inline:${start}:${end}`;
      const signature = `${pattern.block}:${math}`;
      if (!stableMath(id, signature, row, row, cursorRow, now)) continue;
      regions.push({ id, start, end, math, block: pattern.block, signature });
    }
  }
  return regions.sort((left, right) => left.start - right.start);
}

function dominantBackground(start, end) {
  const counts = new Map();
  for (let row = start; row <= end; row += 1) {
    for (const fragment of snapshots[row].fragments) {
      const count = counts.get(fragment.style.background) || 0;
      counts.set(fragment.style.background, count + fragment.text.length);
    }
  }
  let result = DEFAULT_BACKGROUND;
  let maximum = -1;
  for (const [background, count] of counts) {
    if (count > maximum) {
      maximum = count;
      result = background;
    }
  }
  return result;
}

function buildModels(start, cursorRow) {
  const models = [];
  const now = performance.now();
  seenMathIds = new Set();
  for (let row = start; row < snapshots.length; row += 1) {
    const block = findBlock(snapshots, row, cursorRow, now);
    if (block) {
      const source = snapshots.slice(row, block.end + 1);
      const nonempty = source.map((line) => line.text).filter((line) => line.trim());
      const background = dominantBackground(row, block.end);
      const column = nonempty.length
        ? Math.min(...nonempty.map((line) => line.search(/\S/)).filter((value) => value >= 0))
        : 0;
      models.push({
        key: `${bufferType}:${row}`,
        kind: "math",
        start: row,
        end: block.end,
        math: block.math,
        column,
        background,
        signature: `math:${block.signature}:${column}:${background}`,
      });
      row = block.end;
      continue;
    }
    const snapshot = snapshots[row];
    const inline = inlineRegions(snapshot, row, cursorRow, now);
    models.push({
      key: `${bufferType}:${row}`,
      kind: "row",
      start: row,
      end: row,
      snapshot,
      inline,
      signature: `row:${snapshot.signature}:${inline.map(
        (region) => `${region.start}:${region.end}:${region.signature}`,
      ).join("|")}`,
    });
  }
  for (const [id, candidate] of candidates) {
    if (candidate.start >= start && !seenMathIds.has(id)) candidates.delete(id);
  }
  for (const [id, committed] of committedMath) {
    if (committed.start >= start && !seenMathIds.has(id)) committedMath.delete(id);
  }
  seenMathIds = undefined;
  return models;
}

function applyTextStyle(element, style) {
  element.style.color = style.foreground;
  element.style.backgroundColor = style.background;
  if (style.bold) element.style.fontWeight = "700";
  if (style.italic) element.style.fontStyle = "italic";
  if (style.dim) element.style.opacity = "0.65";
  if (style.decorations) element.style.textDecoration = style.decorations;
}

function appendTextRange(parent, snapshot, start, end) {
  for (const fragment of snapshot.fragments) {
    if (fragment.end <= start || fragment.start >= end) continue;
    const from = Math.max(start, fragment.start) - fragment.start;
    const to = Math.min(end, fragment.end) - fragment.start;
    const span = document.createElement("span");
    span.textContent = fragment.text.slice(from, to);
    applyTextStyle(span, fragment.style);
    parent.append(span);
  }
}

function sourceStyleAt(snapshot, offset) {
  return snapshot.fragments.find(
    (fragment) => fragment.start <= offset && fragment.end > offset,
  )?.style;
}

function renderModel(node, model) {
  node.replaceChildren();
  node.className = `transcript-block ${model.kind === "math" ? "display-math" : "terminal-row"}`;
  node.classList.toggle("wrapped", Boolean(model.snapshot?.wrapped));
  node.style.cssText = "";

  if (model.kind === "math") {
    node.style.backgroundColor = model.background;
    const math = document.createElement("div");
    math.className = "display-math-content";
    math.style.marginLeft = `${model.column}ch`;
    katex.render(model.math, math, {
      displayMode: true,
      throwOnError: false,
      strict: "ignore",
      trust: false,
      output: "htmlAndMathml",
    });
    node.append(math);
    return;
  }

  let offset = 0;
  node.style.backgroundColor = model.snapshot.rowBackground;
  for (const region of model.inline) {
    appendTextRange(node, model.snapshot, offset, region.start);
    const math = document.createElement("span");
    math.className = region.block ? "inline-display-math" : "inline-math";
    const sourceStyle = sourceStyleAt(model.snapshot, region.start);
    if (sourceStyle) applyTextStyle(math, sourceStyle);
    katex.render(region.math, math, {
      displayMode: region.block,
      throwOnError: false,
      strict: "ignore",
      trust: false,
      output: "htmlAndMathml",
    });
    node.append(math);
    offset = region.end;
  }
  appendTextRange(node, model.snapshot, offset, model.snapshot.text.length);
}

function scrollAnchor() {
  const atBottom = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 32;
  if (atBottom) return { atBottom };
  const point = document.elementFromPoint(
    scroller.getBoundingClientRect().left + 24,
    scroller.getBoundingClientRect().top + 4,
  );
  const block = point?.closest?.(".transcript-block");
  if (!block) return { atBottom, scrollTop: scroller.scrollTop };
  return {
    atBottom,
    key: block.dataset.key,
    offset: block.offsetTop - scroller.scrollTop,
  };
}

function restoreScroll(anchor) {
  if (anchor.atBottom) {
    scroller.scrollTop = scroller.scrollHeight;
    return;
  }
  const block = anchor.key ? nodes.get(anchor.key) : undefined;
  if (block?.isConnected) {
    scroller.scrollTop = block.offsetTop - anchor.offset;
  } else if (anchor.scrollTop !== undefined) {
    scroller.scrollTop = anchor.scrollTop;
  }
}

function reconcileTail(start, modelsToRender) {
  const anchor = scrollAnchor();
  const desired = new Set(modelsToRender.map((model) => model.key));
  const existingTail = [...transcript.children].filter(
    (node) => Number(node.dataset.endRow) >= start,
  );
  let cursor = existingTail[0] || null;

  for (const model of modelsToRender) {
    let node = nodes.get(model.key);
    if (!node) {
      node = document.createElement("div");
      nodes.set(model.key, node);
    }
    node.dataset.key = model.key;
    node.dataset.startRow = String(model.start);
    node.dataset.endRow = String(model.end);
    if (node._anytexSignature !== model.signature) {
      renderModel(node, model);
      node._anytexSignature = model.signature;
    }
    if (node !== cursor) transcript.insertBefore(node, cursor);
    cursor = node.nextSibling;
  }

  for (const node of existingTail) {
    if (desired.has(node.dataset.key)) continue;
    nodes.delete(node.dataset.key);
    node.remove();
  }
  restoreScroll(anchor);
}

function renderTranscript() {
  const { changedStart, cursorRow } = captureBuffer();
  let pendingStart = changedStart;
  for (const candidate of candidates.values()) {
    pendingStart = Math.min(pendingStart, candidate.start);
  }
  // Include pending formulas even if very fast output moved them beyond the
  // normal tail lookback before their short stability window elapsed.
  const rebuildStart = Math.max(0, Math.min(changedStart - MAX_BLOCK_ROWS, pendingStart));
  const modelsToRender = buildModels(rebuildStart, cursorRow);
  reconcileTail(rebuildStart, modelsToRender);
  transcript.style.setProperty("--terminal-columns", terminal.cols);
}

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
    if (columns > 0 && rows > 0) {
      terminal.resize(columns, rows);
      snapshots = [];
      previousLength = 0;
      previousBaseY = 0;
      bufferType = undefined;
    }
    scheduleRender();
  });
  events.addEventListener("output", (event) => {
    const binary = atob(event.data);
    const bytes = Uint8Array.from(binary, (character) => character.charCodeAt(0));
    terminal.write(bytes, scheduleRender);
  });
}
