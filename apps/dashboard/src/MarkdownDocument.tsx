import type { ReactNode } from 'react';

function safeLink(value: string): string | undefined {
  try {
    const url = new URL(value);
    return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password ? url.href : undefined;
  } catch { return undefined; }
}

function inline(text: string, depth = 0): ReactNode {
  if (depth > 3) return text;
  const pattern = /(`[^`\n]+`|\*\*[^*\n]+\*\*|\*[^*\n]+\*|\[[^\]\n]+\]\([^\s)]+\))/g;
  const result: ReactNode[] = []; let previous = 0;
  for (const match of text.matchAll(pattern)) {
    const position = match.index!;
    result.push(text.slice(previous, position));
    const value = match[0]; const key = `${position}:${depth}`;
    if (value.startsWith('`')) result.push(<code key={key}>{value.slice(1, -1)}</code>);
    else if (value.startsWith('**')) result.push(<strong key={key}>{value.slice(2, -2)}</strong>);
    else if (value.startsWith('*')) result.push(<em key={key}>{value.slice(1, -1)}</em>);
    else {
      const link = /^\[([^\]]+)\]\((.+)\)$/.exec(value)!;
      const url = safeLink(link[2]);
      result.push(url ? <a key={key} href={url} target="_blank" rel="noopener noreferrer">{inline(link[1], depth + 1)}</a> : value);
    }
    previous = position + value.length;
  }
  result.push(text.slice(previous));
  return result;
}

/** Markdown becomes React nodes. Raw HTML, images, scripts and unsafe URLs never execute. */
export function MarkdownDocument({ content }: { content: string }) {
  const lines = content.replace(/\r\n?/g, '\n').split('\n'); const blocks: ReactNode[] = [];
  const cells = (line: string) => line.trim().replace(/^\||\|$/g, '').split('|').map(value => value.trim());
  for (let index = 0; index < lines.length;) {
    const line = lines[index]; const key = index;
    if (!line.trim()) { index++; continue; }
    const fence = /^\s*(```+|~~~+)(.*)$/.exec(line);
    if (fence) {
      const code: string[] = []; index++;
      while (index < lines.length && !lines[index].trim().startsWith(fence[1])) code.push(lines[index++]);
      if (index < lines.length) index++;
      blocks.push(<pre key={key}><code>{code.join('\n')}</code></pre>); continue;
    }
    const heading = /^(#{1,6})\s+(.+)$/.exec(line);
    if (heading) {
      const Tag = `h${Math.min(heading[1].length + 1, 6)}` as 'h2' | 'h3' | 'h4' | 'h5' | 'h6';
      blocks.push(<Tag key={key}>{inline(heading[2])}</Tag>); index++; continue;
    }
    if (line.includes('|') && index + 1 < lines.length && /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(lines[index + 1])) {
      const headers = cells(line); index += 2; const rows: string[][] = [];
      while (index < lines.length && lines[index].trim() && lines[index].includes('|')) rows.push(cells(lines[index++]));
      blocks.push(<div className="artifact-table" key={key}><table><thead><tr>{headers.map((cell, i) => <th key={i}>{inline(cell)}</th>)}</tr></thead><tbody>{rows.map((row, r) => <tr key={r}>{headers.map((_, c) => <td key={c}>{inline(row[c] ?? '')}</td>)}</tr>)}</tbody></table></div>); continue;
    }
    const ordered = /^\s*\d+[.)]\s+/.test(line);
    if (ordered || /^\s*[-+*]\s+/.test(line)) {
      const items: string[] = []; const pattern = ordered ? /^\s*\d+[.)]\s+/ : /^\s*[-+*]\s+/;
      while (index < lines.length && pattern.test(lines[index])) items.push(lines[index++].replace(pattern, ''));
      const Tag = ordered ? 'ol' : 'ul';
      blocks.push(<Tag key={key}>{items.map((value, i) => <li key={i}>{inline(value)}</li>)}</Tag>); continue;
    }
    if (/^\s*>/.test(line)) {
      const quote: string[] = [];
      while (index < lines.length && /^\s*>/.test(lines[index])) quote.push(lines[index++].replace(/^\s*>\s?/, ''));
      blocks.push(<blockquote key={key}>{inline(quote.join(' '))}</blockquote>); continue;
    }
    if (/^\s*([-*_])(?:\s*\1){2,}\s*$/.test(line)) { blocks.push(<hr key={key} />); index++; continue; }
    const paragraph = [line]; index++;
    while (index < lines.length && lines[index].trim() && !/^\s*(#{1,6}\s|```|~~~|>|[-+*]\s|\d+[.)]\s)/.test(lines[index])) {
      if (lines[index].includes('|') && lines[index + 1]?.includes('---')) break;
      paragraph.push(lines[index++]);
    }
    blocks.push(<p key={key}>{inline(paragraph.join('\n'))}</p>);
  }
  return <div className="artifact-markdown">{blocks}</div>;
}
