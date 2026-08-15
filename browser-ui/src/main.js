import { Terminal } from "@xterm/xterm";
import MarkdownIt from "markdown-it";
import katex from "katex";
import "katex/dist/katex.css";
import {
  hasAssistantMarker,
  hasPromptMarker,
  isTransientComposer,
  isUserPanel,
  rememberPromptBackground,
} from "./codex-chrome.js";
import { normalizeTerminalMath } from "./latex-normalize.js";
import { MessageRecords } from "./message-records.js";
import "./style.css";

const DEFAULT_FOREGROUND = "var(--terminal-foreground)";
const DEFAULT_BACKGROUND = "var(--terminal-background)";

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

// xterm is the VT state engine only. It is never mounted into the page.
const terminal = new Terminal({
  cols: 80,
  rows: 24,
  convertEol: false,
  disableStdin: true,
  scrollback: 20_000,
});

function renderKatex(math, displayMode, source, escapeHtml) {
  try {
    return katex.renderToString(normalizeTerminalMath(math), {
      displayMode,
      throwOnError: true,
      strict: "ignore",
      trust: false,
      output: "htmlAndMathml",
    });
  } catch {
    const className = displayMode ? "math-source" : "math-source-inline";
    const tag = displayMode ? "pre" : "span";
    return `<${tag} class="${className}">${escapeHtml(source)}</${tag}>`;
  }
}

