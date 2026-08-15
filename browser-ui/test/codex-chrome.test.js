import assert from "node:assert/strict";
import test from "node:test";

import {
  hasAssistantMarker,
  hasPromptMarker,
  isDefaultSuggestion,
  isTransientComposer,
  isUserPanel,
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
