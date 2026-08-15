import assert from "node:assert/strict";
import test from "node:test";

import { copyText, SNAPSHOT_COPY_SCRIPT } from "../src/copy-source.js";

test("uses the Clipboard API when available", async () => {
  const writes = [];
  const navigatorObject = {
    clipboard: {
      writeText: async (value) => writes.push(value),
    },
  };

  await copyText("exact `source`", navigatorObject, undefined);

  assert.deepEqual(writes, ["exact `source`"]);
});

test("falls back to a temporary textarea for local snapshots", async () => {
  const actions = [];
  const textarea = {
    style: {},
    select: () => actions.push("select"),
    setSelectionRange: (start, end) => actions.push([start, end]),
    remove: () => actions.push("remove"),
  };
  const documentObject = {
    body: { append: (node) => actions.push(["append", node]) },
    createElement: () => textarea,
    execCommand: (command) => {
      actions.push(command);
      return true;
    },
  };

  await copyText("TeX", {}, documentObject);

  assert.equal(textarea.value, "TeX");
  assert.equal(textarea.readOnly, true);
  assert.deepEqual(actions, [
    ["append", textarea],
    "select",
    [0, 3],
    "copy",
    "remove",
  ]);
});

test("snapshot copy runtime is self-contained and script-safe", () => {
  assert.match(SNAPSHOT_COPY_SCRIPT, /copy-region/);
  assert.match(SNAPSHOT_COPY_SCRIPT, /execCommand/);
  assert.doesNotMatch(SNAPSHOT_COPY_SCRIPT, /<\/script>/i);
});
