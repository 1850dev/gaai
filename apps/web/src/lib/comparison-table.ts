import { formatChangeRate } from './quality';
import { isTemporalTableField, tableColumnLabel } from './table';
import type { AnalysisHistoryResult, DatasetRow, VisualizationSpec } from './types';

/** Render only server-verified, exact-key comparison facts. Do not join/sum raw rows. */
export function comparisonTables(result: AnalysisHistoryResult) {
  const spec = result.visualization;
  const axes = spec.dimensions?.length ? spec.dimensions : spec.category ? [spec.category] : [];
  if (!axes.length || axes.some(isTemporalTableField)) return [];
  return result.findings.flatMap(
    (finding): { key: string; spec: VisualizationSpec; rows: DatasetRow[] }[] => {
      if (!finding.comparison_kind || !finding.category_comparisons?.length) return [];
      const metric = tableColumnLabel(finding.metric_field, finding.metric_label);
      const rows = finding.category_comparisons.flatMap((item): DatasetRow[] => {
        if (!axes.every((field) => Object.hasOwn(item.dimensions, field))) return [];
        return [
          {
            ...item.dimensions,
            __baseline: item.comparison_value ?? '未確認',
            __target: item.current_value ?? '未確認',
            __difference: item.status === 'matched' ? item.difference : null,
            __rate: item.status === 'matched' ? formatChangeRate(item.change_rate) : '—'
          }
        ];
      });
      if (!rows.length) return [];
      return [
        {
          key: finding.id,
          spec: {
            ...spec,
            title: metric + 'の内訳比較',
            dimensions: axes,
            category: null,
            x: null,
            series: [
              { field: '__baseline', label: '比較基準（' + metric + '）' },
              { field: '__target', label: '対象（' + metric + '）' },
              { field: '__difference', label: '差（対象 − 基準）' },
              { field: '__rate', label: '増減率' }
            ]
          },
          rows
        }
      ];
    }
  );
}
