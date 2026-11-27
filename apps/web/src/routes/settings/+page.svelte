<script lang="ts">
  import { resolve } from '$app/paths';
  import { onMount } from 'svelte';
  import { SvelteMap } from 'svelte/reactivity';
  import { onAuthStateChanged, type User } from 'firebase/auth';
  import { firebaseAuth } from '#lib/firebase';
  import { api } from '#lib/api';
  import MemorySettings from '#lib/components/MemorySettings.svelte';
  import {
    agentApi,
    emptySite,
    surfaces,
    type Preferences,
    type Schedule,
    type Surface
  } from '#lib/agent';
  import type { Ga4PropertyCandidate, SourceSetSummary } from '#lib/types';
  let user: User | null = null;
  let sources: SourceSetSummary[] = [];
  let properties: { id: string; name: string }[] = [];
  let preferences: Preferences = { revision: 0, hidden_property_ids: [] };
  let source = '';
  let loadedSource = '';
  let site = emptySite();
  let schedules: Schedule[] = [];
  let busy = false;
  let browserMode = false;
  let browserAgentAvailable = false;
  let providerKnown = false;
  let notice = '';
  let error = '';
  let draft: Schedule = {
    id: '',
    revision: 0,
    data_source_set_id: '',
    name: '定期分析レポート',
    prompt:
      '主要KPIと前期間の比較、チャネルとページの傾向を調べて、サイト方針に沿う改善仮説をレポートにまとめて',
    frequency: 'weekly',
    weekday: 0,
    monthday: 1,
    hour: 9,
    timezone: 'Asia/Tokyo',
    enabled: false
  };
  onMount(() => {
    const auth = firebaseAuth();
    if (!auth) return;
    return onAuthStateChanged(auth, async (current) => {
      user = current;
      if (!current) return;
      busy = true;
      try {
        const [sets, prefs, identity] = await Promise.all([
          api.sourceSets(current),
          agentApi.preferences(current),
          api.me(current).catch(() => null)
        ]);
        sources = sets;
        preferences = prefs;
        providerKnown = identity !== null;
        browserMode = identity?.analysis_provider === 'browser_qwen';
        browserAgentAvailable = identity?.browser_agent_available === true;
        schedules = providerKnown && !browserMode ? await agentApi.schedules(current) : [];
        let candidates: Ga4PropertyCandidate[] = [];
        try {
          candidates = await api.ga4Properties(current);
        } catch {
          /* Existing registered sources remain configurable without discovery. */
        }
        const all = new SvelteMap(
          sources
            .filter((s) => s.ga4_property_id)
            .map((s) => [s.ga4_property_id!, { id: s.ga4_property_id!, name: s.name }])
        );
        candidates.forEach((p) =>
          all.set(p.property_id, { id: p.property_id, name: p.property_display_name })
        );
        preferences.hidden_property_ids.forEach((id) => {
          if (!all.has(id)) all.set(id, { id, name: '非表示のプロパティ' });
        });
        properties = [...all.values()];
        source = sources.find((s) => s.has_ga4)?.id ?? '';
        if (source) {
          site = await agentApi.site(current, source);
          loadedSource = source;
        }
      } catch (e) {
        error = e instanceof Error ? e.message : '設定を取得できませんでした';
      } finally {
        busy = false;
      }
    });
  });
  async function loadSite() {
    if (!user || !source) return;
    busy = true;
    error = '';
    notice = '';
    loadedSource = '';
    site = emptySite();
    try {
      site = await agentApi.site(user, source);
      loadedSource = source;
    } catch (e) {
      error = String(e);
    } finally {
      busy = false;
    }
  }
  async function saveSite() {
    if (!user || !source || loadedSource !== source) return;
    busy = true;
    error = '';
    try {
      site = await agentApi.saveSite(user, source, site);
      notice = 'サイト設定を保存しました。次の分析・説明時に参考情報として使います。';
    } catch (e) {
      error = String(e);
    } finally {
      busy = false;
    }
  }
  async function toggleVisibility(id: string) {
    if (!user) return;
    busy = true;
    error = '';
    const hidden = preferences.hidden_property_ids.includes(id);
    try {
      preferences = await agentApi.savePreferences(user, {
        ...preferences,
        hidden_property_ids: hidden
          ? preferences.hidden_property_ids.filter((v) => v !== id)
          : [...preferences.hidden_property_ids, id]
      });
    } catch (e) {
      error = String(e);
    } finally {
      busy = false;
    }
  }
  async function saveSchedule(value: Schedule) {
    if (!user) return;
    busy = true;
    error = '';
    try {
      const saved = await agentApi.saveSchedule(user, value);
      schedules = [saved, ...schedules.filter((s) => s.id !== saved.id)];
      notice = '定期レポート設定を保存しました。実行結果はセッション履歴に追加されます。';
      if (value.id === draft.id) draft = { ...saved };
      if (!value.id) draft = { ...draft, id: '', revision: 0 };
    } catch (e) {
      error = String(e);
    } finally {
      busy = false;
    }
  }
