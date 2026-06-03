import { useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Spinner } from "./Spinner";

// Tailwind has no typography plugin here, so style each element explicitly.
const COMPONENTS = {
  h1: (props: any) => (
    <h3 className="mb-2 mt-4 text-sm font-semibold text-zinc-800" {...props} />
  ),
  h2: (props: any) => (
    <h4 className="mb-1 mt-4 text-xs font-semibold uppercase tracking-wide text-zinc-700" {...props} />
  ),
  p: (props: any) => <p className="mb-2.5 leading-relaxed" {...props} />,
  ul: (props: any) => <ul className="mb-2.5 list-disc space-y-1 pl-5" {...props} />,
  ol: (props: any) => <ol className="mb-2.5 list-decimal space-y-1 pl-5" {...props} />,
  li: (props: any) => <li className="leading-relaxed" {...props} />,
  a: (props: any) => (
    <a
      className="text-primary-600 underline underline-offset-2 hover:text-primary-700"
      target="_blank"
      rel="noopener noreferrer"
      {...props}
    />
  ),
  strong: (props: any) => <strong className="font-semibold text-zinc-700" {...props} />,
  em: (props: any) => <em className="text-zinc-500" {...props} />,
  code: (props: any) => (
    <code className="rounded bg-zinc-100 px-1 py-0.5 font-mono text-[0.7rem] text-zinc-700" {...props} />
  ),
  table: (props: any) => (
    <div className="mb-3 overflow-x-auto">
      <table className="w-full border-collapse text-left" {...props} />
    </div>
  ),
  th: (props: any) => (
    <th className="border border-zinc-200 bg-zinc-50 px-2 py-1.5 font-semibold text-zinc-700" {...props} />
  ),
  td: (props: any) => (
    <td className="border border-zinc-200 px-2 py-1.5 align-top" {...props} />
  ),
};

interface MarkdownDocProps {
  /** URL of the markdown file to fetch and render. */
  src: string;
  /** Human label used in the loading/error messages. */
  title: string;
}

/** Fetches a markdown file and renders it with GFM support (tables, etc.). */
export function MarkdownDoc({ src, title }: MarkdownDocProps) {
  const [text, setText] = useState<string | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setText(null);
    setError(false);
    fetch(src)
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.text();
      })
      .then((body) => {
        if (!cancelled) setText(body);
      })
      .catch(() => {
        if (!cancelled) setError(true);
      });
    return () => {
      cancelled = true;
    };
  }, [src]);

  if (error) {
    return (
      <p className="text-xs text-red-600">
        Couldn&apos;t load the {title}. You can also read it at{" "}
        <a
          href={src}
          target="_blank"
          rel="noopener noreferrer"
          className="underline underline-offset-2"
        >
          this link
        </a>
        .
      </p>
    );
  }

  if (text === null) {
    return (
      <div className="flex items-center gap-2 text-xs text-zinc-400">
        <Spinner className="h-3.5 w-3.5" label={`Loading ${title}`} />
        Loading {title}…
      </div>
    );
  }

  return (
    <div className="text-xs text-zinc-500">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={COMPONENTS}>
        {text}
      </ReactMarkdown>
    </div>
  );
}
