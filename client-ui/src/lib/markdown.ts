import DOMPurify from "dompurify";
import { marked } from "marked";

marked.setOptions({
  breaks: true,
  gfm: true,
});

/** Renders a full markdown block (paragraphs, lists) to sanitized HTML. */
export function renderMarkdownBlock(value: string): string {
  return DOMPurify.sanitize(marked.parse(value) as string);
}

/** Renders a single line of markdown (inline code, emphasis) with no wrapping <p>. */
export function renderMarkdownInline(value: string): string {
  return DOMPurify.sanitize(marked.parseInline(value) as string);
}
