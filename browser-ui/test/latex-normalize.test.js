import assert from "node:assert/strict";
import test from "node:test";

import { normalizeTerminalMath } from "../src/latex-normalize.js";

test("repairs a collapsed matrix row separator", () => {
  const source = String.raw`2\begin{bmatrix}3 \ 2\end{bmatrix}`;

  assert.equal(
    normalizeTerminalMath(source),
    String.raw`2\begin{bmatrix}3 \\ 2\end{bmatrix}`,
  );
});

test("leaves intact matrix separators and ordinary TeX spacing alone", () => {
  const matrix = String.raw`\begin{pmatrix}a \\ b\end{pmatrix}`;
  assert.equal(normalizeTerminalMath(matrix), matrix);
  assert.equal(normalizeTerminalMath(String.raw`x\ y`), String.raw`x\ y`);
});

test("uses surviving matrix source lines when all separators were consumed", () => {
  const source = "\\begin{bmatrix}\n3\n2\n\\end{bmatrix}";

  assert.equal(
    normalizeTerminalMath(source),
    String.raw`\begin{bmatrix} 3 \\ 2 \end{bmatrix}`,
  );
});
