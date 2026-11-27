import type { EChartsCoreOption } from 'echarts/core';
import { formatMetricValue } from './table';
import type { DatasetRow, VisualizationSpec } from './types';

const metricAxisLabel = {
  color: '#8794ad',
  formatter: (value: number | string) => formatMetricValue(value)
};

const metricTooltip = {
  valueFormatter: (value: unknown) => formatMetricValue(value)
};

const temporalDimensions = new Set(['date', 'yearMonth', 'yearWeek', 'isoYearIsoWeek', 'year']);

function dateTimestamp(value: DatasetRow[string], dimension: string): number | null {
  if (dimension !== 'date' && dimension !== 'yearMonth') return null;
  if (typeof value !== 'string' && typeof value !== 'number') return null;
  const rendered = String(value).trim();
  const ga4Date = dimension === 'date' && /^(\d{4})(\d{2})(\d{2})$/.exec(rendered);
  const ga4YearMonth = dimension === 'yearMonth' && /^(\d{4})(\d{2})$/.exec(rendered);
  if (
    !ga4Date &&
    !ga4YearMonth &&
    !(dimension === 'date' && /^\d{4}-\d{2}-\d{2}$/.test(rendered))
  ) {
    return null;
  }
  const normalized = ga4Date
    ? `${ga4Date[1]}-${ga4Date[2]}-${ga4Date[3]}T00:00:00Z`
    : ga4YearMonth
      ? `${ga4YearMonth[1]}-${ga4YearMonth[2]}-01T00:00:00Z`
      : `${rendered.slice(0, 10)}T00:00:00Z`;
  const timestamp = Date.parse(normalized);
  return Number.isFinite(timestamp) &&
    new Date(timestamp).toISOString().slice(0, 10) === normalized.slice(0, 10)
    ? timestamp
    : null;
}

function extractYear(
  rows: DatasetRow[],
  dimensionField: string,
  metricFields: string[]
): string | null {
  if (!temporalDimensions.has(dimensionField)) return null;
  // GA4 may return zero-filled rows for the other date range. They must not
  // choose a range's year, nor may a multi-year series collapse to its first year.
  const populated = rows.filter((row) =>
    metricFields.some((field) => Number(row[field] ?? 0) !== 0)
  );
  const candidates = populated.length ? populated : rows;
  const years = new Set(
    candidates.map((row) => /^(\d{4})/.exec(String(row[dimensionField] ?? ''))?.[1])
  );
  return years.size === 1 && !years.has(undefined) ? ([...years][0] ?? null) : null;
}

function formatDateCategoryLabel(value: DatasetRow[string]): string {
  const rendered = String(value ?? '').trim();
  const ga4Date = /^(\d{4})(\d{2})(\d{2})$/.exec(rendered);
  if (ga4Date) {
    const m = parseInt(ga4Date[2], 10);
    const d = parseInt(ga4Date[3], 10);
    return `${m}/${d}`;
  }
  const ga4YearMonth = /^(\d{4})(\d{2})$/.exec(rendered);
  if (ga4YearMonth) {
    const m = parseInt(ga4YearMonth[2], 10);
    return `${m}月`;
  }
  return rendered;
}

function isComparisonDataset(rows: DatasetRow[]): boolean {
  const ranges = new Set(
    rows
      .map((row) =>
        row.dateRange !== undefined && row.dateRange !== null ? String(row.dateRange).trim() : ''
      )
      .filter(Boolean)
  );
  return ranges.size >= 2 || rows.some((row) => row.dateRange === 'comparison');
}

function splitComparisonRows(rows: DatasetRow[]): {
  currentRows: DatasetRow[];
  comparisonRows: DatasetRow[];
} {
  const currentRows = rows.filter((row) => row.dateRange !== 'comparison');
  const comparisonRows = rows.filter((row) => row.dateRange === 'comparison');
  return { currentRows, comparisonRows };
}

function comparisonSeriesLabels(
  currentRows: DatasetRow[],
  comparisonRows: DatasetRow[],
  dimensionField: string,
  metricLabel: string,
  hasMultipleMetrics: boolean,
  metricFields: string[]
): { currentLabel: string; comparisonLabel: string } {
  const currentYear = extractYear(currentRows, dimensionField, metricFields);
  const comparisonYear = extractYear(comparisonRows, dimensionField, metricFields);

  let curPrefix = '当期';
  let compPrefix = '比較期間';
  if (currentYear && comparisonYear && currentYear !== comparisonYear) {
    curPrefix = `${currentYear}年`;
    compPrefix = `${comparisonYear}年`;
  }

  if (hasMultipleMetrics) {
    return {
      currentLabel: `${curPrefix} ${metricLabel}`,
      comparisonLabel: `${compPrefix} ${metricLabel}`
    };
  }
  return {
    currentLabel: curPrefix,
    comparisonLabel: compPrefix
  };
}

