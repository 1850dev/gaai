import type { DatasetEvidence } from './types';

const labels: Record<string, string> = {
  sampled: 'サンプリングされた結果です。',
  subject_to_thresholding: 'プライバシー保護のしきい値が適用される可能性があります。',
  data_loss_from_other_row: '一部の内訳が「(other)」に集約されています。',
  result_row_limit_applied: '取得件数に上限があります。表示された内訳は全件ではありません。',
  unique_count_may_be_approximate: 'ユーザー数・セッション数は近似値の場合があります。'
};

export function qualityMessages(quality?: Partial<DatasetEvidence['quality']>): string[] {
  if (!quality) return [];
  const notes = [...(quality.notes ?? [])];
  for (const flag of ['sampled', 'subject_to_thresholding', 'data_loss_from_other_row'] as const) {
    if (quality[flag]) notes.push(flag);
  }
  return Array.from(new Set(notes)).map((note) => labels[note] ?? note);
}

// Formatting only: calculations and evidence retain the original precision.
export function formatChangeRate(value: string | null): string {
  if (value === null || value.trim() === '') return '—';
  const rate = Number(value);
  if (!Number.isFinite(rate)) return '—';
  const percent = rate * 100;
  if (!Number.isFinite(percent)) return '—';
  return `${(Math.abs(percent) < 0.005 ? 0 : percent).toLocaleString('ja-JP', {
    maximumFractionDigits: 2
  })}%`;
}
