import assert from "node:assert/strict";
import test from "node:test";

import { isDefaultSuggestion, isTransientComposer } from "../src/codex-chrome.js";

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