export function toEChartsOption(spec: VisualizationSpec, rows: DatasetRow[]): EChartsCoreOption {
  if (!['line', 'bar', 'donut'].includes(spec.type)) {
    throw new Error(`Unsupported ECharts type: ${spec.type}`);
  }
  const metric = spec.series[0];
  if (!metric) throw new Error('Visualization has no series.');
  const dimension = spec.x ?? spec.category;
  if (!dimension) throw new Error('Visualization has no dimension.');

  const base = {
    title: {
      // The wrapping DOM result-heading is the visible title. Repeating a long
      // exact page path inside the canvas clips it at narrow viewport widths.
      show: false,
      text: spec.title,
      left: 'center',
      top: 4,
      textStyle: { color: '#e8eefc', fontSize: 14 }
    },
    tooltip: { trigger: spec.type === 'donut' ? 'item' : 'axis', ...metricTooltip },
    backgroundColor: 'transparent'
  };

  if (spec.type === 'donut') {
    return {
      ...base,
      series: [
        {
          type: 'pie',
          radius: ['48%', '72%'],
          data: rows.map((row) => ({
            name: String(row[dimension] ?? ''),
            value: Number(row[metric.field] ?? 0)
          })),
          label: {
            formatter: (params: { name: string; value: unknown }) =>
              `${params.name}: ${formatMetricValue(params.value)}`
          }
        }
      ]
    };
  }

  // 期間比較（前年比など）の折れ線または棒グラフ
  if ((spec.type === 'line' || spec.type === 'bar') && isComparisonDataset(rows)) {
    const { currentRows, comparisonRows } = splitComparisonRows(rows);
    const metricFields = spec.series.map((item) => item.field);
    const currentYear = extractYear(currentRows, dimension, metricFields);
    const comparisonYear = extractYear(comparisonRows, dimension, metricFields);
    const alignYears =
      currentYear !== null && comparisonYear !== null && currentYear !== comparisonYear;

    const isYearMonth =
      dimension === 'yearMonth' &&
      rows.every((row) => /^\d{4}(0[1-9]|1[0-2])$/.test(String(row[dimension] ?? '')));
    const isDate =
      dimension === 'date' && rows.every((row) => /^\d{8}$/.test(String(row[dimension] ?? '')));

    if (isYearMonth && alignYears) {
      const monthKeys = new Set<string>();
      const currentDataMap = new Map<string, DatasetRow>();
      const comparisonDataMap = new Map<string, DatasetRow>();

      for (const row of currentRows) {
        const m = /^(\d{4})(\d{2})$/.exec(String(row[dimension] ?? ''));
        if (m) {
          const rowYear = m[1];
          const month = m[2];
          monthKeys.add(month);
          // 当期の年を優先（異なる年の0埋め行による上書きを防ぐ）
          if (!currentYear || rowYear === currentYear || !currentDataMap.has(month)) {
            currentDataMap.set(month, row);
          }
        }
      }

      for (const row of comparisonRows) {
        const m = /^(\d{4})(\d{2})$/.exec(String(row[dimension] ?? ''));
        if (m) {
          const rowYear = m[1];
          const month = m[2];
          monthKeys.add(month);
          // 前期の年を優先（当期の0埋め行による上書きを防ぐ）
          if (!comparisonYear || rowYear === comparisonYear || !comparisonDataMap.has(month)) {
            comparisonDataMap.set(month, row);
          }
        }
      }

      const sortedMonths = Array.from(monthKeys).sort();
      const xLabels = sortedMonths.map((m) => `${parseInt(m, 10)}月`);

      const seriesList = spec.series.flatMap((item, metricIndex) => {
        const { currentLabel, comparisonLabel } = comparisonSeriesLabels(
          currentRows,
          comparisonRows,
          dimension,
          item.label,
          spec.series.length > 1,
          metricFields
        );
        const curColor = ['#5de4c7', '#f2c66d', '#d08cff', '#6da7ff'][metricIndex % 4];
        const compColor = ['#6da7ff', '#ff9f7f', '#a0a7e6', '#5de4c7'][metricIndex % 4];

        return [
          {
            type: spec.type,
            name: currentLabel,
            data: sortedMonths.map((m) => Number(currentDataMap.get(m)?.[item.field] ?? 0)),
            smooth: spec.type === 'line',
            symbolSize: 6,
            itemStyle: { color: curColor },
            lineStyle: spec.type === 'line' ? { width: 3 } : undefined
          },
          {
            type: spec.type,
            name: comparisonLabel,
            data: sortedMonths.map((m) => Number(comparisonDataMap.get(m)?.[item.field] ?? 0)),
            smooth: spec.type === 'line',
            symbolSize: 6,
            itemStyle: { color: compColor },
            lineStyle: spec.type === 'line' ? { width: 2, type: 'dashed' as const } : undefined
          }
        ];
      });

      return {
        ...base,
        grid: { left: 56, right: 16, top: 72, bottom: 48 },
        legend: { top: 32, textStyle: { color: '#8794ad' } },
        xAxis: {
          type: 'category',
          name: '月',
          nameLocation: 'middle',
          nameGap: 30,
          data: xLabels,
          axisLabel: { color: '#8794ad' },
          axisLine: { lineStyle: { color: '#243149' } }
        },
        yAxis: {
          type: 'value',
          name: metric.label,
          axisLabel: metricAxisLabel,
          splitLine: { lineStyle: { color: '#1d2940' } }
        },
        series: seriesList
      };
    }

    if (isDate && alignYears) {
      // 日別前年比
      const sortedCurrent = [...currentRows]
        .filter((row) => !currentYear || String(row[dimension] ?? '').startsWith(currentYear))
        .sort((left, right) =>
          String(left[dimension] ?? '').localeCompare(String(right[dimension] ?? ''), 'ja', {
            numeric: true
          })
        );
      const sortedComparison = [...comparisonRows]
        .filter((row) => !comparisonYear || String(row[dimension] ?? '').startsWith(comparisonYear))
        .sort((left, right) =>
          String(left[dimension] ?? '').localeCompare(String(right[dimension] ?? ''), 'ja', {
            numeric: true
          })
        );

      const xLabels = sortedCurrent.map((row) => formatDateCategoryLabel(row[dimension]));

      const seriesList = spec.series.flatMap((item, metricIndex) => {
        const { currentLabel, comparisonLabel } = comparisonSeriesLabels(
          currentRows,
          comparisonRows,
          dimension,
          item.label,
          spec.series.length > 1,
          metricFields
        );
        const curColor = ['#5de4c7', '#f2c66d', '#d08cff', '#6da7ff'][metricIndex % 4];
        const compColor = ['#6da7ff', '#ff9f7f', '#a0a7e6', '#5de4c7'][metricIndex % 4];

        return [
          {
            type: spec.type,
            name: currentLabel,
            data: sortedCurrent.map((row) => Number(row[item.field] ?? 0)),
            smooth: spec.type === 'line',
            symbolSize: 6,
            itemStyle: { color: curColor },
            lineStyle: spec.type === 'line' ? { width: 3 } : undefined
          },
          {
            type: spec.type,
            name: comparisonLabel,
            data: sortedCurrent.map((_, i) =>
              i < sortedComparison.length ? Number(sortedComparison[i]?.[item.field] ?? 0) : 0
            ),
            smooth: spec.type === 'line',
            symbolSize: 6,
            itemStyle: { color: compColor },
            lineStyle: spec.type === 'line' ? { width: 2, type: 'dashed' as const } : undefined
          }
        ];
      });

      return {
        ...base,
        grid: { left: 56, right: 16, top: 72, bottom: 48 },
        legend: { top: 32, textStyle: { color: '#8794ad' } },
        xAxis: {
          type: 'category',
          name: '日付',
          nameLocation: 'middle',
          nameGap: 30,
          data: xLabels,
          axisLabel: { color: '#8794ad' },
          axisLine: { lineStyle: { color: '#243149' } }
        },
        yAxis: {
          type: 'value',
          name: metric.label,
          axisLabel: metricAxisLabel,
          splitLine: { lineStyle: { color: '#1d2940' } }
        },
        series: seriesList
      };
    }

    // 一般カテゴリ（チャネル別・デバイス別など）の比較
    const categoryField = dimension;
    const categories: string[] = [];
    const seen = new Set<string>();
    for (const row of [...currentRows, ...comparisonRows]) {
      const cat = String(row[categoryField] ?? '');
      if (!seen.has(cat)) {
        seen.add(cat);
        categories.push(cat);
      }
    }
    if (temporalDimensions.has(dimension)) {
      categories.sort((left, right) => left.localeCompare(right, 'ja', { numeric: true }));
    }

    const currentMap = new Map<string, DatasetRow>();
    for (const row of currentRows) {
      currentMap.set(String(row[categoryField] ?? ''), row);
    }
    const comparisonMap = new Map<string, DatasetRow>();
    for (const row of comparisonRows) {
      comparisonMap.set(String(row[categoryField] ?? ''), row);
    }

    const seriesList = spec.series.flatMap((item, metricIndex) => {
      const { currentLabel, comparisonLabel } = comparisonSeriesLabels(
        currentRows,
        comparisonRows,
        categoryField,
        item.label,
        spec.series.length > 1,
        metricFields
      );
      const curColor = ['#5de4c7', '#f2c66d', '#d08cff', '#6da7ff'][metricIndex % 4];
      const compColor = ['#6da7ff', '#ff9f7f', '#a0a7e6', '#5de4c7'][metricIndex % 4];

      return [
        {
          type: spec.type,
          name: currentLabel,
          data: categories.map((cat) => Number(currentMap.get(cat)?.[item.field] ?? 0)),
          smooth: spec.type === 'line',
          itemStyle: { color: curColor }
        },
        {
          type: spec.type,
          name: comparisonLabel,
          data: categories.map((cat) => Number(comparisonMap.get(cat)?.[item.field] ?? 0)),
          smooth: spec.type === 'line',
          itemStyle: { color: compColor },
          lineStyle: spec.type === 'line' ? { width: 2, type: 'dashed' as const } : undefined
        }
      ];
    });

    return {
      ...base,
      grid: { left: 44, right: 16, top: 72, bottom: 32 },
      legend: { top: 32, textStyle: { color: '#8794ad' } },
      xAxis: {
        type: 'category',
        data: categories,
        axisLabel: { color: '#8794ad' },
        axisLine: { lineStyle: { color: '#243149' } }
      },
      yAxis: {
        type: 'value',
        axisLabel: metricAxisLabel,
        splitLine: { lineStyle: { color: '#1d2940' } }
      },
      series: seriesList
    };
  }

  // 単一期間（非比較）の時系列折れ線グラフ
  if (spec.type === 'line' && spec.x) {
    const isYearMonth = dimension === 'yearMonth';
    const points = rows.map((row) => ({
      timestamp: dateTimestamp(row[spec.x as string], dimension),
      row
    }));
    if (points.every((point) => point.timestamp !== null)) {
      points.sort((left, right) => (left.timestamp as number) - (right.timestamp as number));
      return {
        ...base,
        grid: { left: 56, right: 16, top: spec.series.length > 1 ? 72 : 48, bottom: 48 },
        xAxis: {
          type: 'time',
          name: isYearMonth ? '年月' : '日付',
          nameLocation: 'middle',
          nameGap: 30,
          boundaryGap: false,
          axisLabel: {
            color: '#8794ad',
            formatter: isYearMonth ? '{yyyy}-{MM}' : undefined
          },
          axisLine: { lineStyle: { color: '#243149' } }
        },
        yAxis: {
          type: 'value',
          name: metric.label,
          axisLabel: metricAxisLabel,
          splitLine: { lineStyle: { color: '#1d2940' } }
        },
        legend: spec.series.length > 1 ? { top: 32, textStyle: { color: '#8794ad' } } : undefined,
        series: spec.series.map((item, index) => ({
          type: 'line',
          name: item.label,
          data: points.map((point) => [
            point.timestamp as number,
            Number(point.row[item.field] ?? 0)
          ]),
          smooth: true,
          symbolSize: 6,
          itemStyle: { color: ['#5de4c7', '#6da7ff', '#f2c66d', '#d08cff'][index % 4] },
          lineStyle: { width: 3 }
        }))
      };
    }
  }

  const orderedRows =
    spec.type === 'line' || temporalDimensions.has(dimension)
      ? [...rows].sort((left, right) =>
          String(left[dimension] ?? '').localeCompare(String(right[dimension] ?? ''), 'ja', {
            numeric: true
          })
        )
      : rows;

  return {
    ...base,
    grid: { left: 44, right: 16, top: spec.series.length > 1 ? 72 : 48, bottom: 32 },
    legend: spec.series.length > 1 ? { top: 32, textStyle: { color: '#8794ad' } } : undefined,
    xAxis: {
      type: 'category',
      data: orderedRows.map((row) => String(row[dimension] ?? '')),
      axisLabel: { color: '#8794ad' },
      axisLine: { lineStyle: { color: '#243149' } }
    },
    yAxis: {
      type: 'value',
      axisLabel: metricAxisLabel,
      splitLine: { lineStyle: { color: '#1d2940' } }
    },
    series: spec.series.map((item, index) => ({
      type: spec.type,
      name: item.label,
      data: orderedRows.map((row) => Number(row[item.field] ?? 0)),
      smooth: spec.type === 'line',
      symbolSize: 6,
      itemStyle: { color: ['#5de4c7', '#6da7ff', '#f2c66d', '#d08cff'][index % 4] },
      lineStyle: { width: 3 }
    }))
  };
}