function mathMarkdownPlugin(md) {
  const escapeHtml = md.utils.escapeHtml;

  function looksLikeMath(content) {
    return /\\[A-Za-z]+|[-_^=+*/<>]|\d\s*[A-Za-z]|[A-Za-z]\s*\d|[∑∏∫√∞≈≠≤≥±×÷·]/.test(content);
  }

  function mathBlock(state, startLine, endLine, silent) {
    const start = state.bMarks[startLine] + state.tShift[startLine];
    const first = state.src.slice(start, state.eMarks[startLine]).trim();

    const singleLinePairs = [["\\[", "\\]"], ["$$", "$$"], ["[", "]"]];
    for (const [opener, closer] of singleLinePairs) {
      if (!first.startsWith(opener) || !first.endsWith(closer)) continue;
      if (first.length <= opener.length + closer.length) continue;
      const content = first.slice(opener.length, first.length - closer.length).trim();
      if (opener === "[" && !looksLikeMath(content)) continue;
      if (silent) return true;
      const token = state.push("math_block", "math", 0);
      token.block = true;
      token.content = content;
      token.meta = { source: first };
      token.map = [startLine, startLine + 1];
      state.line = startLine + 1;
      return true;
    }

    const pairs = new Map([["\\[", "\\]"], ["[", "]"], ["$$", "$$"]]);
    const closer = pairs.get(first);
    if (!closer) return false;
    const limit = Math.min(endLine, startLine + 64);
    let nextLine = startLine + 1;
    while (nextLine < limit) {
      const lineStart = state.bMarks[nextLine] + state.tShift[nextLine];
      const line = state.src.slice(lineStart, state.eMarks[nextLine]).trim();
      if (line === closer) break;
      nextLine += 1;
    }
    if (nextLine >= limit) return false;

    const content = state.getLines(startLine + 1, nextLine, 0, false).trim();
    if (!content || (first === "[" && !looksLikeMath(content))) return false;
    if (silent) return true;
    const token = state.push("math_block", "math", 0);
    token.block = true;
    token.content = content;
    token.meta = { source: `${first}\n${content}\n${closer}` };
    token.map = [startLine, nextLine + 1];
    state.line = nextLine + 1;
    return true;
  }

  function pushInline(state, end, math, source, displayMode) {
    const token = state.push(displayMode ? "math_inline_display" : "math_inline", "math", 0);
    token.content = math;
    token.meta = { source };
    state.pos = end;
  }

  function mathInline(state, silent) {
    const start = state.pos;
    const source = state.src;
    const max = state.posMax;

    const delimited = [
      { opener: "\\[", closer: "\\]", display: true },
      { opener: "\\(", closer: "\\)", display: false },
      { opener: "$$", closer: "$$", display: true },
    ];
    if (config.parseDollars !== false) {
      delimited.push({ opener: "$", closer: "$", display: false, dollars: true });
    }

    for (const rule of delimited) {
      if (!source.startsWith(rule.opener, start)) continue;
      if (rule.dollars && source.startsWith("$$", start)) continue;
      const contentStart = start + rule.opener.length;
      if (contentStart >= max || /\s/.test(source[contentStart])) return false;
      const endMarker = source.indexOf(rule.closer, contentStart);
      if (endMarker < 0 || endMarker >= max) return false;
      if (rule.dollars && source[endMarker + 1] === "$") return false;
      const math = source.slice(contentStart, endMarker).trim();
      if (!math || (rule.dollars && /^[\d.,]+$/.test(math))) return false;
      if (silent) return true;
      const end = endMarker + rule.closer.length;
      pushInline(state, end, math, source.slice(start, end), rule.display);
      return true;
    }

    return false;
  }

  function parenthesizedMath(state) {
    const pattern = /\(([^()\n]*\\[A-Za-z]+[^()\n]*)\)/g;
    const unsafeCommand = /\\(?:begin|end|left|right)\b/;

    for (const block of state.tokens) {
      if (!block.children) continue;
      const children = [];
      for (let index = 0; index < block.children.length;) {
        const token = block.children[index];
        // Code spans and fenced blocks are distinct token types, so this only
        // examines prose. With typographer/linkify enabled, Markdown-it can
        // split `\times` into adjacent text/text_special tokens, so coalesce
        // that prose run before looking for its surrounding parentheses.
        if (token.type !== "text" && token.type !== "text_special") {
          children.push(token);
          index += 1;
          continue;
        }
        const original = [];
        let content = "";
        while (index < block.children.length
          && ["text", "text_special"].includes(block.children[index].type)) {
          original.push(block.children[index]);
          content += block.children[index].content;
          index += 1;
        }
        let cursor = 0;
        let changed = false;
        pattern.lastIndex = 0;
        for (const match of content.matchAll(pattern)) {
          if (unsafeCommand.test(match[1])) continue;
          if (match.index > cursor) {
            const text = new state.Token("text", "", 0);
            text.content = content.slice(cursor, match.index);
            text.level = token.level;
            children.push(text);
          }
          const math = new state.Token("math_inline", "math", 0);
          math.content = match[1].trim();
          math.meta = { source: match[0] };
          math.level = token.level;
          children.push(math);
          cursor = match.index + match[0].length;
          changed = true;
        }
        if (!changed) {
          children.push(...original);
          continue;
        }
        if (cursor < content.length) {
          const text = new state.Token("text", "", 0);
          text.content = content.slice(cursor);
          text.level = token.level;
          children.push(text);
        }
      }
      block.children = children;
    }
  }

  md.block.ruler.before("fence", "anytex_math_block", mathBlock, {
    alt: ["paragraph", "reference", "blockquote", "list"],
  });
  md.inline.ruler.before("escape", "anytex_math_inline", mathInline);
  md.core.ruler.after("inline", "anytex_parenthesized_math", parenthesizedMath);
  md.renderer.rules.math_block = (tokens, index) => {
    const token = tokens[index];
    return `<div class="math-display">${renderKatex(
      token.content,
      true,
      token.meta.source,
      escapeHtml,
    )}</div>\n`;
  };
  md.renderer.rules.math_inline = (tokens, index) => {
    const token = tokens[index];
    return renderKatex(token.content, false, token.meta.source, escapeHtml);
  };
  md.renderer.rules.math_inline_display = (tokens, index) => {
    const token = tokens[index];
    return `<span class="math-inline-display">${renderKatex(
      token.content,
      true,
      token.meta.source,
      escapeHtml,
    )}</span>`;
  };
}

const markdown = new MarkdownIt({
  html: false,
  linkify: true,
  typographer: true,
  breaks: false,
}).use(mathMarkdownPlugin);

let snapshots = [];
let bufferType;
let previousColumns = terminal.cols;
let previousLength = 0;
let previousBaseY = 0;
let renderFrame;
let settledRenderTimer;
const messageRecords = new MessageRecords();
const nodes = new Map();
const promptBackgrounds = new Set();

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

function emptySnapshot() {
  return {
    text: "",
    fragments: [],
    signature: "",
    wrapped: false,
    rowBackground: DEFAULT_BACKGROUND,
  };
}

function captureLine(buffer, row) {
  const line = buffer.getLine(row);
  if (!line) return emptySnapshot();
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

  if (!reset && snapshots.length === buffer.length && snapshots[0]) {
    const firstText = buffer.getLine(0)?.translateToString(true) ?? "";
    if (firstText !== snapshots[0].text) changedStart = 0;
  }
  if (changedStart === 0) snapshots = [];
  snapshots.length = buffer.length;
  for (let row = changedStart; row < buffer.length; row += 1) {
    snapshots[row] = captureLine(buffer, row);
  }

  let contentLength = snapshots.length;
  while (contentLength > 0 && snapshots[contentLength - 1].text === "") contentLength -= 1;
  snapshots.length = contentLength;

  bufferType = buffer.type;
  previousColumns = terminal.cols;
  previousLength = buffer.length;
  previousBaseY = buffer.baseY;
  return { cursorRow: buffer.baseY + buffer.cursorY };
}

