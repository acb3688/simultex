let diagramNumber = 0;
let renderQueue = Promise.resolve();

export function mermaidFenceHtml(source, copyAttributes, escapeHtml) {
  return `<div ${copyAttributes(
    source,
    "copy-block mermaid-diagram",
    "Copy Mermaid source",
  )} data-mermaid-state="pending"><pre class="mermaid-source">${
    escapeHtml(source)
  }</pre></div>\n`;
}

async function renderClaimed(nodes, render, onError) {
  for (const node of nodes) {
    const source = node.dataset.copySource || "";
    try {
      diagramNumber += 1;
      const result = await render(`simultex-mermaid-${diagramNumber}`, source);
      if (!node.isConnected) continue;
      node.innerHTML = result.svg;
      node.dataset.mermaidState = "rendered";
      result.bindFunctions?.(node);
    } catch (error) {
      if (!node.isConnected) continue;
      const fallback = node.ownerDocument.createElement("pre");
      fallback.className = "mermaid-source mermaid-error";
      fallback.textContent = source;
      node.replaceChildren(fallback);
      node.dataset.mermaidState = "failed";
      onError("Could not render SimulTeX Mermaid diagram", error);
    }
  }
}

export function scheduleMermaidDiagrams(
  root,
  render,
  onError = console.warn,
) {
  const nodes = [...root.querySelectorAll(
    '.mermaid-diagram[data-mermaid-state="pending"]',
  )];
  for (const node of nodes) node.dataset.mermaidState = "queued";
  if (!nodes.length) return renderQueue;
  renderQueue = renderQueue.then(() => renderClaimed(nodes, render, onError));
  return renderQueue;
}