</script>

<svelte:head><title>サイト設定・定期レポート · Analytics AI</title></svelte:head>
<main class="settings-page">
  <a href={resolve('/')}>← 分析へ戻る</a>
  <h1>サイト設定・定期レポート</h1>
  <p>
    サイトの性質・目的・運営背景をAIに伝え、事業に沿う分析・説明・改善仮説に生かします。設定はあなたのアカウントと選択した分析対象ごとに保存されます。
  </p>
  {#if error}<p class="error" role="alert">{error}</p>{/if}
  {#if notice}<p class="notice" role="status">{notice}</p>{/if}
  {#if !user}<p>分析画面からログインしてください。</p>{:else}
    <section>
      <h2>プロパティの表示</h2>
      <p>選択一覧から非表示にできます。過去のレポートやアクセス権、定期実行は変更されません。</p>
      {#each properties as property (property.id)}
        <label class="check"
          ><input
            type="checkbox"
            checked={!preferences.hidden_property_ids.includes(property.id)}
            disabled={busy}
            on:change={() => toggleVisibility(property.id)}
          />{property.name}<small>{property.id}</small></label
        >
      {/each}
    </section>
    <section>
      <h2>サイト情報と改善方針</h2>
      <p>
        分析のための参考情報です。禁止事項やアクセス権の設定ではありません。会話での具体的な依頼を優先し、未入力でも分析できます。
      </p>
      {#if browserMode && !browserAgentAvailable}
        <p>この環境の端末内AIでは、レポート作成・改善提案はまだ有効になっていません。</p>
      {/if}
      <label
        >分析対象<select bind:value={source} on:change={loadSite} disabled={busy}
          >{#each sources.filter((s) => s.has_ga4) as s (s.id)}<option value={s.id}>{s.name}</option
            >{/each}</select
        ></label
      >
      {#if !source}<p>
          利用できる分析対象がありません。先に登録済みの対象を選択してください。
        </p>{:else}
        <form on:submit|preventDefault={saveSite}>
          <label
            >サイト基本情報<textarea
              rows="4"
              maxlength="4000"
              bind:value={site.site_basics}
              placeholder="例：ECサイト。女性の若年層がターゲット。セールより新製品の購入を重視。"
            ></textarea></label
          >
          <label
            >目標・評価指標<textarea
              rows="2"
              maxlength="2000"
              bind:value={site.goals}
              placeholder="例：来店前に必要な案内が見つかること。PVやUUは利用規模の参考指標。事業目標や計測状況が未確定ならその旨を記載。"
            ></textarea></label
          >
          <label
            >技術・運営上の背景<textarea
              rows="4"
              maxlength="4000"
              bind:value={site.technical_constraints}
              placeholder="例：共通CMSで運用。催事ページは店舗担当が更新。外部の予約・ECサービスも利用。"
            ></textarea></label
          >
          <fieldset>
            <legend>関心のある改善領域（参考）</legend>
            <p>未選択の領域も分析・相談できます。提案を禁止する設定ではありません。</p>
            <div class="checks">
              {#each Object.entries(surfaces) as [key, label] (key)}<label class="check"
                  ><input
                    type="checkbox"
                    bind:group={site.allowed_surfaces}
                    value={key as Surface}
                  />{label}</label
                >{/each}
            </div>
          </fieldset>
          <label
            >コンテンツ・施策方針<textarea
              rows="4"
              maxlength="4000"
              bind:value={site.content_policy}
              placeholder="例：来店準備に役立つ既存情報を充実させたい。催事・フロア・アクセス案内の見つけやすさを重視。"
            ></textarea></label
          >
          <div class="checks">
            <label class="check"
              ><input
                type="checkbox"
                bind:checked={site.allow_new_content}
              />新規コンテンツ案に関心がある</label
            ><label class="check"
              ><input
                type="checkbox"
                bind:checked={site.allow_rewrite}
              />既存コンテンツのリライト案に関心がある</label
            >
          </div>
          <button class="primary" disabled={busy || loadedSource !== source}
            >サイト設定を保存</button
          >
        </form>
      {/if}
    </section>
    {#if source && loadedSource === source}
      {#key `${user.uid}:${source}`}<MemorySettings {user} {source} />{/key}
    {/if}
    {#if browserMode}
      <section>
        <h2>定期レポート</h2>
        <p>
          端末内AIはブラウザを閉じていると動作しないため、週次・月次の自動作成には対応していません。
        </p>
      </section>
    {:else if providerKnown}<section>
        <h2>定期レポート</h2>
        <p>
          週次は前の完了週（月〜日）、月次は前月を分析します。結果は新しいセッションに保存します。
        </p>
        <form
          on:submit|preventDefault={() =>
            saveSchedule({
              ...draft,
              data_source_set_id: draft.id ? draft.data_source_set_id : source
            })}
        >
          <label>レポート名<input bind:value={draft.name} maxlength="120" required /></label>
          <label
            >分析の依頼<textarea rows="3" bind:value={draft.prompt} maxlength="4000" required
            ></textarea></label
          >
          <div class="schedule-grid">
            <label
              >頻度<select bind:value={draft.frequency}
                ><option value="weekly">毎週</option><option value="monthly">毎月</option></select
              ></label
            >
            {#if draft.frequency === 'weekly'}<label
                >曜日<select bind:value={draft.weekday}
                  >{#each ['月', '火', '水', '木', '金', '土', '日'] as day, i (day)}<option
                      value={i}>{day}曜日</option
                    >{/each}</select
                ></label
              >{:else}<label
                >日付<input
                  type="number"
                  bind:value={draft.monthday}
                  min="1"
                  max="28"
                  required
                /></label
              >{/if}
            <label
              >時刻（時）<input
                type="number"
                bind:value={draft.hour}
                min="0"
                max="23"
                required
              /></label
            >
            <label>タイムゾーン<input bind:value={draft.timezone} required /></label>
          </div>
          <label class="check"
            ><input type="checkbox" bind:checked={draft.enabled} />定期実行を有効にする</label
          >
          <button class="primary" disabled={busy || !source}
            >{draft.id ? '定期レポートを更新' : '選択中の分析対象に登録'}</button
          >
          {#if draft.id}<button
              type="button"
              on:click={() => (draft = { ...draft, id: '', revision: 0, enabled: false })}
              >新規登録に戻る</button
            >{/if}
        </form>
        <p class="hint">
          ブラウザを閉じても実行するには、サーバー側の定期レポートワーカーが稼働している必要があります。
        </p>
        {#each schedules as schedule (schedule.id)}
          <article>
            <h3>{schedule.name}</h3>
            <p>
              {sources.find((s) => s.id === schedule.data_source_set_id)?.name ??
                schedule.data_source_set_id} · {schedule.frequency === 'weekly' ? '週次' : '月次'} · {schedule.hour}時
              ({schedule.timezone})
            </p>
            <p>{schedule.prompt}</p>
            <small
              >次回: {schedule.next_run_at
                ? new Date(schedule.next_run_at).toLocaleString('ja-JP', {
                    timeZone: schedule.timezone
                  })
                : '停止中'} · 前回: {schedule.last_status ?? '未実行'}
              {schedule.last_error ?? ''}</small
            >
            <button
              disabled={busy}
              on:click={() => saveSchedule({ ...schedule, enabled: !schedule.enabled })}
              >{schedule.enabled ? '一時停止' : '再開'}</button
            >
            <button
              disabled={busy}
              on:click={() => {
                draft = { ...schedule };
                source = schedule.data_source_set_id;
                void loadSite();
              }}>編集</button
            >
          </article>
        {/each}
      </section>
    {/if}
  {/if}
</main>

<style>
  .settings-page {
    max-width: 960px;
    margin: 0 auto;
    padding: 32px 24px 80px;
    color: var(--text);
    overflow-wrap: anywhere;
  }
  h1 {
    font-size: 30px;
    margin-top: 28px;
  }
  h2 {
    font-size: 20px;
  }
  p {
    line-height: 1.7;
    color: var(--muted);
  }
  section {
    padding: 24px;
    margin: 24px 0;
    border: 1px solid var(--border);
    border-radius: 16px;
    background: var(--panel);
  }
  form {
    display: block;
    padding: 0;
    background: none;
  }
  label {
    display: flex;
    flex-direction: column;
    gap: 8px;
    margin: 16px 0;
    font-size: 14px;
  }
  input,
  select,
  textarea {
    box-sizing: border-box;
    width: 100%;
    padding: 10px;
    color: inherit;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: var(--panel);
    font: inherit;
  }
  textarea {
    resize: vertical;
  }
  .check {
    flex-direction: row;
    align-items: center;
    gap: 10px;
  }
  .check input {
    width: auto;
  }
  .checks {
    display: flex;
    flex-wrap: wrap;
    gap: 0 20px;
  }
  fieldset {
    border: 1px solid var(--border);
    border-radius: 8px;
  }
  .schedule-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 0 16px;
  }
  button {
    cursor: pointer;
    padding: 10px 16px;
    border: 1px solid var(--border);
    color: var(--text);
    background: var(--panel-soft);
    border-radius: 8px;
    margin-top: 12px;
  }
  article {
    border-top: 1px solid var(--border);
    padding: 18px 0;
  }
  small {
    color: var(--muted);
  }
  .notice {
    color: var(--accent);
  }
  .error {
    color: #c34747;
  }
  .hint {
    font-size: 13px;
  }
  @media (max-width: 600px) {
    .settings-page {
      padding: 20px 12px;
    }
    section {
      padding: 16px;
    }
    .schedule-grid {
      grid-template-columns: 1fr;
    }
  }
</style>
