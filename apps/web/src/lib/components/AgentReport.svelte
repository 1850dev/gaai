<script lang="ts">
  import {
    surfaces,
    goalStatuses,
    goalNextKinds,
    goalBases,
    goalCheckMarkdown,
    type AgentWorkspace
  } from '#lib/agent';
  import type { AnalysisHistoryResult } from '#lib/types';
  import MarkdownNodes from './MarkdownNodes.svelte';
  import { parseMarkdown } from '#lib/markdown';
  export let workspace: AgentWorkspace;
  export let reportMarkdown: string | undefined = undefined;
  export let results: Record<string, AnalysisHistoryResult> = {};
  export let discuss: (question: string) => void = () => {};
  export let forget: ((kind: string, text: string) => void) | undefined = undefined;
  export let busy = false;
  $: reportPreview = reportMarkdown ? parseMarkdown(reportMarkdown) : [];
  const statuses: Record<string, string> = {
    ready: '準備中',
    running: '調査中',
    waiting: '確認待ち',
    complete: '今回の調査完了',
    partial: '追加調査が必要',
    failed: '中断・保存済み'
  };
  function download() {
    const lines = [
      `# ${workspace.objective || '分析レポート'}`,
      '',
      `状態: ${statuses[workspace.status] ?? workspace.status}`,
      ''
    ];
    for (const section of workspace.sections) {
      lines.push(`## ${section.title}`);
      const result = results[section.message_id];
      for (const text of section.facts ?? result?.findings.map((f) => f.text) ?? [])
        lines.push(`- ${text}`);
      lines.push(`根拠: ${section.evidence_ids.join(', ')}`, '');
      for (const note of section.quality_notes ??
        result?.evidence.flatMap((e) => e.quality.notes) ??
        [])
        lines.push(`注記: ${note}`);
    }
    for (const p of workspace.proposals)
      lines.push(
        `## 改善仮説: ${p.title}`,
        p.hypothesis,
        p.changes,
        `変更対象: ${p.surfaces.map((s) => surfaces[s]).join('、')}`,
        `タイトル案: ${p.suggested_title}`,
        `説明文案: ${p.suggested_description}`,
        `キーワード案: ${p.keywords.join('、')}`,
        `検証: ${p.validation_plan}`,
        `評価指標候補: ${p.success_metric ?? '未確定（計測・定義の確認が必要）'}`,
        ...(p.evaluation
          ? [
              `目的との関係: ${p.evaluation.goal_alignment}`,
              `指標を選ぶ理由: ${p.evaluation.metric_reason}`,
              `指標の限界: ${p.evaluation.metric_limitations}`,
              `計測の位置付け: ${p.evaluation.measurement_status === 'proxy' ? '代理指標（事業成果そのものではありません）' : '計測・定義の確認が必要'}`,
              ...p.evaluation.prerequisites.map((item) => `- 実施前の確認: ${item}`),
              ...(p.evaluation.goal_checks ?? []).flatMap(goalCheckMarkdown)
            ]
          : ['評価方法の詳細は旧形式のため未記録です。']),
        `根拠: ${p.evidence_ids.join(', ')}`,
        ''
      );
    lines.push('## 未解決事項', ...workspace.open_questions);
    if (workspace.report_message_id && !reportMarkdown) return;
    const url = URL.createObjectURL(
      new Blob([reportMarkdown || lines.join('\n')], { type: 'text/markdown;charset=utf-8' })
    );
    const link = document.createElement('a');
    link.href = url;
    link.download = 'analytics-report.md';
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
</script>

<details class="agent-report" open>
  <summary
    >{workspace.report_requested || workspace.report_message_id
      ? 'レポート・改善提案'
      : '改善案・次の検証'}
    <small>{statuses[workspace.status] ?? workspace.status}</small></summary
  >
  {#if workspace.report_requested && workspace.objective}<h3>{workspace.objective}</h3>{/if}
  {#if workspace.staged_plan}
    <h3>段階分析の進捗</h3>
    <p>{workspace.staged_plan.objective}</p>
    <ul>
      {#each workspace.staged_plan.items as item (item.value)}<li>
          {item.value}：{item.status === 'complete' ? '内訳取得済み' : '内訳未取得'}
        </li>{/each}
    </ul>
    {#if workspace.staged_plan.phase !== 'complete'}
      <p>
        {workspace.status === 'running'
          ? '計画した対象の内訳を順に調査しています。追加の送信は不要です。'
          : '取得済みの根拠と進捗を保存しています。未取得の対象は評価に含めません。'}
      </p>
    {/if}
  {/if}
  {#if workspace.memory?.entries.length}
    <details>
      <summary>会話の作業記憶</summary>
      <p>ユーザーの発言と未検証仮説です。測定済みの数値とは区別しています。</p>
      {#each workspace.memory.entries as entry (`${entry.message_id}:${entry.kind}:${entry.text}`)}
        <p>
          <small
            >{{
              objective: '目的',
              decision: 'ユーザー方針',
              target: '対象',
              hypothesis: '未検証仮説',
              question: '確認事項'
            }[entry.kind] ?? entry.kind}</small
          >：{entry.text}
          {#if forget && entry.kind !== 'hypothesis'}<button
              type="button"
              disabled={busy}
              on:click={() => forget?.(entry.kind, entry.text)}>作業記憶から外す</button
            >{/if}
        </p>
      {/each}
      {#if workspace.memory.trimmed}<small
          >作業記憶の表示上限を超えた項目があります。必要な過去分析は検索して参照します。</small
        >{/if}
    </details>
  {/if}
  {#if workspace.report_message_id}
    {#if reportMarkdown}
      <details>
        <summary>保存済みレポートのプレビュー</summary>
        <div class="markdown-body"><MarkdownNodes nodes={reportPreview} /></div>
      </details>
      <button type="button" on:click={download}>レポートをMarkdownで保存</button>
    {:else}
      <p>保存済みレポートの本文をまだ取得できていません。履歴を読み直してください。</p>
    {/if}
  {:else if workspace.report_requested && workspace.sections.length}
    <p>
      調査 {workspace.sections.length} 件 · サイト設定 v{workspace.context_revision}
    </p>
    <ol>
      {#each workspace.sections as section (section.message_id)}<li>
          {section.title}<small>根拠: {section.evidence_ids.join(', ')}</small>
        </li>{/each}
    </ol>
    <button type="button" on:click={download}>レポートをMarkdownで保存</button>
  {/if}
  {#each workspace.proposals as proposal, index (index)}
    <section>
      <h4>改善仮説: {proposal.title}</h4>
      <small
        >未検証 · 優先度 {proposal.priority} · {proposal.surfaces
          .map((s) => surfaces[s])
          .join('、')}</small
      >
      <p>{proposal.hypothesis}</p>
      <p>{proposal.changes}</p>
      {#if proposal.evaluation}
        {#if workspace.proposals_site_revision != null && workspace.proposals_site_revision !== workspace.context_revision}
          <small
            >この案は以前のサイト設定に基づく未検証仮説です。現在の目的との関係を再確認してください。</small
          >
        {/if}
        <p><strong>目的との関係:</strong> {proposal.evaluation.goal_alignment}</p>
        <p><strong>指標を選ぶ理由:</strong> {proposal.evaluation.metric_reason}</p>
        <p><strong>指標の限界:</strong> {proposal.evaluation.metric_limitations}</p>
        <small>
          {proposal.evaluation.measurement_status === 'proxy'
            ? '代理指標です。事業成果そのものを測定した値ではありません。'
            : '計測・定義の確認が必要です。'}
        </small>
        {#if proposal.evaluation.prerequisites.length}
          <p>実施前に確認すること</p>
          <ul>
            {#each proposal.evaluation.prerequisites as item, index (index)}<li>{item}</li>{/each}
          </ul>
        {/if}
        {#each proposal.evaluation.goal_checks ?? [] as check, checkIndex (checkIndex)}
          <details class="goal-check" open>
            <summary>評価したい目的: {check.goal}</summary>
            <small
              >目的の出典: {goalBases[
                check.goal_basis ?? 'unconfirmed'
              ]}{#if check.goal_quote}「{check.goal_quote}」{/if}</small
            >
            <p><strong>必要な根拠:</strong> {check.required_evidence}</p>
            <small>{goalStatuses[check.evidence_status]}</small>
            <p><strong>まだ分からないこと:</strong> {check.gap}</p>
            <p><strong>次の確認:</strong> {check.next_check}</p>
            <small>{goalNextKinds[check.next_check_kind]}</small>
          </details>
        {/each}
      {:else}
        <small>評価方法の詳細は旧形式のため未記録です。</small>
      {/if}
      {#if proposal.suggested_title}<p>タイトル案: {proposal.suggested_title}</p>{/if}
      {#if proposal.suggested_description}<p>説明文案: {proposal.suggested_description}</p>{/if}
      {#if proposal.keywords.length}<p>キーワード: {proposal.keywords.join('、')}</p>{/if}
      <p>検証方法: {proposal.validation_plan}</p>
      <small
        >評価指標候補: {proposal.success_metric ?? '未確定（計測・定義の確認が必要）'} · 根拠: {proposal.evidence_ids.join(
          ', '
        )}</small
      >
      <button
        type="button"
        disabled={busy}
        on:click={() =>
          discuss(`「${proposal.title}」について、変更内容と検証方法を一緒に具体化したい`)}
        >この施策を詰める</button
      >
    </section>
  {/each}
  {#each workspace.questions as q, index (index)}<p class="question">{q}</p>{/each}
  {#if workspace.open_questions.length}<h4>未解決事項</h4>
    <ul>
      {#each workspace.open_questions as q, index (index)}<li>{q}</li>{/each}
    </ul>{/if}
</details>

<style>
  .agent-report {
    margin: 16px 0;
    padding: 18px;
    border: 1px solid var(--border);
    border-radius: 12px;
    background: var(--panel);
    overflow-wrap: anywhere;
    min-width: 0;
  }
  summary {
    cursor: pointer;
    font-weight: 700;
  }
  small {
    display: block;
    color: var(--muted);
    font-size: 12px;
    margin-top: 6px;
  }
  p,
  li {
    font-size: 14px;
    line-height: 1.7;
  }
  section {
    border-top: 1px solid var(--border);
    margin-top: 18px;
    padding-top: 8px;
  }
  button {
    padding: 8px 12px;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: var(--panel);
    color: var(--accent);
    cursor: pointer;
    margin-top: 10px;
  }
  .question {
    border-left: 3px solid var(--accent);
    padding-left: 12px;
  }
  h3,
  h4 {
    margin: 12px 0;
  }
</style>