function scheduleRender() {
  window.cancelAnimationFrame(renderFrame);
  renderFrame = window.requestAnimationFrame(renderTranscript);
}

function scheduleSettledRender() {
  window.clearTimeout(settledRenderTimer);
  settledRenderTimer = window.setTimeout(() => {
    // Codex can finish a response with several cursor-addressed repaints in
    // quick succession. Capture once more after that burst so the last math
    // delimiter is present before the response is frozen by the input panel.
    scheduleRender();
  }, 160);
}

function trimRows(rows) {
  let start = 0;
  let end = rows.length;
  while (start < end && !rows[start].text.trim()) start += 1;
  while (end > start && !rows[end - 1].text.trim()) end -= 1;
  return rows.slice(start, end);
}

function logicalLines(rows) {
  const lines = [];
  for (const row of trimRows(rows)) {
    const text = row.text.trimEnd();
    if (row.wrapped && lines.length) lines[lines.length - 1] += text;
    else lines.push(text);
  }
  return lines;
}

function commonIndent(lines) {
  const indents = lines
    .filter((line) => line.trim())
    .map((line) => line.length - line.trimStart().length);
  return indents.length ? Math.min(...indents) : 0;
}

function markdownSource(rows, kind) {
  const lines = logicalLines(rows);
  const indent = commonIndent(lines);
  const normalized = lines.map((line) => line.slice(Math.min(indent, line.length)));
  const first = normalized.findIndex((line) => line.trim());
  if (first >= 0) {
    normalized[first] = kind === "user"
      ? normalized[first].replace(/^\s*[›❯>]\s?/, "")
      : normalized[first].replace(/^\s*[•·●⏺]\s?/, "");
  }
  return normalized.join("\n").trim();
}

function rangeSignature(rows) {
  return rows.map((row) => row.signature).join("\u0002");
}

function rangeBackground(rows) {
  const counts = new Map();
  for (const row of rows) {
    const background = row.rowBackground;
    if (background === DEFAULT_BACKGROUND) continue;
    counts.set(background, (counts.get(background) || 0) + 1);
  }
  let result = DEFAULT_BACKGROUND;
  let maximum = 0;
  for (const [background, count] of counts) {
    if (count > maximum) {
      result = background;
      maximum = count;
    }
  }
  return result;
}

function findPanelRanges(startAt = 0) {
  const ranges = [];
  for (let start = startAt; start < snapshots.length;) {
    if (snapshots[start].rowBackground === DEFAULT_BACKGROUND) {
      start += 1;
      continue;
    }
    let end = start + 1;
    while (end < snapshots.length && snapshots[end].rowBackground !== DEFAULT_BACKGROUND) end += 1;
    const rows = snapshots.slice(start, end);
    const first = rows.find((row) => row.text.trim())?.text.trimStart() || "";
    const background = rangeBackground(rows);
    const marker = hasPromptMarker(first);
    const transient = isTransientComposer(rows);
    rememberPromptBackground(
      marker,
      transient,
      background,
      DEFAULT_BACKGROUND,
      promptBackgrounds,
    );
    ranges.push({
      start,
      end,
      background,
      marker,
      transient,
    });
    start = end;
  }

  // Claude Code uses a prompt marker between divider rows rather than Codex's
  // full-width background. Capture the marked logical line as a panel too.
  // Wrapped continuation rows have isWrapped set on the continuation line.
  const coveredRows = new Set(ranges.flatMap(
    (range) => Array.from({ length: range.end - range.start }, (_, index) => range.start + index),
  ));
  for (let start = startAt; start < snapshots.length; start += 1) {
    if (coveredRows.has(start) || !hasPromptMarker(snapshots[start].text)) continue;
    let end = start + 1;
    while (end < snapshots.length && snapshots[end].wrapped && !coveredRows.has(end)) end += 1;
    const rows = snapshots.slice(start, end);
    ranges.push({
      start,
      end,
      background: rangeBackground(rows),
      marker: true,
      transient: isTransientComposer(rows),
    });
    start = end - 1;
  }
  ranges.sort((left, right) => left.start - right.start);
  for (const range of ranges) {
    range.user = isUserPanel(range.marker, range.background, promptBackgrounds);
  }
  return ranges;
}

function isCodexStatusLine(text) {
  return /^\s*(?:[•·]\s+)?\S+\s+(?:low|medium|high|xhigh|max|ultra)\s+·\s+(?:[~/]|[A-Za-z]:\\)/i
    .test(text);
}

