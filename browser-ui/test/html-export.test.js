import assert from "node:assert/strict";
import test from "node:test";

import {
  inlineCssAssets,
  inlineDocumentImages,
  serializeApiDiagnostics,
  snapshotFilename,
} from "../src/html-export.js";

test("creates a filesystem-safe timestamped snapshot filename", () => {
  const now = new Date("2026-08-15T04:30:12.345Z");
  assert.equal(snapshotFilename(now), "simultex-transcript-20260815T043012Z.html");
});

test("embeds stylesheet assets and leaves data URLs untouched", async () => {
  const requested = [];
  const css = '@font-face { src: url("font.woff2") } .icon { background: url(data:image/png;base64,AA==) }';
  const result = await inlineCssAssets(css, "http://127.0.0.1:8000/app.css", async (url) => {
    requested.push(url);
    return new Response(new Uint8Array([0, 1, 2]), {
      headers: { "content-type": "font/woff2" },
    });
  });

  assert.deepEqual(requested, ["http://127.0.0.1:8000/font.woff2"]);
  assert.match(result, /url\("data:font\/woff2;base64,AAEC"\)/);
  assert.match(result, /url\(data:image\/png;base64,AA==\)/);
});

test("uses an absolute asset URL when embedding fails", async () => {
  const result = await inlineCssAssets(
    ".missing { src: url(missing.woff) }",
    "http://127.0.0.1:8000/assets/app.css",
    async () => { throw new Error("offline"); },
  );

  assert.equal(
    result,
    '.missing { src: url("http://127.0.0.1:8000/assets/missing.woff") }',
  );
});

test("embeds loaded transcript images in downloaded HTML", async () => {
  const attributes = new Map([["src", "/session-image?token=x&path=plot.png"]]);
  const original = {
    currentSrc: "http://127.0.0.1:8000/session-image?token=x&path=plot.png",
  };
  const copy = {
    getAttribute: (name) => attributes.get(name),
    setAttribute: (name, value) => attributes.set(name, value),
    removeAttribute: (name) => attributes.delete(name),
  };
  const document = { querySelectorAll: () => [original] };
  const clone = { querySelectorAll: () => [copy] };

  await inlineDocumentImages(document, clone, async () => new Response(
    new Uint8Array([0, 1, 2]),
    { headers: { "content-type": "image/png" } },
  ));

  assert.equal(attributes.get("src"), "data:image/png;base64,AAEC");
});

test("serializes API diagnostics without creating an executable script terminator", () => {
  const serialized = serializeApiDiagnostics({
    version: 1,
    events: [{ event: { type: "assistant.delta", delta: "</script>&" } }],
  });

  assert.doesNotMatch(serialized, /<\/script>/i);
  assert.deepEqual(JSON.parse(serialized).events[0].event.delta, "</script>&");
});
