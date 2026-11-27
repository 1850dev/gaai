<script lang="ts">
  import {
    formatTableDate,
    formatTableValue,
    isTemporalTableField,
    sortTableRows,
    tableColumnLabel
  } from '#lib/table';
  import type { DatasetRow, VisualizationSpec } from '#lib/types';

  export let spec: VisualizationSpec;
  export let rows: DatasetRow[];

  const pageSize = 100;
  let sortField: string | undefined;
  let direction: 'ascending' | 'descending' = 'ascending';
  let page = 0;
  $: fields = [
    ...(spec.dimensions ?? []),
    ...(spec.x ? [spec.x] : []),
    ...(spec.category ? [spec.category] : []),
    ...spec.series.map((item) => item.field)
  ].filter((field, index, all) => all.indexOf(field) === index);
  $: labels = new Map(spec.series.map((item) => [item.field, item.label]));
  $: metricFields = new Set(spec.series.map((item) => item.field));
  $: activeSort = sortField ?? fields.find(isTemporalTableField);
  $: orderedRows = activeSort
    ? sortTableRows(rows, activeSort, direction, metricFields.has(activeSort))
    : rows;
  $: pageCount = Math.max(1, Math.ceil(orderedRows.length / pageSize));
  $: currentPage = Math.min(page, pageCount - 1);
  $: visibleRows = orderedRows.slice(currentPage * pageSize, (currentPage + 1) * pageSize);

  function sortBy(field: string) {
    direction = activeSort === field && direction === 'ascending' ? 'descending' : 'ascending';
    sortField = field;
    page = 0;
  }

  function label(field: string): string {
    return tableColumnLabel(field, labels.get(field));
  }

  function render(field: string, value: DatasetRow[string]): string {
    if (field === 'dateRange') {
      return value === 'comparison' ? '比較基準' : value === 'current' ? '対象' : '不明';
    }
    return formatTableDate(field, formatTableValue(value, metricFields.has(field)));
  }
</script>

<!-- Scrollable results need keyboard focus; the labelled region is intentional. -->
<!-- svelte-ignore a11y_no_noninteractive_tabindex -->
<div class="table-shell" role="region" aria-label={spec.title} tabindex="0">
  <table>
    <thead>
      <tr>
        {#each fields as field (field)}
          <th scope="col" aria-sort={activeSort === field ? direction : undefined}>
            <button type="button" class="sort-button" on:click={() => sortBy(field)}>
              {label(field)}<span aria-hidden="true"
                >{activeSort === field ? (direction === 'ascending' ? '↑' : '↓') : '↕'}</span
              >
            </button>
          </th>
        {/each}
      </tr>
    </thead>
    <tbody>
      {#each visibleRows as row, rowIndex (rowIndex)}
        <tr>
          {#each fields as field (field)}
            <td>{render(field, row[field])}</td>
          {/each}
        </tr>
      {/each}
    </tbody>
  </table>
</div>
{#if pageCount > 1}
  <div class="table-note" aria-label="表のページ">
    <button
      type="button"
      class="secondary compact"
      disabled={currentPage === 0}
      on:click={() => (page = currentPage - 1)}>前へ</button
    >
    <span>{currentPage + 1} / {pageCount}（取得済み {rows.length} 行）</span>
    <button
      type="button"
      class="secondary compact"
      disabled={currentPage + 1 >= pageCount}
      on:click={() => (page = currentPage + 1)}>次へ</button
    >
  </div>
{/if}

<style>
  .table-shell {
    max-height: 420px;
    overflow: auto;
    border: 1px solid var(--border);
    border-radius: 12px;
  }

  table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.84rem;
  }

  th,
  td {
    padding: 0.7rem 0.8rem;
    border-bottom: 1px solid var(--border);
    text-align: left;
    white-space: nowrap;
  }

  th {
    position: sticky;
    top: 0;
    background: var(--panel);
    color: var(--muted);
    z-index: 1;
  }

  td {
    font-variant-numeric: tabular-nums;
  }

  .table-note {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    color: var(--muted);
    font-size: 0.78rem;
    margin: 0.5rem 0 0;
  }
  .sort-button {
    display: inline-flex;
    align-items: center;
    gap: 0.45rem;
    border: 0;
    padding: 0;
    font: inherit;
    color: inherit;
    background: transparent;
    cursor: pointer;
  }
  .sort-button:hover,
  .sort-button:focus-visible {
    color: var(--accent);
  }
</style>
