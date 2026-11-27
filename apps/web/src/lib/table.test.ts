import { describe, expect, it } from 'vitest';
import {
  formatMetricValue,
  formatTableDate,
  formatTableValue,
  sortTableRows,
  tableColumnLabel,
  timeChannelGroups
} from './table';

describe('formatMetricValue', () => {
  it('formats finite numbers and numeric strings with at most three fractional digits', () => {
    expect(formatMetricValue(1234)).toBe('1,234');
    expect(formatMetricValue(1234.56789)).toBe('1,234.568');
    expect(formatMetricValue('1234.5')).toBe('1,234.5');
  });

  it('handles absent and non-finite or nonnumeric values safely', () => {
    expect(formatMetricValue(null)).toBe('—');
    expect(formatMetricValue(undefined)).toBe('—');
    expect(formatMetricValue('not available')).toBe('not available');
    expect(formatMetricValue(Number.NaN)).toBe('NaN');
    expect(formatMetricValue(Number.POSITIVE_INFINITY)).toBe('Infinity');
  });

  it('does not number-format dimension values', () => {
    expect(formatTableValue('202608', false)).toBe('202608');
    expect(formatTableValue('001234', false)).toBe('001234');
    expect(formatTableValue('1234.56789', true)).toBe('1,234.568');
  });
});

describe('tableColumnLabel', () => {
  it('uses a readable label for GA4 response field names', () => {
    expect(tableColumnLabel('yearMonth')).toBe('年月');
    expect(tableColumnLabel('screenPageViews')).toBe('ページビュー');
  });

  it('translates a canonical semantic label instead of showing an internal name', () => {
    expect(tableColumnLabel('activeUsers', 'active_users')).toBe('アクティブユーザー');
  });

  it('preserves an explicitly configured business label', () => {
    expect(tableColumnLabel('keyEvents', '資料請求')).toBe('資料請求');
  });

  it('humanizes an unknown field instead of exposing camelCase syntax', () => {
    expect(tableColumnLabel('customEvent:membershipLevel')).toBe('Membership Level');
  });
});

describe('chronological channel tables', () => {
  const rows = [
    { date: '20260515', sessionDefaultChannelGroup: 'Organic Search', screenPageViews: 590 },
    { date: '20260502', sessionDefaultChannelGroup: 'Direct', screenPageViews: 10 },
    { date: '2026-05-01', sessionDefaultChannelGroup: 'Organic Search', screenPageViews: 20 }
  ];
  it('groups by the returned channel without dropping either column or mutating input', () => {
    const groups = timeChannelGroups(
      {
        type: 'table',
        title: '日付とチャネル',
        dimensions: ['date', 'sessionDefaultChannelGroup'],
        series: [],
        dataset_ids: [],
        evidence_ids: []
      },
      rows
    );
    expect(groups.map((g) => g.label)).toEqual(['Direct', 'Organic Search']);
    expect(sortTableRows(groups[1].rows, 'date', 'ascending').map((r) => r.date)).toEqual([
      '2026-05-01',
      '20260515'
    ]);
    expect(rows[0].date).toBe('20260515');
    expect(formatTableDate('date', '20260230')).toBe('20260230');
  });
  it('sorts metric columns numerically and keeps unconfirmed values last', () => {
    const metrics = [{ share: '2%' }, { share: '未確認' }, { share: '1,000%' }, { share: '-3%' }];
    expect(sortTableRows(metrics, 'share', 'descending', true).map((r) => r.share)).toEqual([
      '1,000%',
      '2%',
      '-3%',
      '未確認'
    ]);
  });
});
