<script lang="ts">
  import { onMount } from 'svelte';
  import { resolve } from '$app/paths';
  import { onAuthStateChanged, type User } from 'firebase/auth';
  import { firebaseAuth } from '#lib/firebase';
  import {
    adminApi,
    type InventoryItem,
    type Registration,
    type RegistrationDefinition
  } from '#lib/admin';

  let user: User | null = null;
  let companies: { id: string; name: string }[] = [];
  let company = '';
  let items: InventoryItem[] = [];
  let next: string | null = null;
  let selected: InventoryItem | null = null;
  let record: Registration | null = null;
  let busy = false;
  let error = '';
  let notice = '';
  let name = '';
  let property = '';
  let stream = '';
  let platform: 'web' | 'android' | 'ios' = 'web';
  let timezone = 'Asia/Tokyo';
  let currency = 'JPY';
  let recipient = '';
  let reviewed = false;

  onMount(() => {
    const auth = firebaseAuth();
    if (!auth) return;
    return onAuthStateChanged(auth, async (current) => {
      user = current;
      companies = [];
      items = [];
      selected = null;
      record = null;
      if (!current) return;
      await run(async () => {
        companies = await adminApi.companies(current);
        company = companies[0]?.id ?? '';
        if (company) await load();
      });
    });
  });
  async function run(action: () => Promise<void>) {
    if (busy) return;
    busy = true;
    error = '';
    notice = '';
    try {
      await action();
    } catch (e) {
      error = e instanceof Error ? e.message : '管理操作に失敗しました';
      if (user && record) {
        try {
          record = await adminApi.read(user, company, record.source_set.id);
        } catch {
          /* Preserve original error; never invent verified state. */
        }
      }
    } finally {
      busy = false;
    }
  }
  async function load(after = '') {
    if (!user || !company) return;
    const page = await adminApi.inventory(user, company, after);
    items = after ? [...items, ...page.items] : page.items;
    next = page.next_cursor;
    if (selected) {
      const fresh = items.find((item) => item.source_set.id === selected?.source_set.id);
      if (fresh) {
        if (
          fresh.source_hash !== selected.source_hash ||
          fresh.grants_hash !== selected.grants_hash
        )
          reviewed = false;
        selected = fresh;
      }
    }
  }
  function select(item: InventoryItem | null) {
    selected = item;
    record = item?.registration ?? null;
    reviewed = false;
    const source = item?.source_set;
    name = source?.name ?? '';
    property = source?.ga4?.property_id ?? '';
    stream = source?.ga4?.stream_scopes?.[0]?.stream_id ?? '';
    platform = source?.ga4?.stream_scopes?.[0]?.platform ?? 'web';
    timezone = source?.ga4?.timezone ?? 'Asia/Tokyo';
    currency = source?.ga4?.currency_code ?? 'JPY';
  }
  async function draft() {
    if (!user) return;
    const definition: RegistrationDefinition = {
      name,
      ga4: {
        property_id: property,
        display_name: name,
        auth_mode: 'USER_OAUTH',
        timezone,
        currency_code: currency,
        stream_scopes: [{ stream_id: stream, platform }]
      }
    };
    await run(async () => {
      record = await adminApi.write(
        user!,
        company,
        selected?.source_set.id,
        selected ? 'legacy' : '',
        selected
          ? {
              definition,
              source_hash: selected.source_hash,
              grants_hash: selected.grants_hash
            }
          : definition
      );
      notice = '下書きを保存しました。接続検証の後に有効化してください。';
    });
  }
  async function transition(action: 'verify' | 'activate') {
    if (!user || !record) return;
    await run(async () => {
      record = await adminApi.write(user!, company, record!.source_set.id, action, {
        expected_revision: record!.revision
      });
      await load();
      notice =
        action === 'activate'
          ? '有効化しました。閲覧許可はUIDを指定して別途付与してください。'
          : '検証できました。まだ閲覧許可は付与されていません。';
    });
  }
  async function grant(enabled: boolean) {
    if (!user || !record) return;
    await run(async () => {
      record = await adminApi.write(user!, company, record!.source_set.id, 'grant', {
        expected_revision: record!.revision,
        user_id: recipient.trim(),
        enabled
      });
      await load();
      notice = enabled ? '指定UIDへ閲覧許可を付与しました。' : '指定UIDの閲覧許可を停止しました。';
    });
  }
  async function disable() {
    if (!user || !selected || !reviewed) return;
    await run(async () => {
      record = await adminApi.write(user!, company, selected!.source_set.id, 'disable', {
        source_hash: selected!.source_hash,
        grants_hash: selected!.grants_hash,
        expected_revision: selected!.registration?.revision ?? null
      });
      await load();
      notice = 'Setと既存Grantを停止しました。会話・根拠の記録は削除していません。';
    });
  }
</script>

