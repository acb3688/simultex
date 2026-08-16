import assert from "node:assert/strict";
import test from "node:test";

import { ApiTranscript } from "../src/api-transcript.js";

function record(kind, source, start, extra = {}) {
  return {
    key: `pty:${start}:${kind}`,
    kind,
    messageRole: kind === "terminal" ? undefined : kind,
    source,
    start,
    end: start,
    signature: `pty:${kind}:${source}`,
    ...extra,
  };
}

function event(type, extra = {}) {
  return {
    version: 1,
    type,
    session_id: "session-1",
    turn_id: "turn-1",
    provider: "openai",
    ...extra,
  };
}

function beginTurn(transcript, turnId, user, ordinal = 1) {
  transcript.accept(event("turn.started", {
    turn_id: turnId,
    ordinal,
    user: { markdown: user },
  }));
}

function completeCall(transcript, turnId, callId, markdown) {
  transcript.accept(event("call.started", { turn_id: turnId, call_id: callId }));
  transcript.accept(event("assistant.delta", {
    turn_id: turnId,
    call_id: callId,
    delta: markdown.slice(0, Math.ceil(markdown.length / 2)),
  }));
  transcript.accept(event("assistant.delta", {
    turn_id: turnId,
    call_id: callId,
    delta: markdown.slice(Math.ceil(markdown.length / 2)),
  }));
  transcript.accept(event("assistant.part.done", {
    turn_id: turnId,
    call_id: callId,
    markdown,
  }));
  transcript.accept(event("call.completed", {
    turn_id: turnId,
    call_id: callId,
    status: "completed",
  }));
}

test("authoritative Markdown replaces matching PTY messages without merging", () => {
  const transcript = new ApiTranscript();
  const exact = String.raw`\[
\begin{bmatrix}
2 & 1\\
0 & 3
\end{bmatrix}
\]`;
  beginTurn(transcript, "turn-1", "Show a matrix");
  completeCall(transcript, "turn-1", "call-1", exact);

  const splash = record("terminal", "Codex", 0);
  const composer = record("terminal", "›", 8, { panel: true, messageRole: "user" });
  const reconciled = transcript.reconcile([
    splash,
    record("user", "Show a matrix", 2),
    record("assistant", String.raw`[ \begin{bmatrix}2 & 1\ \\ 0 & 3\end{bmatrix} ]`, 4),
    composer,
  ]);

  assert.strictEqual(reconciled[0], splash);
  assert.equal(reconciled[1].source, "Show a matrix");
  assert.equal(reconciled[2].source, exact);
  assert.equal(reconciled[2].authoritative, true);
  assert.match(reconciled[2].key, /assistant:call-1$/);
  assert.strictEqual(reconciled[3], composer);
});

test("a proxy user message is inserted when the TUI erased its panel", () => {
  const transcript = new ApiTranscript();
  beginTurn(transcript, "turn-1", "First prompt", 1);
  completeCall(transcript, "turn-1", "call-1", "First answer");
  beginTurn(transcript, "turn-2", "Second prompt", 2);
  completeCall(transcript, "turn-2", "call-2", "Second answer");

  const reconciled = transcript.reconcile([
    record("assistant", "damaged first answer", 2),
    record("user", "Second prompt", 5),
    record("assistant", "damaged second answer", 7),
  ]);

  assert.deepEqual(
    reconciled.map((item) => [item.kind, item.source]),
    [
      ["user", "First prompt"],
      ["assistant", "First answer"],
      ["user", "Second prompt"],
      ["assistant", "Second answer"],
    ],
  );
});

test("assistant calls remain separated around PTY tool UI", () => {
  const transcript = new ApiTranscript();
  beginTurn(transcript, "turn-1", "Inspect it");
  completeCall(transcript, "turn-1", "call-1", "I’ll inspect the file.");
  completeCall(transcript, "turn-1", "call-2", "The result is $x^2$.");
  const tool = record("terminal", "Reading file…", 4);

  const reconciled = transcript.reconcile([
    record("user", "Inspect it", 1),
    record("assistant", "I'll inspect the file.", 2),
    tool,
    record("assistant", "The result is (x^2).", 6),
  ]);

  assert.deepEqual(
    reconciled.map((item) => item.source),
    ["Inspect it", "I’ll inspect the file.", "Reading file…", "The result is $x^2$."],
  );
  assert.strictEqual(reconciled[2], tool);
});

test("PTY assistant content remains the fallback until API text exists", () => {
  const transcript = new ApiTranscript();
  beginTurn(transcript, "turn-1", "Hello");
  transcript.accept(event("call.started", { call_id: "call-1" }));
  transcript.accept(event("call.failed", { call_id: "call-1", error: "cancelled" }));
  const fallback = record("assistant", "Request cancelled", 3);

  const reconciled = transcript.reconcile([
    record("user", "Hello", 1),
    fallback,
  ]);

  assert.equal(reconciled[0].authoritative, true);
  assert.strictEqual(reconciled[1], fallback);
});

test("a streaming API assistant appears before the live composer", () => {
  const transcript = new ApiTranscript();
  beginTurn(transcript, "turn-1", "Explain it");
  transcript.accept(event("call.started", { call_id: "call-1" }));
  transcript.accept(event("assistant.delta", {
    call_id: "call-1",
    delta: "Partial **Markdown**",
  }));
  const composer = record("terminal", "›", 4, { panel: true, messageRole: "user" });

  const reconciled = transcript.reconcile([
    record("user", "Explain it", 1),
    composer,
  ]);

  assert.deepEqual(reconciled.map((item) => item.kind), ["user", "assistant", "terminal"]);
  assert.equal(reconciled[1].source, "Partial **Markdown**");
  assert.equal(reconciled[1].active, true);
  assert.strictEqual(reconciled[2], composer);
});

