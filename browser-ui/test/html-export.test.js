import assert from "node:assert/strict";
import test from "node:test";

import { inlineCssAssets, snapshotFilename } from "../src/html-export.js";

test("creates a filesystem-safe timestamped snapshot filename", () => {
  const now = new Date("2026-08-15T04:30:12.345Z");
  assert.equal(snapshotFilename(now), "anytex-transcript-20260815T043012Z.html");
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