<svelte:head><title>データソース管理 — GAAI</title></svelte:head>
<main>
  <header>
    <a href={resolve('/')}>← 分析に戻る</a>
    <h1>データソースの登録・承認</h1>
  </header>
  <p>
    Google側の閲覧権限と、登録済みSet・明示的な閲覧許可のすべてが必要です。管理者にも自動で閲覧許可は付与しません。
  </p>
  {#if error}<p role="alert" class="error">{error}</p>{/if}
  {#if notice}<p role="status">{notice}</p>{/if}
  {#if !user}<p>分析画面でGoogleログインしてください。</p>
  {:else if !companies.length}<p>
      管理可能なCompanyはありません。Company管理者の明示的な登録が必要です。
    </p>
  {:else}
    <label
      >Company<select
        bind:value={company}
        disabled={busy}
        onchange={() => {
          select(null);
          void run(() => load());
        }}
      >
        {#each companies as c (c.id)}<option value={c.id}>{c.name}</option>{/each}
      </select></label
    >
    <section>
      <h2>公開Setの棚卸し</h2>
      <button disabled={busy} onclick={() => void run(() => load())}>再読込み</button>
      <button disabled={busy} onclick={() => select(null)}>新規登録</button>
      {#each items as item (item.source_set.id)}
        <button class="source" disabled={busy} onclick={() => select(item)}>
          <strong>{item.source_set.name}</strong> — {item.source_set.ga4?.property_id ?? 'GA4なし'}
          / {item.registration?.status ?? '未承認（旧Set）'} / {item.source_set.enabled
            ? 'enabled'
            : 'disabled'}
        </button>
      {/each}
      {#if next}<button disabled={busy} onclick={() => void run(() => load(next!))}
          >次の100件</button
        >{/if}
    </section>
    {#if selected}
      <section>
        <h2>対象・現在の閲覧許可</h2>
        <code>{selected.source_set.id}</code>
        <ul>
          {#each selected.grants as g (g.id)}<li>
              {g.user_id} — {g.enabled ? '有効' : '停止中'}
            </li>{/each}
        </ul>
        {#if selected.grants_truncated}<p role="alert">
            Grantが100件を超えています。画面から移行できません。
          </p>{/if}
        <label class="confirmation"
          ><input type="checkbox" bind:checked={reviewed} disabled={busy} /> この対象とGrantを確認しました。承認移行・Set停止では旧Grantを停止し、必要なUIDへ明示的に付与し直します。</label
        >
        <button
          disabled={busy || !reviewed || selected.grants_truncated}
          onclick={() => void disable()}>このSetとGrantを停止</button
        >
      </section>
    {/if}
    {#if !record}
      <section>
        <h2>{selected ? '同じSet IDで承認用の下書き' : '新規登録（USER_OAUTH）'}</h2>
        <form
          onsubmit={(e) => {
            e.preventDefault();
            void draft();
          }}
        >
          <label>名称<input required maxlength="200" bind:value={name} /></label>
          <label>GA4 Property ID<input required pattern="[0-9]+" bind:value={property} /></label>
          <label>Stream ID<input required pattern="[0-9]+" bind:value={stream} /></label>
          <label
            >Platform<select bind:value={platform}
              ><option value="web">web</option><option value="android">android</option><option
                value="ios">ios</option
              ></select
            ></label
          >
          <label>Timezone<input required bind:value={timezone} /></label>
          <label>Currency<input required pattern={'[A-Z]{3}'} bind:value={currency} /></label>
          <button
            type="submit"
            disabled={busy || (!!selected && (!reviewed || selected.grants_truncated))}
            >下書きを登録</button
          >
        </form>
      </section>
    {:else}
      <section>
        <h2>登録状態：{record.status}</h2>
        <p>
          {record.source_set.name} / Property {record.source_set.ga4.property_id} / revision {record.revision}
        </p>
        {#if record.status === 'draft' || record.status === 'verified'}
          <button disabled={busy} onclick={() => void transition('verify')}>自分の接続で検証</button
          >
          <button
            disabled={busy ||
              record.status !== 'verified' ||
              (!!record.legacy_source_hash && !reviewed)}
            onclick={() => void transition('activate')}>検証済みの対象を承認・有効化</button
          >
        {:else if record.status === 'active'}
          <label>閲覧者のFirebase UID<input bind:value={recipient} maxlength="128" /></label>
          <button disabled={busy || !recipient.trim()} onclick={() => void grant(true)}
            >指定UIDに付与</button
          >
          <button disabled={busy || !recipient.trim()} onclick={() => void grant(false)}
            >指定UIDの許可を停止</button
          >
        {:else}<p>停止済みです。自動で再有効化しません。</p>{/if}
      </section>
    {/if}
  {/if}
</main>

<style>
  main {
    max-width: 1080px;
    margin: 0 auto;
    padding: 28px;
    color: #263448;
  }
  section {
    background: white;
    border: 1px solid #dce3ec;
    border-radius: 10px;
    padding: 20px;
    margin: 20px 0;
  }
  h1 {
    font-size: 24px;
  }
  h2 {
    font-size: 18px;
  }
  label {
    display: grid;
    gap: 6px;
    margin: 12px 0;
  }
  form {
    max-width: 640px;
  }
  input,
  select {
    border: 1px solid #c9d3df;
    border-radius: 6px;
    padding: 10px;
    min-width: 0;
  }
  button {
    border: 1px solid #c9d3df;
    border-radius: 6px;
    padding: 10px 14px;
    margin: 6px 6px 6px 0;
    background: #f3f6fa;
    cursor: pointer;
  }
  button:disabled {
    opacity: 0.5;
    cursor: default;
  }
  .source {
    display: block;
    width: 100%;
    text-align: left;
    overflow-wrap: anywhere;
  }
  .confirmation {
    display: flex;
    align-items: start;
  }
  .confirmation input {
    margin-top: 5px;
  }
  .error {
    color: #a12735;
  }
  code,
  li {
    overflow-wrap: anywhere;
  }
</style>
