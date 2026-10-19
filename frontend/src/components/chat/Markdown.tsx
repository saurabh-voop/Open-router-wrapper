"use client";

import { memo, useRef, useState, type ComponentPropsWithoutRef } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import rehypeHighlight from "rehype-highlight";
import remarkGfm from "remark-gfm";
import { Icon } from "../ui";

function CodeBlock({ children, node: _node, ...props }: ComponentPropsWithoutRef<"pre"> & { node?: unknown }) {
  void _node;
  const ref = useRef<HTMLPreElement>(null);
  const [copied, setCopied] = useState(false);

  // The language comes from the <code class="language-x"> child.
  let language = "";
  const child = Array.isArray(children) ? children[0] : children;
  if (child && typeof child === "object" && "props" in child) {
    const cls = (child.props as { className?: string }).className ?? "";
    language = /language-([\w+-]+)/.exec(cls)?.[1] ?? "";
  }

  async function copy() {
    const text = ref.current?.innerText ?? "";
    try {
      await navigator.clipboard.writeText(text.replace(/\n$/, ""));
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard may be blocked (e.g. insecure context); nothing to do.
    }
  }

  return (
    <div className="group my-2 overflow-hidden rounded-lg border border-border bg-code">
      <div className="flex h-7 items-center justify-between border-b border-border px-2.5 text-[11px] text-muted">
        <span className="font-mono">{language || "code"}</span>
        <button
          type="button"
          onClick={copy}
          className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 hover:bg-surface-2 hover:text-foreground"
          aria-label="Copy code"
        >
          {copied ? <Icon.Check width={12} height={12} /> : <Icon.Copy width={12} height={12} />}
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
      <pre ref={ref} {...props} className="!my-0">
        {children}
      </pre>
    </div>
  );
}

const components: Components = {
  pre: CodeBlock,
  a: ({ node: _node, ...props }) => {
    void _node;
    return <a {...props} target="_blank" rel="noopener noreferrer" />;
  },
};

const remarkPlugins = [remarkGfm];
const rehypePlugins = [rehypeHighlight];

export const Markdown = memo(function Markdown({ text }: { text: string }) {
  return (
    <div className="markdown">
      <ReactMarkdown remarkPlugins={remarkPlugins} rehypePlugins={rehypePlugins} components={components}>
        {text}
      </ReactMarkdown>
    </div>
  );
});
