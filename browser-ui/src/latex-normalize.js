const MATRIX_ENVIRONMENT = /\\begin\{(matrix|pmatrix|bmatrix|Bmatrix|vmatrix|Vmatrix)\}([\s\S]*?)\\end\{\1\}/g;

export function normalizeLatexFence(math) {
  return math
    .replace(/\\begin\{align\*?\}/g, "\\begin{aligned}")
    .replace(/\\end\{align\*?\}/g, "\\end{aligned}");
}

// Terminal Markdown renderers commonly collapse TeX's `\\` row separator to
// `\ ` (a control-space). Inside a matrix this is unambiguous enough to repair:
// a non-space cell followed by `\` + whitespace + another cell is a row break.
export function normalizeTerminalMath(math) {
  return math.replace(MATRIX_ENVIRONMENT, (environment, name, body) => {
    let repaired = body.replace(/(\S)[ \t]+\\\s+(?=\S)/g, "$1 \\\\ ");
    // Compact numeric matrices can lose the same slash without retaining any
    // whitespace, e.g. `3\2` or `1\-4`. Restrict this repair to numeric/sign
    // cell starts so legitimate commands such as `3\alpha` remain untouched.
    repaired = repaired.replace(/([^\\\s])\\(?=[+-]?(?:\d|\.\d))/g, "$1 \\\\ ");
    let lines = repaired.split("\n").map((line) => line.trim()).filter(Boolean);
    if (lines.length > 1 && !repaired.includes("\\\\")) {
      lines = lines.map((line, index) => (
        index < lines.length - 1 ? line.replace(/\\\s*$/, "").trimEnd() : line
      ));
      repaired = ` ${lines.join(" \\\\ ")} `;
    }
    return `\\begin{${name}}${repaired}\\end{${name}}`;
  });
}
