import assert from "node:assert/strict";
import test from "node:test";

import { highlightCode } from "../src/code-highlighting.js";

test("highlights explicitly labeled Python source", () => {
  const result = highlightCode('def greet(name):\n    return f"Hello, {name}"\n', "python");

  assert.match(result, /hljs-keyword/);
  assert.match(result, /hljs-title function_/);
  assert.match(result, /hljs-string/);
});

test("recognizes common fence aliases", () => {
  assert.match(highlightCode("console.log('hi')", "js"), /hljs-variable/);
  assert.match(highlightCode("<main>Hi</main>", "html"), /hljs-tag/);
  assert.match(highlightCode("+added", "patch"), /hljs-addition/);
});

test("leaves unknown and unlabeled fences to Markdown's escaped fallback", () => {
  assert.equal(highlightCode("<unsafe>", ""), "");
  assert.equal(highlightCode("<unsafe>", "made-up-language"), "");
});
