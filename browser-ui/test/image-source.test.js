import assert from "node:assert/strict";
import test from "node:test";

import { resolveMarkdownImageSource } from "../src/image-source.js";

test("keeps HTTP and embedded Markdown image sources unchanged", () => {
  assert.equal(
    resolveMarkdownImageSource("https://example.test/plot.png", "secret"),
    "https://example.test/plot.png",
  );
  assert.equal(
    resolveMarkdownImageSource("http://example.test/plot.png", "secret"),
    "http://example.test/plot.png",
  );
  assert.equal(
    resolveMarkdownImageSource("data:image/png;base64,AA==", "secret"),
    "data:image/png;base64,AA==",
  );
});

test("routes relative Markdown images through the protected session endpoint", () => {
  const resolved = resolveMarkdownImageSource("plots/my chart.png", "a/b token");
  const url = new URL(resolved, "http://127.0.0.1");

  assert.equal(url.pathname, "/session-image");
  assert.equal(url.searchParams.get("token"), "a/b token");
  assert.equal(url.searchParams.get("path"), "plots/my chart.png");
});

test("routes absolute filesystem paths through the protected session endpoint", () => {
  const resolved = resolveMarkdownImageSource("/Users/example/session/plot.png", "secret");
  const url = new URL(resolved, "http://127.0.0.1");

  assert.equal(url.searchParams.get("path"), "/Users/example/session/plot.png");
});
