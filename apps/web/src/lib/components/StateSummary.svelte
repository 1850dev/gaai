<script lang="ts">
  import { tableColumnLabel } from '#lib/table';
  import type { AnalysisState } from '#lib/types';

  export let state: AnalysisState;

  const display = (value: string | null | undefined) => value || '未指定';
  const semanticLabel = (value: string | null | undefined) =>
    value ? tableColumnLabel(value) : '未指定';
  const semanticLabels = (values: string[] | undefined, fallback: string | null) =>
    values?.length
      ? values.map((value) => tableColumnLabel(value)).join('、')
      : semanticLabel(fallback);
  const filterLabel = (kind: 'channel' | 'device', fallback: string | null) =>
    state.filters?.find((item) => item.kind === kind)?.value ?? display(fallback);
</script>

<section class="state-panel" aria-labelledby="state-heading">
  <div class="section-title">
    <span>Analysis State</span>
    <span class="live-dot">LIVE</span>
  </div>
  <h2 id="state-heading">現在の分析コンテキスト</h2>
  <dl>
    <div>
      <dt>Metric</dt>
      <dd title={semanticLabels(state.metrics, state.focus.metric)}>
        {semanticLabels(state.metrics, state.focus.metric)}
      </dd>
    </div>
    <div>
      <dt>Dimension</dt>
      <dd title={semanticLabels(state.dimensions, state.focus.dimension)}>
        {semanticLabels(state.dimensions, state.focus.dimension)}
      </dd>
    </div>
    <div>
      <dt>Channel filter</dt>
      <dd>{filterLabel('channel', state.focus.channel)}</dd>
    </div>
    <div>
      <dt>Device filter</dt>
      <dd>{filterLabel('device', state.focus.device)}</dd>
    </div>
    <div>
      <dt>Skill</dt>
      <dd title={state.selected_skill ?? '未指定'}>{display(state.selected_skill)}</dd>
    </div>
    <div>
      <dt>Semantic</dt>
      <dd>{state.semantic_version}</dd>
    </div>
  </dl>
</section>

<style>
  .state-panel {
    border: 1px solid var(--border);
    border-radius: 14px;
    background: var(--panel);
    padding: 18px;
  }
  .section-title {
    align-items: center;
    color: var(--muted);
    display: flex;
    font-size: 0.72rem;
    font-weight: 700;
    justify-content: space-between;
    letter-spacing: 0.09em;
    text-transform: uppercase;
  }
  .live-dot {
    color: var(--accent);
    font-size: 0.65rem;
  }
  h2 {
    font-size: 0.95rem;
    margin: 10px 0 16px;
  }
  dl {
    display: grid;
    gap: 12px;
    margin: 0;
  }
  dl div {
    display: flex;
    justify-content: space-between;
    gap: 16px;
  }
  dt {
    color: var(--muted);
    font-size: 0.78rem;
  }
  dd {
    font-size: 0.78rem;
    margin: 0;
    max-width: 130px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
</style>
