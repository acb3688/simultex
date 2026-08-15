const DEFAULT_SUGGESTIONS = [
  /^Explain this codebase$/i,
  /^Implement \{feature\}$/i,
  /^Improve documentation in @filename$/i,
  /^Summarize recent commits$/i,
  /^Try\s+["“].+["”]$/i,
];

export function hasPromptMarker(text) {
  return /^[›❯>]\s?/.test(text.trimStart());
}

export function hasAssistantMarker(text) {
  return /^[•·●⏺]\s?/.test(text.trimStart());
}

export function panelText(rows) {
  return rows
    .map((row) => row.text.trim())
    .filter(Boolean)
    .join("\n")
    .replace(/^[›❯>]\s?/, "")
    .trim();
}

export function isDefaultSuggestion(text) {
  return DEFAULT_SUGGESTIONS.some((pattern) => pattern.test(text.trim()));
}

export function isUserPanel(marker, background, promptBackgrounds) {
  return marker || promptBackgrounds.has(background);
}

export function rememberPromptBackground(
  marker,
  transient,
  background,
  defaultBackground,
  promptBackgrounds,
) {
  if ((marker || transient) && background !== defaultBackground) {
    promptBackgrounds.add(background);
  }
}

function dimCharacterRatio(rows) {
  let dim = 0;
  let total = 0;
  for (const row of rows) {
    for (const fragment of row.fragments) {
      const meaningful = fragment.text.replace(/\s/g, "").replace(/[›❯>]/g, "");
      total += meaningful.length;
      if (fragment.style.dim) dim += meaningful.length;
    }
  }
  return total ? dim / total : 1;
}

// Codex draws its inactive composer suggestions in the same full-width panel
// used for submitted prompts. The suggestion is dim, while submitted input is
// bright. Treating both as messages creates false boundaries in the transcript.
export function isTransientComposer(rows) {
  const text = panelText(rows);
  const first = rows.find((row) => row.text.trim())?.text.trimStart() || "";
  const marked = hasPromptMarker(first);
  return !text
    || isDefaultSuggestion(text)
    || (marked && dimCharacterRatio(rows) >= 0.6);
}
