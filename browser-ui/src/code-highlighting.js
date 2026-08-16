import hljs from "highlight.js/lib/core";
import bash from "highlight.js/lib/languages/bash";
import c from "highlight.js/lib/languages/c";
import cpp from "highlight.js/lib/languages/cpp";
import csharp from "highlight.js/lib/languages/csharp";
import css from "highlight.js/lib/languages/css";
import diff from "highlight.js/lib/languages/diff";
import dockerfile from "highlight.js/lib/languages/dockerfile";
import go from "highlight.js/lib/languages/go";
import java from "highlight.js/lib/languages/java";
import javascript from "highlight.js/lib/languages/javascript";
import json from "highlight.js/lib/languages/json";
import kotlin from "highlight.js/lib/languages/kotlin";
import markdown from "highlight.js/lib/languages/markdown";
import php from "highlight.js/lib/languages/php";
import python from "highlight.js/lib/languages/python";
import ruby from "highlight.js/lib/languages/ruby";
import rust from "highlight.js/lib/languages/rust";
import sql from "highlight.js/lib/languages/sql";
import swift from "highlight.js/lib/languages/swift";
import typescript from "highlight.js/lib/languages/typescript";
import xml from "highlight.js/lib/languages/xml";
import yaml from "highlight.js/lib/languages/yaml";

const languages = {
  bash,
  c,
  cpp,
  csharp,
  css,
  diff,
  dockerfile,
  go,
  java,
  javascript,
  json,
  kotlin,
  markdown,
  php,
  python,
  ruby,
  rust,
  sql,
  swift,
  typescript,
  xml,
  yaml,
};

for (const [name, definition] of Object.entries(languages)) {
  hljs.registerLanguage(name, definition);
}

const aliases = new Map([
  ["c++", "cpp"],
  ["c#", "csharp"],
  ["console", "bash"],
  ["docker", "dockerfile"],
  ["html", "xml"],
  ["language-html", "xml"],
  ["language-js", "javascript"],
  ["language-javascript", "javascript"],
  ["language-python", "python"],
  ["language-ts", "typescript"],
  ["language-typescript", "typescript"],
  ["patch", "diff"],
  ["shell", "bash"],
  ["svg", "xml"],
]);

function resolveLanguage(info) {
  const requested = String(info || "").trim().split(/\s+/, 1)[0].toLowerCase();
  return aliases.get(requested) || requested;
}

export function highlightCode(source, info) {
  const language = resolveLanguage(info);
  if (!language || !hljs.getLanguage(language)) return "";
  try {
    return hljs.highlight(source, { language, ignoreIllegals: true }).value;
  } catch {
    return "";
  }
}
