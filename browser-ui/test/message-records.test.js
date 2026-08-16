import assert from "node:assert/strict";
import test from "node:test";

import { MessageRecords } from "../src/message-records.js";

function candidate(kind, source, start, end = start, messageRole) {
  return {
    key: `vt-row-${start}-${kind}`,
    kind,
    messageRole: messageRole ?? (kind === "terminal" ? undefined : kind),
    source,
    start,
    end,
    signature: `${kind}:${source}`,
  };
}

test("committed messages advance the row watermark and detach from VT updates", () => {
  const messages = new MessageRecords();

  let records = messages.update([
    candidate("assistant", "Final: $x + y$", 10, 14),
  ], 0);
  const assistant = records[0];
  assert.equal(assistant.frozen, false);

  records = messages.update([
    candidate("assistant", "Final: $x + y$", 10, 14),
    candidate("terminal", "›", 16, 17, "user"),
  ], 1, 0);
  assert.equal(assistant.frozen, false);
  records = messages.update([
    candidate("assistant", "Final: $x + y$", 10, 14),
    candidate("terminal", "›", 16, 17, "user"),
  ], 1, 121);
  assert.strictEqual(records[0], assistant);
  assert.equal(assistant.frozen, true);
  assert.equal(messages.startRow, 15);

  // The caller now reconstructs only rows at or after startRow, so a later VT
  // repaint cannot even present the old assistant as a candidate.
  records = messages.update([
    candidate("terminal", "› $a + b$", 16, 17, "user"),
  ], 0);
  assert.strictEqual(records[0], assistant);
  assert.equal(records[0].source, "Final: $x + y$");
  assert.equal(records[1].source, "› $a + b$");
});

test("a shrinking live tail keeps updating after a submitted user message", () => {
  const messages = new MessageRecords();
  let records = messages.update([
    candidate("user", "Explain determinants", 20, 22),
    candidate("assistant", "Working", 24),
    candidate("terminal", "model status", 26),
  ], 1, 0);

  assert.equal(messages.startRow, 20);
  assert.equal(records[0].frozen, true);
  const response = records[1];
  assert.equal(response.source, "Working");

  records = messages.update([
    candidate("assistant", "A determinant measures signed volume.", 24, 30),
  ], 0);

  assert.strictEqual(records[1], response);
  assert.equal(records.length, 2);
  assert.equal(records[1].source, "A determinant measures signed volume.");
  assert.equal(records[1].frozen, false);
});

test("an active message and trailing terminal chrome stay mutable together", () => {
  const messages = new MessageRecords();
  let records = messages.update([
    candidate("terminal", "Codex splash", 0, 2),
    candidate("terminal", "› Implement {feature}", 4, 5, "user"),
    candidate("terminal", "model: loading", 7),
  ], 1, 0);
  records = messages.update([
    candidate("terminal", "Codex splash", 0, 2),
    candidate("terminal", "› Implement {feature}", 4, 5, "user"),
    candidate("terminal", "model: loading", 7),
  ], 1, 121);

  assert.deepEqual(records.map((record) => record.frozen), [true, false, false]);
  assert.equal(messages.startRow, 3);
  const input = records[1];

  records = messages.update([
    candidate("terminal", "› $x + y$", 4, 5, "user"),
    candidate("terminal", "model: gpt-5.6", 7),
  ], 0);

  assert.strictEqual(records[1], input);
  assert.equal(records[1].source, "› $x + y$");
  assert.equal(records[2].source, "model: gpt-5.6");
});

test("reused live records discard fields absent from their new candidate", () => {
  const messages = new MessageRecords();

  messages.update([
    candidate("terminal", "1. Yes", 20, 20, "user"),
  ], 0);
  const replacement = candidate("terminal", undefined, 20);
  delete replacement.source;
  const records = messages.update([
    replacement,
  ], 0);

  assert.equal("source" in records[0], false);
  assert.equal("messageRole" in records[0], true);
  assert.equal(records[0].messageRole, undefined);
});

test("a repaint resets the freeze window before a response is committed", () => {
  const messages = new MessageRecords();

  messages.update([
    candidate("assistant", "[partial matrix]", 10, 14),
    candidate("terminal", "› Next question", 16, 17, "user"),
  ], 1, 0);
  let records = messages.update([
    candidate("assistant", "Opening sentence\n[complete matrix]", 10, 14),
    candidate("terminal", "› Next question", 16, 17, "user"),
  ], 1, 121);

  assert.equal(records[0].frozen, false);
  assert.match(records[0].source, /^Opening sentence/);

  records = messages.update([
    candidate("assistant", "Opening sentence\n[complete matrix]", 10, 14),
    candidate("terminal", "› Next question", 16, 17, "user"),
  ], 1, 242);
  assert.equal(records[0].frozen, true);
  assert.match(records[0].source, /^Opening sentence/);
});

test("a submitted user message is committed before its rows are repainted", () => {
  const messages = new MessageRecords();

  const recordsBeforeSubmit = messages.update([
    candidate("assistant", "Earlier response", 10, 19),
    candidate("terminal", "Explain determinants", 20, 22, "user"),
  ], 0);
  const response = recordsBeforeSubmit[0];
  const draft = recordsBeforeSubmit[1];

  const recordsOnSubmit = messages.update([
    candidate("assistant", "Earlier response", 10, 19),
    candidate("user", "Explain determinants", 20, 22),
    candidate("terminal", "› Implement {feature}", 20, 22),
  ], 2, 0);

  assert.strictEqual(recordsOnSubmit[0], response);
  assert.strictEqual(recordsOnSubmit[1], draft);
  assert.deepEqual(recordsOnSubmit.slice(0, 2).map((record) => record.frozen), [true, true]);
  assert.deepEqual(recordsOnSubmit.slice(0, 2).map((record) => record.kind), ["assistant", "user"]);
  assert.equal(recordsOnSubmit[1].source, "Explain determinants");
  assert.equal(messages.startRow, 20);

  const recordsWhileStillPainted = messages.update([
    candidate("user", "Explain determinants", 20, 22),
    candidate("terminal", "› Implement {feature}", 20, 22),
  ], 1, 20);
  assert.deepEqual(
    recordsWhileStillPainted.filter((record) => record.kind === "user")
      .map((record) => record.source),
    ["Explain determinants"],
  );

  const recordsAfterRepaint = messages.update([
    candidate("assistant", "A determinant measures signed volume.", 20, 25),
  ], 0);
  assert.strictEqual(recordsAfterRepaint[0], response);
  assert.strictEqual(recordsAfterRepaint[1], draft);
  assert.equal(recordsAfterRepaint[2].kind, "assistant");
});
