import { describe, expect, it } from 'vitest';
import { messageMarkdown, parseMarkdown, safeLink, type MarkdownNode } from './markdown';

function flatten(nodes: MarkdownNode[]): MarkdownNode[] {
  return nodes.flatMap((n) => [n, ...flatten(n.children ?? []), ...flatten(n.rows?.flat(2) ?? [])]);
}

describe('safe Markdown document', () => {
  it('keeps headings, lists, tables, code and the original metric text', () => {
    const source =
      '## 結果\n\n- **事実**\n\n| ページ | PV |\n| --- | ---: |\n| /a\\|b/ | 123 |\n\n`evidence_one`\n\n```sql\nSELECT 1\n```';
    const all = flatten(parseMarkdown(source));
    expect(all.map((n) => n.type)).toEqual(
      expect.arrayContaining(['heading', 'list', 'strong', 'table', 'codespan', 'code'])
    );
    expect(all.some((n) => n.text === '123')).toBe(true);
    expect(all.some((n) => n.text === '/a|b/')).toBe(true);
    expect(messageMarkdown(source, 'markdown')).toBe(source);
  });

  it.each([
    'javascript:alert(1)',
    'data:text/html,test',
    '//tracker.test/pixel',
    '/api/admin',
    'java\nscript:alert(1)',
    'javascript&#58;alert(1)',
    'file:///etc/passwd',
    'https://user:secret@example.test'
  ])('rejects unsafe or implicit links: %s', (href) => {
    expect(safeLink(href)).toBeUndefined();
  });

  it('renders HTML and images as inert text, without tracking requests', () => {
    const all = flatten(
      parseMarkdown(
        '<script>alert(1)</script>\n\n<img src=x onerror=alert(1)>\n\n![tracking](https://tracker.test/pixel)\n\n[bad](javascript:alert%281%29)'
      )
    );
    expect(all.filter((n) => n.type === 'link').every((n) => !n.href)).toBe(true);
    expect(all.some((n) => n.type === 'image' || n.type === 'html')).toBe(false);
    expect(all.some((n) => n.text?.includes('<script>'))).toBe(true);
    expect(safeLink('https://example.test/path?q=a')).toBe('https://example.test/path?q=a');
  });

  it('adapts legacy newlines and headings without treating old text as HTML', () => {
    const original =
      'まとめ\n評価の前提・限界\n・PVだけでは判断できない\n<script>\n【改善仮説】導線を検証';
    const formatted = messageMarkdown(original);
    expect(formatted).toContain('\n\n## 評価の前提・限界\n\n');
    expect(formatted).toContain('- PVだけでは判断できない');
    expect(formatted).toContain('\\<script\\>');
    expect(formatted).toContain('**改善仮説**\n\n');
    expect(original).toContain('\n<script>\n');
  });
});
