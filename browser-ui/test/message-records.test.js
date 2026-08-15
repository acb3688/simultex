import assert from "node:assert/strict";
import test from "node:test";

import { MessageRecords } from "../src/message-records.js";

function candidate(kind, source, messageRole = kind === "terminal" ? undefined : kind) {
  return {
    key: `vt-row-${source}`,
    kind,
    messageRole,
    source,
    signature: `${kind}:${source}`,
  };
}

test("a completed message is detached permanently from later VT updates", () => {
  const messages = new MessageRecords();

  let records = messages.update([candidate("assistant", "Final: $x + y$")], 0);
  const assistant = records[0];
  assert.equal(assistant.frozen, false);

  records = messages.update([
    candidate("assistant", "Final: $x + y$"),
    candidate("terminal", "›", "user"),
  ], 1);
  assert.strictEqual(records[0], assistant);
  assert.equal(assistant.frozen, true);
  assert.equal(assistant.source, "Final: $x + y$");
  const frozenSignature = assistant.signature;

  records = messages.update([
    candidate("assistant", "CORRUPTED BY A LATER REPAINT"),
    candidate("terminal", "› $a + b$", "user"),
  ], 1);
  assert.strictEqual(records[0], assistant);
  assert.equal(assistant.source, "Final: $x + y$");
  assert.equal(assistant.signature, frozenSignature);
  assert.equal(records[1].source, "› $a + b$");
});

test("only the last message remains mutable", () => {
  const messages = new MessageRecords();
  const records = messages.update([
    candidate("terminal", "Codex"),
    candidate("user", "Question"),
    candidate("assistant", "Streaming"),
  ], 2);

  assert.deepEqual(records.map((record) => record.frozen), [true, true, false]);
  assert.deepEqual(records.map((record) => record.key), [
    "message-1",
    "message-2",
    "message-3",
  ]);
});

test("an active message and trailing terminal chrome stay mutable together", () => {
  const messages = new MessageRecords();
  let records = messages.update([
    candidate("terminal", "Codex splash"),
    candidate("terminal", "› Implement {feature}", "user"),
    candidate("terminal", "model: loading"),
  ], 1);

  assert.deepEqual(records.map((record) => record.frozen), [true, false, false]);
  const input = records[1];

  records = messages.update([
    candidate("terminal", "Codex splash changed by repaint"),
    candidate("terminal", "› $x + y$", "user"),
    candidate("terminal", "model: gpt-5.6"),
  ], 1);

  assert.strictEqual(records[1], input);
  assert.equal(records[1].source, "› $x + y$");
  assert.equal(records[2].source, "model: gpt-5.6");
  assert.equal(records[0].source, "Codex splash");

  records = messages.update([
    candidate("terminal", "Codex splash changed again"),
    candidate("terminal", "› $x + y$", "user"),
  ], 1);
  assert.equal(records.length, 2);
  assert.equal(records[0].source, "Codex splash");
});
