const MATRIX_ENVIRONMENT = /\\begin\{(matrix|pmatrix|bmatrix|Bmatrix|vmatrix|Vmatrix)\}([\s\S]*?)\\end\{\1\}/g;

// Terminal Markdown renderers commonly collapse TeX's `\\` row separator to
// `\ ` (a control-space). Inside a matrix this is unambiguous enough to repair:
// a non-space cell followed by `\` + whitespace + another cell is a row break.
export function normalizeTerminalMath(math) {
  return math.replace(MATRIX_ENVIRONMENT, (environment, name, body) => {
    let repaired = body.replace(/(\S)[ \t]+\\\s+(?=\S)/g, "$1 \\\\ ");
    const lines = repaired.split("\n").map((line) => line.trim()).filter(Boolean);
    if (lines.length > 1 && !repaired.includes("\\\\")) {
      repaired = ` ${lines.join(" \\\\ ")} `;
    }
    return `\\begin{${name}}${repaired}\\end{${name}}`;
  });
}
