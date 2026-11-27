const metricNumberFormatter = new Intl.NumberFormat('ja-JP', {
  maximumFractionDigits: 3
});

export function formatMetricValue(value: unknown): string {
  if (value === null || value === undefined) return '—';

  const numericValue =
    typeof value === 'number'
      ? value
      : typeof value === 'string' && value.trim() !== ''
        ? Number(value)
        : Number.NaN;

  return Number.isFinite(numericValue) ? metricNumberFormatter.format(numericValue) : String(value);
}

export function formatTableValue(value: unknown, isMetric: boolean): string {
  if (isMetric) return formatMetricValue(value);
  if (value === null || value === undefined) return '—';
  return String(value);
}

const columnLabels: Readonly<Record<string, string>> = {
  active_users: 'アクティブユーザー',
  activeUsers: 'アクティブユーザー',
  average_session_duration: '平均セッション時間',
  averageSessionDuration: '平均セッション時間',
  bounce_rate: '直帰率',
  bounceRate: '直帰率',
  city: '市区町村',
  country: '国',
  date: '日付',
  dateRange: '期間',
  device_category: 'デバイス',
  deviceCategory: 'デバイス',
  engagement_rate: 'エンゲージメント率',
  engagementRate: 'エンゲージメント率',
  event_count: 'イベント数',
  event_name: 'イベント名',
  eventCount: 'イベント数',
  eventName: 'イベント名',
  host_name: 'ホスト名',
  hostName: 'ホスト名',
  key_events: 'キーイベント',
  keyEvents: 'キーイベント',
  landing_page: 'ランディングページ',
  landingPagePlusQueryString: 'ランディングページ',
  new_users: '新規ユーザー',
  newUsers: '新規ユーザー',
  page_path: 'ページ',
  page_title: 'ページタイトル',
  page_views: 'ページビュー',
  pagePathPlusQueryString: 'ページ',
  pageTitle: 'ページタイトル',
  platform: 'プラットフォーム',
  screenPageViews: 'ページビュー',
  session_campaign: 'キャンペーン',
  session_channel_group: 'チャネル',
  session_medium: 'メディア',
  session_source: '参照元',
  sessionCampaignName: 'キャンペーン',
  sessionDefaultChannelGroup: 'チャネル',
  sessionMedium: 'メディア',
  sessionSource: '参照元',
  sessions: 'セッション',
  total_users: '総ユーザー',
  totalUsers: '総ユーザー',
  user_age_bracket: '年齢層',
  user_gender: '性別',
  userAgeBracket: '年齢層',
  userGender: '性別',
  year: '年',
  year_month: '年月',
  year_week: '年週',
  yearMonth: '年月',
  yearWeek: '年週'
};

function readableFallback(field: string): string {
  const unscopedField = field.includes(':') ? field.slice(field.lastIndexOf(':') + 1) : field;
  const readable = unscopedField
    .replace(/([a-z\d])([A-Z])/g, '$1 $2')
    .replace(/[_-]+/g, ' ')
    .trim();
  return readable ? readable.charAt(0).toUpperCase() + readable.slice(1) : '';
}

export function tableColumnLabel(field: string, configuredLabel?: string): string {
  if (configuredLabel) {
    return columnLabels[configuredLabel] ?? configuredLabel;
  }
  return columnLabels[field] ?? (readableFallback(field) || field);
}
import type { DatasetRow, VisualizationSpec } from './types';

const dateFields = new Set(['date', 'dateHour', 'yearWeek', 'isoYearIsoWeek', 'yearMonth', 'year']);
const channelFields = new Set([
  'sessionDefaultChannelGroup',
  'firstUserDefaultChannelGroup',
  'defaultChannelGroup',
  'sessionPrimaryChannelGroup',
  'firstUserPrimaryChannelGroup',
  'primaryChannelGroup'
]);
const collator = new Intl.Collator('ja-JP', { numeric: true, sensitivity: 'base' });

export function isTemporalTableField(field: string) {
  return dateFields.has(field);
}

export function formatTableDate(field: string, value: string): string {
  if (field === 'date' && /^\d{8}$/.test(value)) {
    const normalized = value.slice(0, 4) + '-' + value.slice(4, 6) + '-' + value.slice(6);
    const instant = new Date(normalized + 'T00:00:00Z');
    return Number.isFinite(instant.valueOf()) && instant.toISOString().slice(0, 10) === normalized
      ? normalized
      : value;
  }
  if (field === 'yearMonth' && /^\d{4}(0[1-9]|1[0-2])$/.test(value))
    return value.slice(0, 4) + '-' + value.slice(4);
  return value;
}

export function sortTableRows(
  rows: DatasetRow[],
  field: string,
  direction: 'ascending' | 'descending',
  metric = false
): DatasetRow[] {
  const sign = direction === 'ascending' ? 1 : -1;
  return [...rows].sort((a, b) => {
    const one = a[field];
    const two = b[field];
    const missing = (v: unknown) => v == null || v === '' || v === '—' || v === '未確認';
    if (missing(one) || missing(two)) return Number(missing(one)) - Number(missing(two));
    if (metric) {
      const n1 = Number(String(one).replace(/[,%]/g, ''));
      const n2 = Number(String(two).replace(/[,%]/g, ''));
      if (Number.isFinite(n1) && Number.isFinite(n2)) return sign * (n1 - n2);
    }
    return (
      sign *
      collator.compare(formatTableDate(field, String(one)), formatTableDate(field, String(two)))
    );
  });
}

export function timeChannelGroups(spec: VisualizationSpec, rows: DatasetRow[]) {
  const axes = [...(spec.dimensions ?? []), spec.x ?? '', spec.category ?? ''];
  const channel = axes.find(
    (field) =>
      channelFields.has(field) || /^(?:session|firstUser)CustomChannelGroup:\d+$/.test(field)
  );
  if (!channel || !axes.some(isTemporalTableField)) return [];
  const groups = new Map<string | null, DatasetRow[]>();
  for (const row of rows) {
    const value = row[channel];
    const key = value == null || value === '' ? null : String(value);
    const group = groups.get(key) ?? [];
    group.push(row);
    groups.set(key, group);
  }
  return [...groups.entries()]
    .sort(([one], [two]) => (one === null ? 1 : two === null ? -1 : collator.compare(one, two)))
    .map(([key, group]) => ({ key, label: key ?? '未確認のチャネル', rows: group }));
}
