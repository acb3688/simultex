import assert from "node:assert/strict";
import test from "node:test";

import {
  hasAssistantMarker,
  hasPromptMarker,
  isActiveComposer,
  isDefaultSuggestion,
  isTransientComposer,
  isUserPanel,
  recoverComposerStart,
  rememberPromptBackground,
} from "../src/codex-chrome.js";

function row(text, dim = false) {
  return {
    text,
    fragments: [{ text, style: { dim } }],
  };
}

test("recognizes Codex's built-in composer suggestions", () => {
  assert.equal(isDefaultSuggestion("Explain this codebase"), true);
  assert.equal(isDefaultSuggestion("Implement {feature}"), true);
  assert.equal(isDefaultSuggestion("Prove the matrix identity"), false);
});

test("recognizes Claude's composer suggestion and message markers", () => {
  assert.equal(isDefaultSuggestion('Try "how does cli.py work?"'), true);
  assert.equal(hasPromptMarker("❯ Explain this codebase"), true);
  assert.equal(hasAssistantMarker("⏺ Here is the answer"), true);
});

test("does not mistake a Markdown blockquote for a user prompt", () => {
  assert.equal(hasPromptMarker("> Vectors represent things"), false);
});

test("dim and empty composer panels are transient", () => {
  assert.equal(isTransientComposer([row("› Explain something", true)]), true);
  assert.equal(isTransientComposer([row("")]), true);
});

test("dim assistant code is not mistaken for a composer", () => {
  assert.equal(isTransientComposer([row("# an intentionally dim comment", true)]), false);
});

test("bright submitted prompts are conversation messages", () => {
  assert.equal(isTransientComposer([row("› Render this matrix", false)]), false);
});

test("recognizes historical prompts after Codex removes their marker", () => {
  const promptBackgrounds = new Set(["#444955"]);

  assert.equal(isUserPanel(false, "#444955", promptBackgrounds), true);
  assert.equal(isUserPanel(false, "#1b1f26", promptBackgrounds), false);
});

test("learns the prompt background from a real marked prompt", () => {
  const promptBackgrounds = new Set();

  rememberPromptBackground(true, false, "#444955", "default", promptBackgrounds);

  assert.equal(isUserPanel(false, "#444955", promptBackgrounds), true);
});

test("keeps the latest composer active while a completion overlay owns the cursor", () => {
  assert.equal(isActiveComposer(false, true, false), true);
  assert.equal(isActiveComposer(false, true, true), false);
});

test("keeps a composer active while its own panel owns the cursor", () => {
  assert.equal(isActiveComposer(true, true, true), true);
});

test("recovers a visible unanswered composer before the frozen row watermark", () => {
  const ranges = [
    { start: 20, end: 23, marker: true },
    { start: 36, end: 39, marker: true },
  ];

  assert.equal(recoverComposerStart(43, ranges, () => false), 36);
});

test("does not rewind to a submitted panel or an unmarked panel", () => {
  assert.equal(
    recoverComposerStart(43, [{ start: 36, end: 39, marker: true }], () => true),
    43,
  );
  assert.equal(
    recoverComposerStart(43, [{ start: 36, end: 39, marker: false }], () => false),
    43,
  );
});
