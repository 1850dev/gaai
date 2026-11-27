<script lang="ts">
  import type { User } from 'firebase/auth';
  import { request } from '#lib/api';
  import { onMount } from 'svelte';

  export let user: User;
  export let source: string;
  type Agreement = {
    id: string;
    revision: number;
    text: string;
    status: 'pending' | 'confirmed';
    updated_at: string;
  };
  type Hit = {
    session_id: string;
    question: string;
    recorded_at: string;
    section: {
      title: string;
      period: { start: string; end: string } | null;
      evidence_ids: string[];
    };
  };
  let agreements: Agreement[] = [];
  let draft = '';
  let edit: Agreement | null = null;
  let busy = false;
  let error = '';
  let query = '';
  let hits: Hit[] = [];
  let searched = false;
  let truncated = false;
  let disposed = false;
  const base = `/api/data-source-sets/${encodeURIComponent(source)}/memory`;
  onMount(() => {
    void load();
    return () => {
      disposed = true;
    };
  });
  async function load() {
    busy = true;
    try {
      const value = await request<Agreement[]>(user, `${base}/agreements`);
      if (!disposed) agreements = value;
    } catch (e) {
      if (!disposed) error = String(e);
    } finally {
      if (!disposed) busy = false;
    }
  }
  async function change(action: 'propose' | 'confirm' | 'edit' | 'delete', item?: Agreement) {
    if (busy) return;
    busy = true;
    error = '';
    try {
      await request<Agreement>(user, `${base}/agreements`, {
        method: 'POST',
        body: JSON.stringify({
          action,
          id: item?.id ?? '',
          revision: item?.revision ?? 0,
          text: action === 'edit' || action === 'propose' ? draft : ''
        })
      });
      if (!disposed) {
        draft = '';
        edit = null;
        await load();
      }
    } catch (e) {
      if (!disposed) error = String(e);
    } finally {
      if (!disposed) busy = false;
    }
  }
  async function search() {
    if (busy || !query.trim()) return;
    busy = true;
    error = '';
    try {
      const result = await request<{ hits: Hit[]; truncated: boolean }>(
        user,
        `${base}/search?q=${encodeURIComponent(query)}`
      );
      if (!disposed) {
        hits = result.hits;
        truncated = result.truncated;
        searched = true;
      }
    } catch (e) {
      if (!disposed) error = String(e);
    } finally {
      if (!disposed) busy = false;
    }
  }
</script>

<section class="memory-settings">
  <h2>記憶と過去の分析</h2>
  <p>
    あなたと選択中の分析対象だけに適用されます。AIの仮説や測定値を、確定した方針として保存しません。
  </p>
  {#if error}<p role="alert">{error}</p>{/if}
  <h3>長期的な合意・制約</h3>
  <p>
    候補の内容を確認してから「確定」してください。訂正した内容は再確認が必要です。削除後は長期記憶として参照しませんが、元の会話や過去の回答は残ります。
  </p>
  <form on:submit|preventDefault={() => change(edit ? 'edit' : 'propose', edit ?? undefined)}>
    <label
      >記憶する方針<textarea
        bind:value={draft}
        maxlength="1000"
        required
        rows="3"
        placeholder="例：カート〜決済画面の変更は対象外"></textarea></label
    >
    <button disabled={busy || !draft.trim()}
      >{edit ? '訂正内容を候補として保存' : '確認候補として追加'}</button
    >
    {#if edit}<button
        type="button"
        on:click={() => {
          edit = null;
          draft = '';
        }}>編集をやめる</button
      >{/if}
  </form>
  {#each agreements as agreement (agreement.id)}
    <article>
      <strong>{agreement.status === 'confirmed' ? '確認済み' : '未確認の候補'}</strong>
      <p>{agreement.text}</p>
      {#if agreement.status === 'pending'}<button
          disabled={busy}
          on:click={() => change('confirm', agreement)}>内容を確認して確定</button
        >{/if}
      <button
        disabled={busy}
        on:click={() => {
          edit = agreement;
          draft = agreement.text;
        }}>訂正</button
      >
      <button disabled={busy} on:click={() => change('delete', agreement)}>記憶から削除</button>
    </article>
  {/each}
  <h3>過去の分析を検索</h3>
  <form on:submit|preventDefault={search}>
    <label
      >期間・指標・ページなど<input
        bind:value={query}
        maxlength="1000"
        placeholder="例：2026年5月 /campaign/ 参照元"
      /></label
    >
    <button disabled={busy || !query.trim()}>検索</button>
  </form>
  {#if truncated}<p>検索候補の上限に達しました。全履歴を網羅した結果ではありません。</p>{/if}
  {#if searched && !hits.length}<p>
      参照可能な分析は見つかりませんでした。旧セッションは開いて会話を再開すると保存済み分析が検索対象へ追加されます。
    </p>{/if}
  {#each hits as hit (`${hit.session_id}:${hit.section.evidence_ids.join(',')}`)}
    <article>
      <strong>{hit.section.title}</strong>
      <p>{hit.question}</p>
      <small
        >過去の取得結果 · {hit.section.period?.start ?? '期間不明'} 〜 {hit.section.period?.end ??
          ''} · 保存日時 {hit.recorded_at}</small
      >
      <p>根拠: {hit.section.evidence_ids.join(', ')}</p>
    </article>
  {/each}
</section>

<style>
  .memory-settings {
    padding: 24px;
    margin: 24px 0;
    border: 1px solid var(--border);
    border-radius: 16px;
    background: var(--panel);
    overflow-wrap: anywhere;
  }
  p {
    line-height: 1.7;
    color: var(--muted);
  }
  label {
    display: grid;
    gap: 8px;
  }
  textarea,
  input {
    box-sizing: border-box;
    width: 100%;
    padding: 10px;
    background: var(--panel);
    border: 1px solid var(--border);
    color: var(--text);
    border-radius: 8px;
    font: inherit;
  }
  form {
    display: block;
    padding: 0;
    background: none;
  }
  button {
    margin: 10px 8px 0 0;
    padding: 8px 12px;
    background: var(--panel-soft);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 8px;
  }
  article {
    border-top: 1px solid var(--border);
    padding: 16px 0;
    margin-top: 16px;
  }
  small {
    color: var(--muted);
  }
</style>
