import type { ReactNode } from "react";

import {
  RESTRICTED_MARKDOWN_LIMITS,
  safeArtifactLinkHref,
} from "./restrictedMarkdownSafety";

const HEADING = /^(#{1,6})[ \t]+(.+)$/u;
const ORDERED_ITEM = /^[ \t]*\d+[.)][ \t]+(.+)$/u;
const UNORDERED_ITEM = /^[ \t]*[-*+][ \t]+(.+)$/u;

interface InlinePart {
  readonly kind: "code" | "link" | "removed-link" | "text";
  readonly href: string | null;
  readonly text: string;
}

function appendText(parts: InlinePart[], text: string): void {
  if (text.length === 0) return;
  const previous = parts.at(-1);
  if (previous?.kind === "text") {
    parts[parts.length - 1] = {
      kind: "text",
      href: null,
      text: `${previous.text}${text}`,
    };
  } else {
    parts.push({ kind: "text", href: null, text });
  }
}

function parseInlineMarkdown(source: string): readonly InlinePart[] {
  const parts: InlinePart[] = [];
  let cursor = 0;

  while (
    cursor < source.length &&
    parts.length < RESTRICTED_MARKDOWN_LIMITS.maxInlinePartsPerBlock
  ) {
    const codeStart = source.indexOf("`", cursor);
    const imageStart = source.indexOf("![", cursor);
    const linkStart = source.indexOf("[", cursor);
    const candidates = [codeStart, imageStart, linkStart].filter(
      (position) => position >= cursor,
    );
    const next = candidates.length === 0 ? -1 : Math.min(...candidates);

    if (next === -1) {
      appendText(parts, source.slice(cursor));
      cursor = source.length;
      break;
    }
    appendText(parts, source.slice(cursor, next));

    if (next === codeStart) {
      const end = source.indexOf("`", next + 1);
      if (end === -1) {
        appendText(parts, source.slice(next));
        cursor = source.length;
      } else {
        parts.push({
          kind: "code",
          href: null,
          text: source.slice(next + 1, end),
        });
        cursor = end + 1;
      }
      continue;
    }

    const labelStart = next + (next === imageStart ? 2 : 1);
    const labelEnd = source.indexOf("](", labelStart);
    if (labelEnd === -1) {
      appendText(parts, source.slice(next, next + 1));
      cursor = next + 1;
      continue;
    }
    const destinationEnd = source.indexOf(")", labelEnd + 2);
    if (destinationEnd === -1) {
      appendText(parts, source.slice(next, next + 1));
      cursor = next + 1;
      continue;
    }

    const label = source.slice(labelStart, labelEnd);
    const destination = source.slice(labelEnd + 2, destinationEnd);
    if (next === imageStart) {
      appendText(parts, `[Image omitted: ${label}]`);
    } else {
      const href = safeArtifactLinkHref(destination);
      parts.push({
        kind: href === null ? "removed-link" : "link",
        href,
        text: label,
      });
    }
    cursor = destinationEnd + 1;
  }

  if (cursor < source.length) {
    appendText(parts, " [Content omitted: Markdown inline limit reached]");
  }
  return parts;
}

function InlineMarkdown({
  source,
}: {
  readonly source: string;
}): React.JSX.Element {
  return (
    <>
      {parseInlineMarkdown(source).map((part, index) => {
        const key = `${String(index)}-${part.kind}`;
        if (part.kind === "code") return <code key={key}>{part.text}</code>;
        if (part.kind === "link" && part.href !== null) {
          return (
            <a
              key={key}
              href={part.href}
              target="_blank"
              rel="noopener noreferrer"
              referrerPolicy="no-referrer"
            >
              {part.text}
            </a>
          );
        }
        if (part.kind === "removed-link") {
          return (
            <span key={key} className="artifact-markdown__removed-link">
              {part.text} <span>(unsafe link removed)</span>
            </span>
          );
        }
        return <span key={key}>{part.text}</span>;
      })}
    </>
  );
}

function isBlockStart(line: string): boolean {
  return (
    line.startsWith("```") ||
    HEADING.test(line) ||
    ORDERED_ITEM.test(line) ||
    UNORDERED_ITEM.test(line) ||
    line.startsWith("> ")
  );
}

