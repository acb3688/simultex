import assert from "node:assert/strict";
import test from "node:test";

import {
  mermaidFenceHtml,
  scheduleMermaidDiagrams,
} from "../src/mermaid-support.js";

test("creates an escaped copyable Mermaid placeholder", () => {
  const html = mermaidFenceHtml(
    "flowchart LR\nA[API] --> B<Browser>",
    (_source, kind, label) => `class="${kind}" aria-label="${label}"`,
    (value) => value.replaceAll("<", "&lt;").replaceAll(">", "&gt;"),
  );

  assert.match(html, /copy-block mermaid-diagram/);
  assert.match(html, /data-mermaid-state="pending"/);
  assert.match(html, /B&lt;Browser&gt;/);
  assert.doesNotMatch(html, /B<Browser>/);
});

test("renders each pending Mermaid diagram once", async () => {
  let calls = 0;
  let bound;
  const node = {
    dataset: { copySource: "flowchart LR\nA --> B", mermaidState: "pending" },
    isConnected: true,
    innerHTML: "source",
  };
  const root = {
    querySelectorAll: () => (
      node.dataset.mermaidState === "pending" ? [node] : []
    ),
  };
  const render = async (_id, source) => {
    calls += 1;
    assert.equal(source, "flowchart LR\nA --> B");
    return {
      svg: "<svg>diagram</svg>",
      bindFunctions: (target) => { bound = target; },
    };
  };

  await Promise.all([
    scheduleMermaidDiagrams(root, render),
    scheduleMermaidDiagrams(root, render),
  ]);

  assert.equal(calls, 1);
  assert.equal(node.innerHTML, "<svg>diagram</svg>");
  assert.equal(node.dataset.mermaidState, "rendered");
  assert.strictEqual(bound, node);
});

test("keeps the original source visible when Mermaid rejects a diagram", async () => {
  let replacement;
  let warning;
  const fallback = {};
  const node = {
    dataset: { copySource: "not a diagram", mermaidState: "pending" },
    isConnected: true,
    ownerDocument: { createElement: () => fallback },
    replaceChildren: (child) => { replacement = child; },
  };
  const root = { querySelectorAll: () => [node] };

  await scheduleMermaidDiagrams(
    root,
    async () => { throw new Error("parse failed"); },
    (message, error) => { warning = [message, error.message]; },
  );

  assert.strictEqual(replacement, fallback);
  assert.equal(fallback.className, "mermaid-source mermaid-error");
  assert.equal(fallback.textContent, "not a diagram");
  assert.equal(node.dataset.mermaidState, "failed");
  assert.deepEqual(warning, [
    "Could not render AnyTeX Mermaid diagram",
    "parse failed",
  ]);
});
