import { Lexer, type Token, type Tokens } from 'marked';

export type MarkdownNode = {
  type: string;
  text?: string;
  children?: MarkdownNode[];
  depth?: number;
  ordered?: boolean;
  start?: number;
  href?: string;
  rows?: MarkdownNode[][][];
};

export function safeLink(href: string): string | undefined {
  if (/\s/u.test(href) || [...href].some((c) => c.charCodeAt(0) < 32 || c.charCodeAt(0) === 127))
    return undefined;
  try {
    const url = new URL(href);
    return ['https:', 'http:'].includes(url.protocol) && !url.username && !url.password
      ? url.href
      : undefined;
  } catch {
    return undefined;
  }
}

// Render an allowlisted tree with Svelte text interpolation, never raw HTML.
function nodes(tokens: Token[], depth = 0): MarkdownNode[] {
  if (depth > 32) return [{ type: 'text', text: tokens.map((t) => t.raw).join('') }];
  return tokens.flatMap((token): MarkdownNode[] => {
    const children = (values: Token[]) => nodes(values, depth + 1);
    switch (token.type) {
      case 'space':
      case 'def':
        return [];
      case 'heading': {
        const t = token as Tokens.Heading;
        return [
          {
            type: 'heading',
            depth: Math.max(2, Math.min(6, t.depth)),
            children: children(t.tokens)
          }
        ];
      }
      case 'paragraph':
      case 'strong':
      case 'em':
      case 'del':
      case 'blockquote':
        return [{ type: token.type, children: children((token as Tokens.Paragraph).tokens) }];
      case 'list': {
        const t = token as Tokens.List;
        return [
          {
            type: 'list',
            ordered: t.ordered,
            start: t.start || 1,
            children: t.items.map((item) => ({ type: 'item', children: children(item.tokens) }))
          }
        ];
      }
      case 'table': {
        const t = token as Tokens.Table;
        return [
          {
            type: 'table',
            rows: [t.header, ...t.rows].map((row) => row.map((cell) => children(cell.tokens)))
          }
        ];
      }
      case 'link': {
        const t = token as Tokens.Link;
        return [{ type: 'link', href: safeLink(t.href), children: children(t.tokens) }];
      }
      case 'image':
        return [{ type: 'text', text: `[画像: ${(token as Tokens.Image).text}]` }];
      case 'code':
      case 'codespan':
        return [{ type: token.type, text: (token as Tokens.Code).text }];
      case 'br':
      case 'hr':
        return [{ type: token.type }];
      case 'text': {
        const t = token as Tokens.Text;
        return t.tokens ? children(t.tokens) : [{ type: 'text', text: t.text }];
      }
      case 'escape':
        return [{ type: 'text', text: (token as Tokens.Escape).text }];
      default:
        // Includes HTML, scripts, iframes and unsupported extensions.
        return [{ type: 'text', text: token.raw }];
    }
  });
}

export function parseMarkdown(source: string): MarkdownNode[] {
  try {
    return nodes(Lexer.lex(source, { gfm: true, breaks: true }));
  } catch {
    return [{ type: 'text', text: source }];
  }
}

const legacyHeadings = new Set([
  '解釈・改善仮説（数値の根拠とは区別）',
  '評価の前提・限界',
  '確認した数値',
  '取得済みの内訳（確認できた事実）',
  'データ上の注意',
  '次に確認できること',
  '処理状況:'
]);

export function messageMarkdown(content: string, format: 'plain' | 'markdown' = 'plain'): string {
  if (format === 'markdown') return content;
  // Read-only presentation adapter for old messages. Never rewrite their stored text.
  return content
    .split(/\r?\n/)
    .map((line) => {
      const escaped = line.replace(/([\\`*_{}[\]<>#!|~])/g, '\\$1');
      if (legacyHeadings.has(line.trim())) return `## ${escaped}`;
      if (line.startsWith('・')) return `- ${escaped.slice(1)}`;
      return escaped.replace(/【(確認された事実|改善仮説)】/g, '\n\n**$1**\n\n');
    })
    .join('\n\n');
}