function readList(
  lines: readonly string[],
  start: number,
  pattern: RegExp,
): { readonly items: readonly string[]; readonly next: number } {
  const items: string[] = [];
  let cursor = start;
  while (
    cursor < lines.length &&
    items.length < RESTRICTED_MARKDOWN_LIMITS.maxBlocks
  ) {
    const match = pattern.exec(lines[cursor] ?? "");
    if (match === null) break;
    items.push(match[1] ?? "");
    cursor += 1;
  }
  return { items, next: cursor };
}

function renderMarkdownBlocks(source: string): readonly ReactNode[] {
  const lines = source
    .replaceAll("\r\n", "\n")
    .replaceAll("\r", "\n")
    .split("\n");
  const blocks: ReactNode[] = [];
  let cursor = 0;

  while (
    cursor < lines.length &&
    blocks.length < RESTRICTED_MARKDOWN_LIMITS.maxBlocks
  ) {
    const line = lines[cursor] ?? "";
    if (line.trim().length === 0) {
      cursor += 1;
      continue;
    }
    if (line.startsWith("```")) {
      const code: string[] = [];
      cursor += 1;
      while (
        cursor < lines.length &&
        !(lines[cursor] ?? "").startsWith("```")
      ) {
        code.push(lines[cursor] ?? "");
        cursor += 1;
      }
      if (cursor < lines.length) cursor += 1;
      blocks.push(
        <pre
          key={`code-${String(cursor)}`}
          className="artifact-markdown__code-block"
          aria-label="Artifact Markdown code block"
          tabIndex={0}
        >
          <code>{code.join("\n")}</code>
        </pre>,
      );
      continue;
    }

    const heading = HEADING.exec(line);
    if (heading !== null) {
      const level = heading[1]?.length ?? 1;
      const content = <InlineMarkdown source={heading[2] ?? ""} />;
      const key = `heading-${String(cursor)}`;
      blocks.push(
        level === 1 ? (
          <h2 key={key}>{content}</h2>
        ) : level === 2 ? (
          <h3 key={key}>{content}</h3>
        ) : level === 3 ? (
          <h4 key={key}>{content}</h4>
        ) : level === 4 ? (
          <h5 key={key}>{content}</h5>
        ) : (
          <h6 key={key}>{content}</h6>
        ),
      );
      cursor += 1;
      continue;
    }

    if (UNORDERED_ITEM.test(line)) {
      const list = readList(lines, cursor, UNORDERED_ITEM);
      blocks.push(
        <ul key={`unordered-${String(cursor)}`}>
          {list.items.map((item, index) => (
            <li key={`${String(index)}-${item.slice(0, 24)}`}>
              <InlineMarkdown source={item} />
            </li>
          ))}
        </ul>,
      );
      cursor = list.next;
      continue;
    }
    if (ORDERED_ITEM.test(line)) {
      const list = readList(lines, cursor, ORDERED_ITEM);
      blocks.push(
        <ol key={`ordered-${String(cursor)}`}>
          {list.items.map((item, index) => (
            <li key={`${String(index)}-${item.slice(0, 24)}`}>
              <InlineMarkdown source={item} />
            </li>
          ))}
        </ol>,
      );
      cursor = list.next;
      continue;
    }
    if (line.startsWith("> ")) {
      blocks.push(
        <blockquote key={`quote-${String(cursor)}`}>
          <InlineMarkdown source={line.slice(2)} />
        </blockquote>,
      );
      cursor += 1;
      continue;
    }

    const paragraph: string[] = [line];
    cursor += 1;
    while (
      cursor < lines.length &&
      (lines[cursor] ?? "").trim().length > 0 &&
      !isBlockStart(lines[cursor] ?? "")
    ) {
      paragraph.push(lines[cursor] ?? "");
      cursor += 1;
    }
    blocks.push(
      <p key={`paragraph-${String(cursor)}`}>
        <InlineMarkdown source={paragraph.join("\n")} />
      </p>,
    );
  }

  if (cursor < lines.length) {
    blocks.push(
      <p key="markdown-limit" className="artifact-content-omission">
        [Content omitted: Markdown block limit reached]
      </p>,
    );
  }
  return blocks;
}

export function RestrictedArtifactMarkdown({
  source,
}: {
  readonly source: string;
}): React.JSX.Element {
  return (
    <div className="artifact-markdown" data-artifact-markdown="restricted">
      {renderMarkdownBlocks(source)}
    </div>
  );
}
