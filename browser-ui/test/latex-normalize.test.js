import assert from "node:assert/strict";
import test from "node:test";

import {
  normalizeLatexFence,
  normalizeTerminalMath,
} from "../src/latex-normalize.js";

test("converts standalone align fences to a KaTeX-compatible environment", () => {
  const source = "\\begin{align}\nx&=1 \\\\\ny&=2\n\\end{align}";
  const expected = "\\begin{aligned}\nx&=1 \\\\\ny&=2\n\\end{aligned}";

  assert.equal(normalizeLatexFence(source), expected);
});

test("repairs a collapsed matrix row separator", () => {
  const source = String.raw`2\begin{bmatrix}3 \ 2\end{bmatrix}`;

  assert.equal(
    normalizeTerminalMath(source),
    String.raw`2\begin{bmatrix}3 \\ 2\end{bmatrix}`,
  );
});

test("repairs compact collapsed numeric matrix separators", () => {
  const source = String.raw`\begin{bmatrix}3\2\end{bmatrix} + \begin{bmatrix}1\-4\end{bmatrix}`;

  assert.equal(
    normalizeTerminalMath(source),
    String.raw`\begin{bmatrix}3 \\ 2\end{bmatrix} + \begin{bmatrix}1 \\ -4\end{bmatrix}`,
  );
  assert.equal(
    normalizeTerminalMath(String.raw`\begin{bmatrix}3\alpha\end{bmatrix}`),
    String.raw`\begin{bmatrix}3\alpha\end{bmatrix}`,
  );
});

test("leaves intact matrix separators and ordinary TeX spacing alone", () => {
  const matrix = String.raw`\begin{pmatrix}a \\ b\end{pmatrix}`;
  const compactNumericMatrix = String.raw`\begin{bmatrix}3\\2\end{bmatrix}`;
  assert.equal(normalizeTerminalMath(matrix), matrix);
  assert.equal(normalizeTerminalMath(compactNumericMatrix), compactNumericMatrix);
  assert.equal(normalizeTerminalMath(String.raw`x\ y`), String.raw`x\ y`);
});

test("uses surviving matrix source lines when all separators were consumed", () => {
  const source = "\\begin{bmatrix}\n3\n2\n\\end{bmatrix}";

  assert.equal(
    normalizeTerminalMath(source),
    String.raw`\begin{bmatrix} 3 \\ 2 \end{bmatrix}`,
  );
});

test("removes a surviving single slash before joining matrix source lines", () => {
  const source = "\\begin{bmatrix}\na & b\\\nc & d\n\\end{bmatrix}";

  assert.equal(
    normalizeTerminalMath(source),
    String.raw`\begin{bmatrix} a & b \\ c & d \end{bmatrix}`,
  );
});
