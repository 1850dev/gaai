import { describe, it, expect } from 'vitest';
import { confirmation, explicitPageViewRanking, readDraft, skills, type Context } from './contract';
const ctx: Context = {
  state: {},
  revision: 0,
  today: '2026-11-27',
  skills: Object.keys(skills),
  terms: [
    { kind: 'metric', name: 'page_views', aliases: ['PV'] },
    { kind: 'dimension', name: 'page', aliases: [] }
  ]
};
const plan = {
  skill_id: 'ga4.trend.v1',
  metric: 'page_views',
  period: { start: '2026-10-01', end: '2026-10-31' },
  grain: 'day'
};
describe('local plan review', () => {
  it('resolves the requested month, page axis, ranked row cap and table without Qwen guessing', () => {
    const context: Context = {
      ...ctx,
      terms: [...ctx.terms, { kind: 'dimension', name: 'page_path', aliases: ['ページ'] }]
    };
    expect(
      explicitPageViewRanking('2026年11月のPVランキングを上位10件で表にしてください。', context)
    ).toEqual({
      plan: {
        skill_id: 'ga4.breakdown_rank.v1',
        metric: 'page_views',
        dimension: 'page_path',
        period: { start: '2026-11-01', end: '2026-11-30' },
        limit: 10,
        presentation: 'table'
      }
    });
    expect(
      explicitPageViewRanking(
        '2026年11月の参照元別PVランキングを上位10件で表にしてください。',
        context
      )
    ).toBeNull();
    expect(
      explicitPageViewRanking('2026年11月のPVランキングを上位30件で表にしてください。', context)
    ).toHaveProperty('ask');
    expect(
      explicitPageViewRanking('2026年11月のPVランキングを上位10件で表にしてください。', ctx)
    ).toHaveProperty('ask');
    expect(() =>
      readDraft(JSON.stringify({ plan: { ...plan, presentation: 'pie' } }), context)
    ).toThrow();
  });
  it('accepts explicit acknowledgements only, not requests that happen to contain them', () => {
    for (const word of ['はい', 'ＯＫ！', 'お願いします。']) expect(confirmation(word)).toBe('yes');
    expect(confirmation('いいえ。')).toBe('no');
    expect(confirmation('解説してください', 'plan')).toBe('question');
    expect(confirmation('解説してください', 'explain')).toBe('yes');
    expect(confirmation('実行してください', 'explain')).toBe('question');
    for (const text of ['はい、でも期間を変えて', 'OKではない', 'はい？'])
      expect(confirmation(text)).toBe('question');
  });
  it('accepts registered semantic terms, or a clarification without execution', () => {
    expect(readDraft(JSON.stringify({ plan }), ctx)).toEqual({ plan });
    expect(readDraft('{"ask":"どの期間ですか？"}', ctx)).toEqual({ ask: 'どの期間ですか？' });
  });
  it.each([
    { skill_id: 'run_report' },
    { property_id: '123' },
    { sql: 'SELECT 1' },
    { metric: 'unregistered' },
    { dimension: 'page' },
    { grain: 'hour' },
    { period: { start: '2026-02-30', end: '2026-03-01' } }
  ])('rejects out-of-contract selections', (patch) => {
    expect(() => readDraft(JSON.stringify({ plan: { ...plan, ...patch } }), ctx)).toThrow();
  });
});
