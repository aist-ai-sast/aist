import { renderMarkdownBlock } from "../lib/markdown";

type DescriptionBlockProps = {
  value?: string | null;
};

export default function DescriptionBlock({ value }: DescriptionBlockProps) {
  if (!value) {
    return <div className="text-sm text-slate-400">No description provided.</div>;
  }

  const html = renderMarkdownBlock(value);
  return (
    <div
      className="aist-markdown text-sm text-slate-200"
      dangerouslySetInnerHTML={{ __html: html }}
    />
  );
}
