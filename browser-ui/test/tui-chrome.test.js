import assert from "node:assert/strict";
import test from "node:test";

import {
  hasAssistantMarker,
  hasPromptMarker,
  isActiveComposer,
  isClaudePermissionChoice,
  isClaudePermissionPrompt,
  isClaudeStatusLine,
  isCodexStatusLine,
  isDefaultSuggestion,
  isTransientComposer,
  isUserPanel,
  recoverComposerStart,
  rememberPromptBackground,
} from "../src/tui-chrome.js";

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

test("recognizes Claude's animated thinking row as transient chrome", () => {
  assert.equal(
    isClaudeStatusLine("✶ Blanching… (11s · thinking with xhigh effort)"),
    true,
  );
  assert.equal(isClaudeStatusLine("✻ Cogitating… (esc to interrupt)"), true);
  assert.equal(isClaudeStatusLine("⏵⏵ accept edits on (shift+tab to cycle)"), true);
  assert.equal(
    isClaudeStatusLine("Do you want to allow Claude to fetch this content?"),
    true,
  );
  assert.equal(isClaudeStatusLine("✶ This symbol is part of the answer"), false);
});

test("recognizes Codex status lines without enumerating mode keywords", () => {
  assert.equal(isCodexStatusLine("gpt-5.6-sol high · ~/simultex"), true);
  assert.equal(isCodexStatusLine("gpt-5.6-sol default · ~/thingy"), true);
  assert.equal(isCodexStatusLine("• gpt-next future-mode · C:\\workspace"), true);
  assert.equal(isCodexStatusLine("The default path is ~/thingy"), false);
  assert.equal(isCodexStatusLine("gpt-5.6-sol default · ready"), false);
});

test("recognizes Claude's permission prompt and choices without matching prose", () => {
  assert.equal(
    isClaudePermissionPrompt("Do you want to allow Claude to fetch this content?"),
    true,
  );
  assert.equal(isClaudePermissionChoice("1. Yes"), true);
  assert.equal(
    isClaudePermissionChoice("2. No, and tell Claude what to do differently"),
    true,
  );
  assert.equal(isClaudePermissionChoice("1. Yes is a valid answer."), false);
  assert.equal(isClaudePermissionPrompt("Do you want Claude to explain this?"), false);
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

test("submits a bright panel when a newer empty composer has appeared", () => {
  assert.equal(isActiveComposer(false, true, false, true), false);
  assert.equal(isActiveComposer(true, true, false, true), false);
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