function isCodexProgressLine(text) {
  return /^\s*[•·]\s+(?:Working|Starting MCP servers)\b.*(?:esc to interrupt)/i.test(text);
}

function isTuiDivider(text) {
  return /^\s*[─━]{8,}\s*$/.test(text);
}

function isClaudeStatusLine(text) {
  return /^\s*(?:⏸|⏵)\s+.*(?:mode on|for shortcuts|for agents)/i.test(text);
}

function filteredRows(start, end, excludedRows, stripChrome = false) {
  const rows = [];
  for (let row = start; row < end; row += 1) {
    if (excludedRows.has(row)) continue;
    if (stripChrome && (isCodexStatusLine(snapshots[row].text)
      || isCodexProgressLine(snapshots[row].text)
      || isClaudeStatusLine(snapshots[row].text)
      || isTuiDivider(snapshots[row].text))) continue;
    rows.push(snapshots[row]);
  }
  return rows;
}

function firstAssistantMarker(start, end) {
  for (let row = start; row < end; row += 1) {
    const text = snapshots[row].text;
    if (!isCodexStatusLine(text)
      && !isCodexProgressLine(text)
      && hasAssistantMarker(text)) return row;
  }
  return -1;
}

function looksLikeMarkdown(rows) {
  const source = logicalLines(rows).join("\n");
  return /(^|\n)\s*(?:#{1,6}\s|```|\\\[|\[\s*$|\$\$\s*$)/m.test(source)
    || /\\\(.+?\\\)|\$\S.+?\$/s.test(source);
}

function messageModel(kind, start, end, cursorRow, excludedRows) {
  const rows = filteredRows(start, end, excludedRows, kind === "assistant");
  if (!rows.some((row) => row.text.trim())) return undefined;
  const source = markdownSource(rows, kind);
  return {
    key: `${bufferType}:${start}:${kind}`,
    kind,
    messageRole: kind,
    start,
    end: end - 1,
    rows,
    source,
    active: cursorRow >= start && cursorRow < end,
    background: kind === "user" ? rangeBackground(rows) : DEFAULT_BACKGROUND,
    signature: `${kind}:${rangeSignature(rows)}:${source}`,
  };
}

function terminalModel(start, end, excludedRows, panel = false, messageRole) {
  const rows = filteredRows(start, end, excludedRows, true);
  if (!rows.some((row) => row.text.trim())) return undefined;
  return {
    key: `${bufferType}:${start}:terminal`,
    kind: "terminal",
    messageRole,
    start,
    end: end - 1,
    rows,
    panel,
    signature: `terminal:${Number(panel)}:${rangeSignature(rows)}`,
  };
}

function pushModel(models, model) {
  if (model) models.push(model);
}

function addDefaultRange(models, start, end, asAssistant, cursorRow, excludedRows) {
  const rows = filteredRows(start, end, excludedRows, asAssistant);
  if (start >= end || !rows.some((row) => row.text.trim())) return;
  if (asAssistant) {
    pushModel(models, messageModel("assistant", start, end, cursorRow, excludedRows));
    return;
  }
  const marker = firstAssistantMarker(start, end);
  if (marker >= 0) {
    if (marker > start) pushModel(models, terminalModel(start, marker, excludedRows));
    pushModel(models, messageModel("assistant", marker, end, cursorRow, excludedRows));
  } else if (looksLikeMarkdown(rows)) {
    pushModel(models, messageModel("assistant", start, end, cursorRow, excludedRows));
  } else {
    pushModel(models, terminalModel(start, end, excludedRows));
  }
}

function buildModels(cursorRow, startAt = 0) {
  const models = [];
  const panelRanges = findPanelRanges(startAt);
  // Composer panels are part of the useful mirror even though they are not
  // conversation messages. Segment them from the surrounding transcript so
  // their prompt background and live terminal styling remain visible without
  // allowing placeholder text to leak into an assistant Markdown block.
  const visiblePanelRanges = panelRanges.filter((range) => range.user || range.transient);
  const excludedRows = new Set();
  const userRanges = panelRanges.filter((range) => range.user && !range.transient);
  let position = startAt;
  let haveUser = messageRecords.committed.some((record) => record.messageRole === "user");
  let activeUserTail = false;

  for (const range of visiblePanelRanges) {
    addDefaultRange(models, position, range.start, haveUser, cursorRow, excludedRows);
    if (range.transient) {
      pushModel(models, terminalModel(range.start, range.end, excludedRows, true));
      position = range.end;
      continue;
    }
    const cursorInside = cursorRow >= range.start && cursorRow < range.end;
    const isLatest = range === userRanges[userRanges.length - 1];
    const tailHasResponse = isLatest && snapshots.slice(range.end).some(
      (row) => row.text.trim() && !isCodexStatusLine(row.text),
    );
    const active = cursorInside || (isLatest && !tailHasResponse);
    if (active) {
      pushModel(models, terminalModel(range.start, range.end, excludedRows, true, "user"));
      activeUserTail = true;
    }
    else pushModel(models, messageModel("user", range.start, range.end, cursorRow, excludedRows));
    position = range.end;
    haveUser = true;
  }
  if (activeUserTail) {
    if (position < snapshots.length
      && snapshots.slice(position).some((row) => row.text.trim())) {
      pushModel(models, terminalModel(position, snapshots.length, excludedRows));
    }
  } else {
    addDefaultRange(models, position, snapshots.length, haveUser, cursorRow, excludedRows);
  }
  return models;
}

function freezeBefore(models) {
  let latestMessage = -1;
  for (let index = 0; index < models.length; index += 1) {
    if (models[index].messageRole) latestMessage = index;
  }
  return Math.max(latestMessage, 0);
}

function applyTextStyle(element, style) {
  element.style.color = style.foreground;
  element.style.backgroundColor = style.background;
  if (style.bold) element.style.fontWeight = "700";
  if (style.italic) element.style.fontStyle = "italic";
  if (style.dim) element.style.opacity = "0.65";
  if (style.decorations) element.style.textDecoration = style.decorations;
}

function renderTerminalRows(node, model) {
  node.className = `transcript-block terminal-group${model.panel ? " panel" : ""}`;
  for (const snapshot of model.rows) {
    const row = document.createElement("div");
    row.className = "terminal-row";
    row.style.backgroundColor = snapshot.rowBackground;
    for (const fragment of snapshot.fragments) {
      const span = document.createElement("span");
      span.textContent = fragment.text;
      applyTextStyle(span, fragment.style);
      row.append(span);
    }
    node.append(row);
  }
}

function renderModel(node, model) {
  node.replaceChildren();
  node.style.cssText = "";
  if (model.kind === "terminal") {
    renderTerminalRows(node, model);
    return;
  }
  node.className = `transcript-block message ${model.kind}${model.active ? " active" : ""}`;
  if (model.kind === "user") node.style.backgroundColor = model.background;
  if (model.source) node.innerHTML = markdown.render(model.source);
  else node.textContent = "\u00a0";
}

function scrollAnchor() {
  const atBottom = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 32;
  if (atBottom) return { atBottom };
  const rect = scroller.getBoundingClientRect();
  const point = document.elementFromPoint(rect.left + 24, rect.top + 4);
  const block = point?.closest?.(".transcript-block");
  if (!block) return { atBottom, scrollTop: scroller.scrollTop };
  return { atBottom, key: block.dataset.key, offset: block.offsetTop - scroller.scrollTop };
}

function restoreScroll(anchor) {
  if (anchor.atBottom) {
    scroller.scrollTop = scroller.scrollHeight;
    return;
  }
  const block = anchor.key ? nodes.get(anchor.key) : undefined;
  if (block?.isConnected) scroller.scrollTop = block.offsetTop - anchor.offset;
  else if (anchor.scrollTop !== undefined) scroller.scrollTop = anchor.scrollTop;
}

function reconcile(models) {
  const anchor = scrollAnchor();
  const desired = new Set(models.map((model) => model.key));
  let cursor = transcript.firstChild;

  for (const model of models) {
    let node = nodes.get(model.key);
    if (!node) {
      node = document.createElement("section");
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

  for (const [key, node] of nodes) {
    if (desired.has(key)) continue;
    nodes.delete(key);
    node.remove();
  }
  restoreScroll(anchor);
}

function renderTranscript() {
  const { cursorRow } = captureBuffer();
  const models = buildModels(cursorRow, messageRecords.startRow);
  const records = messageRecords.update(models, freezeBefore(models));
  reconcile(records);
  transcript.style.setProperty("--terminal-columns", terminal.cols);
  // If the candidate changed during the settle pass, give it another bounded
  // pass. This self-flushes the final repaint without polling forever.
  if (models.length && messageRecords.pendingFreeze) scheduleSettledRender();
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
    scheduleSettledRender();
  });
  events.addEventListener("output", (event) => {
    const binary = atob(event.data);
    const bytes = Uint8Array.from(binary, (character) => character.charCodeAt(0));
    terminal.write(bytes, () => {
      scheduleRender();
      scheduleSettledRender();
    });
  });
}