test("assistant deltas do not invalidate an unchanged authoritative user block", () => {
  const transcript = new ApiTranscript();
  beginTurn(transcript, "turn-1", "Explain it");
  transcript.accept(event("call.started", { call_id: "call-1" }));
  let reconciled = transcript.reconcile([
    record("user", "Explain it", 1),
    record("assistant", "fallback", 2),
  ]);
  const userSignature = reconciled[0].signature;

  transcript.accept(event("assistant.delta", { call_id: "call-1", delta: "A" }));
  reconciled = transcript.reconcile([
    record("user", "Explain it", 1),
    record("assistant", "fallback", 2),
  ]);

  assert.equal(reconciled[0].signature, userSignature);
  assert.equal(reconciled[1].source, "A");
});

test("extra PTY assistant blocks are removed after an authoritative call settles", () => {
  const transcript = new ApiTranscript();
  beginTurn(transcript, "turn-1", "Hello");
  completeCall(transcript, "turn-1", "call-1", "Clean answer");

  const reconciled = transcript.reconcile([
    record("user", "Hello", 1),
    record("assistant", "WebSocket error", 2),
    record("assistant", "damaged answer", 3),
  ]);

  assert.deepEqual(reconciled.map((item) => item.source), ["Hello", "Clean answer"]);
});

test("duplicate PTY copies of an authoritative user message are removed", () => {
  const transcript = new ApiTranscript();
  beginTurn(transcript, "turn-1", "Show the equation");
  completeCall(transcript, "turn-1", "call-1", String.raw`\[x+y\]`);

  const reconciled = transcript.reconcile([
    record("user", "Show the equation", 27),
    record("user", "Show the equation", 27),
    record("assistant", "damaged equation", 30),
  ]);

  assert.deepEqual(
    reconciled.map((item) => [item.kind, item.source]),
    [
      ["user", "Show the equation"],
      ["assistant", String.raw`\[x+y\]`],
    ],
  );
  assert.equal(reconciled[0].authoritative, true);
});

test("an abandoned provider retry does not create a duplicate user turn", () => {
  const transcript = new ApiTranscript();
  const prompt = String.raw`$$\int x^2 \; dx$$`;
  beginTurn(transcript, "turn-1", prompt, 1);
  transcript.accept(event("call.started", {
    turn_id: "turn-1",
    call_id: "call-1",
  }));
  transcript.accept(event("turn.completed", { turn_id: "turn-1" }));
  beginTurn(transcript, "turn-2", prompt, 2);
  completeCall(transcript, "turn-2", "call-2", "`x³/3 + C`");

  const reconciled = transcript.reconcile([
    record("user", prompt, 39),
    record("user", prompt, 39),
    record("assistant", "x³/3 + C", 39),
    record("terminal", "❯", 39, { panel: true, messageRole: "user" }),
  ]);

  assert.deepEqual(
    reconciled.map((item) => [item.kind, item.source]),
    [
      ["user", prompt],
      ["assistant", "`x³/3 + C`"],
      ["terminal", "❯"],
    ],
  );
  assert.equal(reconciled[0].apiTurnId, "turn-2");
});

test("a misclassified LaTeX prompt still produces one stable API turn", () => {
  const transcript = new ApiTranscript();
  const prompt = String.raw`$\int x^2 \; dx$, can you solve it?`;
  const answer = String.raw`\[\int x^2\,dx=\frac{x^3}{3}+C\]`;
  beginTurn(transcript, "turn-1", prompt);
  completeCall(transcript, "turn-1", "call-1", answer);

  const damagedPtyBlock = record("assistant", "$ int x2 dx can you solve it", 52, {
    end: 57,
    panel: true,
    rows: ["terminal-only metadata"],
  });
  const reconciled = transcript.reconcile([damagedPtyBlock]);

  assert.deepEqual(
    reconciled.map((item) => [item.kind, item.source]),
    [
      ["user", prompt],
      ["assistant", answer],
    ],
  );
  assert.deepEqual(
    reconciled.map((item) => item.key),
    [
      "api:session-1:turn-1:user",
      "api:session-1:turn-1:assistant:call-1",
    ],
  );
  assert.equal("panel" in reconciled[0], false);
  assert.equal("rows" in reconciled[0], false);
  assert.equal(reconciled[0].background, undefined);
});

test("duplicate SSE event ids do not duplicate streamed text", () => {
  const transcript = new ApiTranscript();
  beginTurn(transcript, "turn-1", "Hello");
  transcript.accept(event("call.started", { call_id: "call-1" }), "10");
  const delta = event("assistant.delta", { call_id: "call-1", delta: "exact" });
  assert.equal(transcript.accept(delta, "11"), true);
  assert.equal(transcript.accept(delta, "11"), false);

  const reconciled = transcript.reconcile([
    record("user", "Hello", 1),
    record("assistant", "fallback", 2),
  ]);
  assert.equal(reconciled[1].source, "exact");
  assert.equal(transcript.diagnostics().events.length, 3);
});

test("without proxy turns reconciliation returns the PTY records unchanged", () => {
  const transcript = new ApiTranscript();
  const records = [record("assistant", "PTY only", 1)];
  assert.strictEqual(transcript.reconcile(records), records);
});
