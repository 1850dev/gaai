import { describe, expect, it } from 'vitest';
import { toEChartsOption } from './chart';

describe('toEChartsOption', () => {
  it.each(['yearWeek', 'isoYearIsoWeek', 'campaignId'])(
    'does not infer a calendar month from six-digit values in %s',
    (field) => {
      const option = toEChartsOption(
        {
          type: 'line',
          title: '集計',
          x: field,
          dataset_ids: ['dataset_test'],
          evidence_ids: ['evidence_test'],
          series: [{ field: 'activeUsers', label: 'UU' }]
        },
        [
          { [field]: '202602', activeUsers: 20 },
          { [field]: '202601', activeUsers: 10 }
        ]
      );
      expect(option).toMatchObject({
        xAxis: { type: 'category', data: ['202601', '202602'] },
        series: [{ data: [10, 20] }]
      });
    }
  );

  it('sorts a multi-year monthly bar chart without mutating the stored rows', () => {
    const rows = [
      { yearMonth: '202601', activeUsers: 20 },
      { yearMonth: '202512', activeUsers: 10 }
    ];
    const option = toEChartsOption(
      {
        type: 'bar',
        title: '月別UU',
        category: 'yearMonth',
        dataset_ids: ['dataset_test'],
        evidence_ids: ['evidence_test'],
        series: [{ field: 'activeUsers', label: 'UU' }]
      },
      rows
    );
    expect(option).toMatchObject({
      xAxis: { data: ['202512', '202601'] },
      series: [{ data: [10, 20] }]
    });
    expect(rows.map((row) => row.yearMonth)).toEqual(['202601', '202512']);
  });

  it('preserves annual labels rather than treating years as dates', () => {
    const option = toEChartsOption(
      {
        type: 'line',
        title: '年別UU',
        x: 'year',
        dataset_ids: ['dataset_test'],
        evidence_ids: ['evidence_test'],
        series: [{ field: 'activeUsers', label: 'UU' }]
      },
      [
        { year: '2026', activeUsers: 20 },
        { year: '2025', activeUsers: 10 }
      ]
    );
    expect(option).toMatchObject({
      xAxis: { type: 'category', data: ['2025', '2026'] },
      series: [{ data: [10, 20] }]
    });
  });

  it('does not silently normalize an impossible calendar date', () => {
    const option = toEChartsOption(
      {
        type: 'line',
        title: '日別UU',
        x: 'date',
        dataset_ids: ['dataset_test'],
        evidence_ids: ['evidence_test'],
        series: [{ field: 'activeUsers', label: 'UU' }]
      },
      [{ date: '20260230', activeUsers: 10 }]
    );
    expect(option).toMatchObject({
      xAxis: { type: 'category', data: ['20260230'] },
      series: [{ data: [10] }]
    });
  });

  it('retains same-year monthly comparison keys and does not label the comparison as last year', () => {
    const option = toEChartsOption(
      {
        type: 'line',
        title: '5月と6月',
        x: 'yearMonth',
        dataset_ids: ['dataset_test'],
        evidence_ids: ['evidence_test'],
        series: [{ field: 'activeUsers', label: 'UU' }]
      },
      [
        { dateRange: 'current', yearMonth: '202606', activeUsers: 20 },
        { dateRange: 'comparison', yearMonth: '202605', activeUsers: 10 }
      ]
    );
    expect(option).toMatchObject({
      xAxis: { data: ['202605', '202606'] },
      series: [
        { name: '当期', data: [0, 20] },
        { name: '比較期間', data: [10, 0] }
      ]
    });
  });

  it('keeps GA4 week numbers as weeks rather than parsing them as months', () => {
    const option = toEChartsOption(
      {
        type: 'line',
        title: '週別UU',
        x: 'yearWeek',
        dataset_ids: ['dataset_week'],
        evidence_ids: ['evidence_week'],
        series: [{ field: 'activeUsers', label: 'UU' }]
      },
      [
        { yearWeek: '202602', activeUsers: 20 },
        { yearWeek: '202601', activeUsers: 10 }
      ]
    );
    expect(option).toMatchObject({
      xAxis: { type: 'category', data: ['202601', '202602'] },
      series: [{ data: [10, 20] }]
    });
  });

  it('does not treat six-digit weekly comparison categories as months', () => {
    const option = toEChartsOption(
      {
        type: 'bar',
        title: '週別比較',
        category: 'yearWeek',
        dataset_ids: ['dataset_week_comparison'],
        evidence_ids: ['evidence_week_comparison'],
        series: [{ field: 'activeUsers', label: 'UU' }]
      },
      [
        { dateRange: 'current', yearWeek: '202623', activeUsers: 20 },
        { dateRange: 'comparison', yearWeek: '202622', activeUsers: 10 }
      ]
    );
    expect(option).toMatchObject({
      xAxis: { type: 'category', data: ['202622', '202623'] },
      series: [{ data: [0, 20] }, { data: [10, 0] }]
    });
  });

  it('keeps multiple years of monthly comparison data without dropping months', () => {
    const option = toEChartsOption(
      {
        type: 'line',
        title: '年を跨ぐ月別比較',
        x: 'yearMonth',
        dataset_ids: ['dataset_multi_year'],
        evidence_ids: ['evidence_multi_year'],
        series: [{ field: 'activeUsers', label: 'UU' }]
      },
      [
        { dateRange: 'current', yearMonth: '202601', activeUsers: 30 },
        { dateRange: 'current', yearMonth: '202501', activeUsers: 20 },
        { dateRange: 'comparison', yearMonth: '202401', activeUsers: 10 }
      ]
    );
    expect(option).toMatchObject({
      xAxis: { type: 'category', data: ['202401', '202501', '202601'] },
      series: [{ data: [0, 20, 30] }, { data: [10, 0, 0] }]
    });
  });

  it('renders a line spec without generated code', () => {
    const option = toEChartsOption(
      {
        type: 'line',
        title: 'CV推移',
        x: 'date',
        dataset_ids: ['dataset_001'],
        evidence_ids: ['evidence_001'],
        series: [{ field: 'keyEvents', label: 'CV' }]
      },
      [
        { date: '20260802', keyEvents: 17 },
        { date: '20260801', keyEvents: 12 }
      ]
    );
    expect(option).toMatchObject({
      xAxis: { type: 'time', name: '日付' },
      yAxis: { type: 'value', name: 'CV' },
      series: [
        {
          type: 'line',
          name: 'CV',
          data: [
            [Date.UTC(2026, 7, 1), 12],
            [Date.UTC(2026, 7, 2), 17]
          ]
        }
      ]
    });
  });

  it('formats metric displays without formatting dimension data', () => {
    const option = toEChartsOption(
      {
        type: 'bar',
        title: '月別指標',
        x: 'yearMonth',
        dataset_ids: ['dataset_001'],
        evidence_ids: ['evidence_001'],
        series: [{ field: 'value', label: '指標' }]
      },
      [{ yearMonth: '202608', value: '1234.56789' }]
    ) as {
      tooltip: { valueFormatter: (value: unknown) => string };
      xAxis: { data: string[] };
      yAxis: { axisLabel: { formatter: (value: number | string) => string } };
      series: Array<{ data: number[] }>;
    };

    expect(option.xAxis.data).toEqual(['202608']);
    expect(option.series[0]?.data).toEqual([1234.56789]);
    expect(option.yAxis.axisLabel.formatter(1234.56789)).toBe('1,234.568');
    expect(option.tooltip.valueFormatter('1234.5')).toBe('1,234.5');
  });

  it('formats visible donut values while preserving dimension labels', () => {
    const option = toEChartsOption(
      {
        type: 'donut',
        title: 'チャネル別指標',
        category: 'channelId',
        dataset_ids: ['dataset_001'],
        evidence_ids: ['evidence_001'],
        series: [{ field: 'value', label: '指標' }]
      },
      [{ channelId: '001234', value: 12.34567 }]
    ) as {
      series: Array<{
        data: Array<{ name: string; value: number }>;
        label: { formatter: (params: { name: string; value: unknown }) => string };
      }>;
    };

    expect(option.series[0]?.data).toEqual([{ name: '001234', value: 12.34567 }]);
    expect(option.series[0]?.label.formatter({ name: '001234', value: 12.34567 })).toBe(
      '001234: 12.346'
    );
  });

  it('rejects table specs at the chart renderer boundary', () => {
    expect(() =>
      toEChartsOption(
        {
          type: 'table',
          title: 'Details',
          dataset_ids: ['dataset_001'],
          evidence_ids: ['evidence_001'],
          series: [{ field: 'value', label: 'Value' }]
        },
        []
      )
    ).toThrow('Unsupported ECharts type');
  });

  it('sorts GA4 yearMonth values on a monthly time axis', () => {
    const option = toEChartsOption(
      {
        type: 'line',
        title: '月別アクティブユーザー',
        x: 'yearMonth',
        dataset_ids: ['dataset_monthly'],
        evidence_ids: ['evidence_monthly'],
        series: [{ field: 'activeUsers', label: 'アクティブユーザー' }]
      },
      [
        { yearMonth: '202608', activeUsers: 20 },
        { yearMonth: '202607', activeUsers: 15 }
      ]
    );

    expect(option).toMatchObject({
      xAxis: { type: 'time', name: '年月' },
      yAxis: { type: 'value', name: 'アクティブユーザー' },
      series: [
        {
          data: [
            [Date.UTC(2026, 6, 1), 15],
            [Date.UTC(2026, 7, 1), 20]
          ]
        }
      ]
    });
  });

  it('renders every metric in a multi-series trend', () => {
    const option = toEChartsOption(
      {
        type: 'line',
        title: '主要KPI推移',
        x: 'date',
        dimensions: ['date'],
        dataset_ids: ['dataset_multi'],
        evidence_ids: ['evidence_multi'],
        series: [
          { field: 'activeUsers', label: 'アクティブユーザー' },
          { field: 'sessions', label: 'セッション' }
        ]
      },
      [{ date: '20260801', activeUsers: 12, sessions: 18 }]
    );

    expect(option).toMatchObject({
      series: [
        { name: 'アクティブユーザー', data: [[Date.UTC(2026, 7, 1), 12]] },
        { name: 'セッション', data: [[Date.UTC(2026, 7, 1), 18]] }
      ]
    });
  });

  it('renders aligned category X-axis and dual series for year-over-year monthly trend', () => {
    const option = toEChartsOption(
      {
        type: 'line',
        title: '月別アクティブユーザー数（前年比）',
        x: 'yearMonth',
        dimensions: ['yearMonth'],
        dataset_ids: ['dataset_yoy_monthly'],
        evidence_ids: ['evidence_yoy_monthly'],
        series: [{ field: 'activeUsers', label: 'アクティブユーザー数' }]
      },
      [
        { dateRange: 'current', yearMonth: '202604', activeUsers: 120 },
        { dateRange: 'comparison', yearMonth: '202504', activeUsers: 100 },
        { dateRange: 'current', yearMonth: '202605', activeUsers: 150 },
        { dateRange: 'comparison', yearMonth: '202505', activeUsers: 110 }
      ]
    );

    expect(option).toMatchObject({
      xAxis: {
        type: 'category',
        name: '月',
        data: ['4月', '5月']
      },
      yAxis: { type: 'value', name: 'アクティブユーザー数' },
      series: [
        {
          type: 'line',
          name: '2026年',
          data: [120, 150]
        },
        {
          type: 'line',
          name: '2025年',
          data: [100, 110]
        }
      ]
    });
  });

  it('renders aligned category X-axis and dual series for year-over-year daily trend', () => {
    const option = toEChartsOption(
      {
        type: 'line',
        title: '日別アクティブユーザー数（前年比）',
        x: 'date',
        dimensions: ['date'],
        dataset_ids: ['dataset_yoy_daily'],
        evidence_ids: ['evidence_yoy_daily'],
        series: [{ field: 'activeUsers', label: 'アクティブユーザー数' }]
      },
      [
        { dateRange: 'current', date: '20260401', activeUsers: 25 },
        { dateRange: 'comparison', date: '20250401', activeUsers: 20 },
        { dateRange: 'current', date: '20260402', activeUsers: 30 },
        { dateRange: 'comparison', date: '20250402', activeUsers: 22 }
      ]
    );

    expect(option).toMatchObject({
      xAxis: {
        type: 'category',
        name: '日付',
        data: ['4/1', '4/2']
      },
      yAxis: { type: 'value', name: 'アクティブユーザー数' },
      series: [
        {
          type: 'line',
          name: '2026年',
          data: [25, 30]
        },
        {
          type: 'line',
          name: '2025年',
          data: [20, 22]
        }
      ]
    });
  });

  it('renders grouped bar chart with dual series for year-over-year category breakdown', () => {
    const option = toEChartsOption(
      {
        type: 'bar',
        title: 'チャネルごとのアクティブユーザー数（前年比）',
        category: 'sessionDefaultChannelGroup',
        dimensions: ['sessionDefaultChannelGroup'],
        dataset_ids: ['dataset_yoy_bar'],
        evidence_ids: ['evidence_yoy_bar'],
        series: [{ field: 'activeUsers', label: 'アクティブユーザー数' }]
      },
      [
        { dateRange: 'current', sessionDefaultChannelGroup: 'Organic Search', activeUsers: 200 },
        { dateRange: 'comparison', sessionDefaultChannelGroup: 'Organic Search', activeUsers: 180 },
        { dateRange: 'current', sessionDefaultChannelGroup: 'Direct', activeUsers: 150 },
        { dateRange: 'comparison', sessionDefaultChannelGroup: 'Direct', activeUsers: 120 }
      ]
    );

    expect(option).toMatchObject({
      xAxis: {
        type: 'category',
        data: ['Organic Search', 'Direct']
      },
      series: [
        {
          type: 'bar',
          name: '当期',
          data: [200, 150]
        },
        {
          type: 'bar',
          name: '比較期間',
          data: [180, 120]
        }
      ]
    });
  });

  it('renders grouped bar chart for multiple metrics without comparison', () => {
    const option = toEChartsOption(
      {
        type: 'bar',
        title: 'デバイス別指標',
        category: 'deviceCategory',
        dimensions: ['deviceCategory'],
        dataset_ids: ['dataset_multi_bar'],
        evidence_ids: ['evidence_multi_bar'],
        series: [
          { field: 'activeUsers', label: 'アクティブユーザー数' },
          { field: 'sessions', label: 'セッション数' }
        ]
      },
      [
        { deviceCategory: 'mobile', activeUsers: 300, sessions: 450 },
        { deviceCategory: 'desktop', activeUsers: 200, sessions: 280 }
      ]
    );

    expect(option).toMatchObject({
      xAxis: {
        type: 'category',
        data: ['mobile', 'desktop']
      },
      series: [
        {
          type: 'bar',
          name: 'アクティブユーザー数',
          data: [300, 200]
        },
        {
          type: 'bar',
          name: 'セッション数',
          data: [450, 280]
        }
      ]
    });
  });

  it('filters out GA4 cross-period zero-value rows in year-over-year monthly trend', () => {
    const option = toEChartsOption(
      {
        type: 'line',
        title: '月別ページビュー数（前年比）',
        x: 'yearMonth',
        dimensions: ['yearMonth'],
        dataset_ids: ['dataset_pv_yoy'],
        evidence_ids: ['evidence_pv_yoy'],
        series: [{ field: 'screenPageViews', label: 'ページビュー数' }]
      },
      [
        { dateRange: 'current', yearMonth: '202604', screenPageViews: 2406436 },
        { dateRange: 'current', yearMonth: '202603', screenPageViews: 2270912 },
        { dateRange: 'comparison', yearMonth: '202503', screenPageViews: 2243103 },
        { dateRange: 'comparison', yearMonth: '202504', screenPageViews: 2115566 },
        { dateRange: 'current', yearMonth: '202503', screenPageViews: 0 },
        { dateRange: 'current', yearMonth: '202504', screenPageViews: 0 },
        { dateRange: 'comparison', yearMonth: '202603', screenPageViews: 0 },
        { dateRange: 'comparison', yearMonth: '202604', screenPageViews: 0 }
      ]
    );

    expect(option).toMatchObject({
      xAxis: {
        type: 'category',
        name: '月',
        data: ['3月', '4月']
      },
      series: [
        {
          type: 'line',
          name: '2026年',
          data: [2270912, 2406436]
        },
        {
          type: 'line',
          name: '2025年',
          data: [2243103, 2115566]
        }
      ]
    });
  });

  it('renders grouped bar chart for year-over-year monthly trend when spec type is bar', () => {
    const option = toEChartsOption(
      {
        type: 'bar',
        title: '月別ページビュー数（前年比）',
        category: 'yearMonth',
        dimensions: ['yearMonth'],
        dataset_ids: ['dataset_pv_yoy_bar'],
        evidence_ids: ['evidence_pv_yoy_bar'],
        series: [{ field: 'screenPageViews', label: 'ページビュー数' }]
      },
      [
        { dateRange: 'current', yearMonth: '202604', screenPageViews: 2406436 },
        { dateRange: 'current', yearMonth: '202603', screenPageViews: 2270912 },
        { dateRange: 'comparison', yearMonth: '202503', screenPageViews: 2243103 },
        { dateRange: 'comparison', yearMonth: '202504', screenPageViews: 2115566 },
        { dateRange: 'current', yearMonth: '202503', screenPageViews: 0 },
        { dateRange: 'current', yearMonth: '202504', screenPageViews: 0 },
        { dateRange: 'comparison', yearMonth: '202603', screenPageViews: 0 },
        { dateRange: 'comparison', yearMonth: '202604', screenPageViews: 0 }
      ]
    );

    expect(option).toMatchObject({
      xAxis: {
        type: 'category',
        name: '月',
        data: ['3月', '4月']
      },
      series: [
        {
          type: 'bar',
          name: '2026年',
          data: [2270912, 2406436]
        },
        {
          type: 'bar',
          name: '2025年',
          data: [2243103, 2115566]
        }
      ]
    });
  });
});
