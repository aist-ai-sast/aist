import DOMPurify from "dompurify";
import { marked } from "marked";

type DescriptionBlockProps = {
  value?: string | null;
};

marked.setOptions({
  breaks: true,
  gfm: true,
});

export default function DescriptionBlock({ value }: DescriptionBlockProps) {
  if (!value) {
    return <div className="text-sm text-slate-400">No description provided.</div>;
  }

  const html = DOMPurify.sanitize(marked.parse(value));
  return (
    <div
      className="aist-markdown text-sm text-slate-200"
      dangerouslySetInnerHTML={{ __html: html }}
    />
  );
}
