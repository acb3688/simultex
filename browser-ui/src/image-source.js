export function resolveMarkdownImageSource(source, token) {
  const value = String(source || "");
  if (!value || /^(?:https?:|data:|blob:)/i.test(value) || value.startsWith("//")) {
    return value;
  }

  const query = new URLSearchParams({ token: token || "", path: value });
  return `/session-image?${query.toString()}`;
}
