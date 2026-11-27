<script lang="ts">
  import Chart from '#lib/components/Chart.svelte';
  import DataTable from '#lib/components/DataTable.svelte';
  import type { AnalysisHistoryResult } from '#lib/types';
  import { formatChangeRate, qualityMessages } from '#lib/quality';
  import { comparisonTables } from '#lib/comparison-table';
  import { timeChannelGroups } from '#lib/table';

  export let result: AnalysisHistoryResult;

  $: qualityNotes = Array.from(
    new Set(result.evidence.flatMap((item) => qualityMessages(item.quality)))
  );
  $: comparisons = result.visualization.type === 'table' ? comparisonTables(result) : [];
  $: hasBaseline = result.rows.some((row) => row.dateRange === 'comparison');
  $: rawSpec = hasBaseline
    ? {
        ...result.visualization,
        dimensions: ['dateRange', ...(result.visualization.dimensions ?? [])]
      }
    : result.visualization;
  $: groups = result.visualization.type === 'table' ? timeChannelGroups(rawSpec, result.rows) : [];
</script>

<section class="analysis-result">
  <header class="result-heading">
    <strong>{result.visualization.title}</strong>
    <span class:hit={result.cache_hit} class="cache-status">
      Cache {result.cache_hit ? 'hit' : 'miss'}
    </span>
  </header>
  {#if result.visualization.subtitle}
    <p class="visualization-subtitle">{result.visualization.subtitle}</p>
  {/if}
  {#if result.source_message_id}
    <p class="quality-note">保存済みの分析結果を再表示しています。追加取得は行っていません。</p>
  {/if}
  {#if ['line', 'bar', 'donut'].includes(result.visualization.type)}
    <Chart spec={result.visualization} rows={result.rows} />
  {:else if result.visualization.type === 'kpi' && result.findings.length}
    <div class="kpi-grid">
      {#each result.findings as finding (finding.id)}
        <div class="kpi-result">
          <span>{finding.metric_label}</span>
          <strong>{finding.value ?? '—'}</strong>
          {#if finding.change_rate !== null}
            <small>
              {finding.comparison_kind === 'mom'
                ? '前月比'
                : finding.comparison_kind === 'yoy'
                  ? '前年比'
                  : '比較期間比'}
              {formatChangeRate(finding.change_rate)}
            </small>
          {/if}
        </div>
      {/each}
    </div>
  {:else if result.visualization.type === 'table'}
    {#if comparisons.length}
      {#each comparisons as table (table.key)}
        <DataTable spec={table.spec} rows={table.rows} />
      {/each}
      <p class="quality-note">
        取得範囲内の比較です。片方の期間で確認できない値は「未確認」とし、差や増減率を算出しません。
      </p>
    {:else}
      {#if groups.length}
        {#each groups as group (group.key)}
          <div class="channel-table">
            <h4>{group.label}</h4>
            <DataTable spec={{ ...rawSpec, title: group.label + 'の集計表' }} rows={group.rows} />
          </div>
        {/each}
      {:else}
        <DataTable spec={rawSpec} rows={result.rows} />
      {/if}
      {#if hasBaseline}
        <p class="quality-note">
          期間別の取得値です。対応する項目が確認できない行の差は計算していません。
        </p>
      {/if}
    {/if}
  {:else}
    <p>{result.rows.length}行の分析結果を取得しました。</p>
  {/if}
  {#if qualityNotes.length}
    <p class="quality-note">
      データ品質注記: {qualityNotes.join('、')}
    </p>
  {/if}
</section>

<style>
  .channel-table + .channel-table {
    margin-top: 1rem;
  }
  .channel-table h4 {
    font-size: 0.9rem;
    margin: 0 0 0.45rem;
  }
</style>
