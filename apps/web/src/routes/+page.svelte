<script lang="ts">
  import { resolve } from '$app/paths';
  import { onMount, tick } from 'svelte';
  import { onAuthStateChanged, type User } from 'firebase/auth';
  import { api, ApiError } from '#lib/api';
  import { QwenRuntime, type ModelPreparation } from '#lib/qwen/runtime';
  import {
    agentApi,
    analysisErrorCode,
    analysisFailureMessage,
    canRecoverAgentHistory,
    recoverAgentOperation,
    streamAgent,
    type AgentWorkspace
  } from '#lib/agent';
  import AgentReport from '#lib/components/AgentReport.svelte';
  import MessageDocument from '#lib/components/MessageDocument.svelte';
  import VisualizationResult from '#lib/components/VisualizationResult.svelte';
  import { firebaseAuth, firebaseConfigured, loginWithGoogle, logout } from '#lib/firebase';
  import StateSummary from '#lib/components/StateSummary.svelte';
  import BrowserAnalysis from '#lib/components/BrowserAnalysis.svelte';
  import type {
    AnalysisFinding,
    AnalysisHistoryResult,
    AnalysisResponse,
    AnalysisSession,
    AnalysisStreamEvent,
    DatasetEvidence,
    Ga4ConnectionSummary,
    Ga4PropertyCandidate,
    IdentitySummary,
    SourceSetSummary
  } from '#lib/types';

  type ChatMessage = {
    content_format?: 'plain' | 'markdown';
    kind?: 'chat' | 'agent_execution';
    id: string;
    role: 'user' | 'assistant';
    content: string;
    localOnly?: boolean;
  };

  let connectionDialog: HTMLDialogElement;
  let stateDialog: HTMLDialogElement;
  let evidenceDialog: HTMLDialogElement;
  let user: User | null = null;
  let verifiedIdentity: IdentitySummary | null = null;
  let authReady = false;
  let loading = false;
  let error = '';
  let sourceSets: SourceSetSummary[] = [];
  let sessions: AnalysisSession[] = [];
  let selectedSetId = '';
  let activeSession: AnalysisSession | null = null;
  let question = '';
  let messages: ChatMessage[] = [];
  let messageResults: Record<string, AnalysisHistoryResult> = {};
  let latestFindings: AnalysisFinding[] = [];
  let latestEvidence: DatasetEvidence[] = [];
  let streamStage = '';
  let ga4Connection: Ga4ConnectionSummary | null = null;
  let ga4Properties: Ga4PropertyCandidate[] = [];

  let ga4Busy = false;
  let ga4Loading = false;
  let ga4StatusRequest: Promise<void> | null = null;
  let ga4PropertiesLoading = false;
  let ga4PropertiesChecked = false;
  let browserActivity = '';
  let messageStream: HTMLDivElement | undefined;
  const qwenRuntime = new QwenRuntime();
  let modelPreparation: ModelPreparation = 'idle';
  let modelProgress = '';
  let modelAbort: AbortController | null = null;
  let ga4Error = '';
  let agentMode = true;
  let agentWorkspace: AgentWorkspace | null = null;
  let agentWorkspaceError = false;
  let hiddenPropertyIds: string[] = [];
  let agentAbort: AbortController | null = null;
  let operationNotice = '';
  $: visibleSources = sourceSets.filter(
    (s) => !s.ga4_property_id || !hiddenPropertyIds.includes(s.ga4_property_id)
  );
  $: visibleProperties = ga4Properties.filter((p) => !hiddenPropertyIds.includes(p.property_id));
  $: ga4Label = ga4Busy
    ? 'GA4 · 接続を更新中'
    : ga4Loading
      ? 'GA4 · 確認中'
      : ga4Connection?.status === 'connected'
        ? 'GA4 · 接続済み'
        : ga4Connection?.status === 'disconnected'
          ? 'GA4 · 未接続'
          : ga4Connection?.status === 'reauthorization_required'
            ? 'GA4 · 再接続が必要'
            : 'GA4 · 状態未確認';

  function sourceSetName(dataSourceSetId: string): string {
    return (
      sourceSets.find((sourceSet) => sourceSet.id === dataSourceSetId)?.name ?? dataSourceSetId
    );
  }

  function sourceSetPropertyId(dataSourceSetId: string): string | null {
    return (
      sourceSets.find((sourceSet) => sourceSet.id === dataSourceSetId)?.ga4_property_id ?? null
    );
  }

  function sessionLabel(session: AnalysisSession): string {
    return session.title === '新しい分析'
      ? `${sourceSetName(session.data_source_set_id)}｜新しい分析`
      : session.title;
  }

  function loadGa4Connection(current: User) {
    if (ga4StatusRequest) return ga4StatusRequest;
    ga4Loading = true;
    ga4Error = '';
    ga4Properties = [];
    ga4PropertiesChecked = false;
    ga4StatusRequest = api
      .ga4Status(current, AbortSignal.timeout(15000))
      .then(
        (connection) => {
          if (user === current) ga4Connection = connection;
        },
        (reason) => {
          if (user !== current) return;
          ga4Connection = null;
          ga4Error =
            reason instanceof ApiError && reason.code === 'ga4_user_oauth_disabled'
              ? 'この環境ではGA4接続を利用できません。'
              : '接続状態を確認できませんでした。「状態を再確認」をお試しください。';
        }
      )
      .finally(() => {
        if (user === current) {
          ga4Loading = false;
          ga4StatusRequest = null;
        }
      });
    return ga4StatusRequest;
  }

  async function openGa4Connection(current: User) {
    if (!ga4Connection || ga4StatusRequest) await loadGa4Connection(current);
    if (
      user !== current ||
      ga4Connection?.status !== 'connected' ||
      ga4PropertiesLoading ||
      ga4PropertiesChecked
    )
      return;
    ga4PropertiesLoading = true;
    ga4Error = '';
    try {
      const properties = await api.ga4Properties(current, AbortSignal.timeout(25000));
      if (user !== current || ga4Connection?.status !== 'connected') return;
      ga4Properties = properties;
      ga4PropertiesChecked = true;
    } catch (reason) {
      if (user !== current) return;
      if (
        reason instanceof ApiError &&
        reason.code === 'ga4_reauthorization_required' &&
        ga4Connection
      ) {
        ga4Connection = { ...ga4Connection, status: 'reauthorization_required' };
      } else {
        ga4Error = '接続済みですが、プロパティ候補を読み込めませんでした。';
      }
    } finally {
      if (user === current) ga4PropertiesLoading = false;
    }
  }

  async function connectGa4() {
    if (!user) return;
    ga4Busy = true;
    error = '';
    try {
      const result = await api.ga4Connect(user);
      window.location.assign(result.authorization_url);
    } catch (reason) {
      error = reason instanceof ApiError ? reason.code : 'ga4_connect_failed';
      ga4Busy = false;
    }
  }

  async function disconnectGa4() {
    if (!user) return;
    ga4Busy = true;
    error = '';
    try {
      await api.ga4Disconnect(user);
      await loadGa4Connection(user);
    } catch (reason) {
      error = reason instanceof ApiError ? reason.code : 'ga4_disconnect_failed';
    } finally {
      ga4Busy = false;
    }
  }

  function clearHistory() {
    question = '';
    agentWorkspace = null;
    agentWorkspaceError = false;
    messages = [];
    messageResults = {};
    latestFindings = [];
    latestEvidence = [];
    operationNotice = '';
    streamStage = '';
    browserActivity = '';
  }

  async function restoreMessages(current: User, session: AnalysisSession | null) {
    if (!session) return false;
    // Never restore one session/user's results into a subsequently selected workspace.
    const isCurrent = () => user === current && activeSession?.id === session.id;
    const workspaceLoad = agentApi.workspace(current, session.id).then(
      (workspace) => {
        if (!isCurrent()) return;
        agentWorkspace = workspace;
        agentWorkspaceError = false;
      },
      () => {
        if (isCurrent()) agentWorkspaceError = true;
      }
    );
    const historyLoad = api.history(current, session.id).then((history) => {
      if (!isCurrent()) return;
      messages = history.messages.map(({ id, role, content, kind, content_format }) => ({
        id,
        role,
        content,
        kind,
        content_format
      }));
      messageResults = Object.fromEntries(
        history.results.map((result) => [result.message_id, result])
      );
      latestFindings = history.latest_result?.findings ?? [];
      latestEvidence = history.latest_result?.evidence ?? [];
      streamStage = '';
    });
    const [history] = await Promise.allSettled([historyLoad, workspaceLoad]);
    if (history.status === 'rejected') throw history.reason;
    return isCurrent();
  }

  async function loadWorkspace(current: User) {
    loading = true;
    error = '';
    void loadGa4Connection(current);
    try {
      const [identity, sets, history, preferences] = await Promise.all([
        api.me(current).then((identity) => {
          if (user === current) {
            verifiedIdentity = identity;
            if (identity.analysis_provider === 'browser_qwen') void prepareModel(current);
          }
          return identity;
        }),
        api.sourceSets(current),
        api.sessions(current),
        agentApi.preferences(current)
      ]);
      if (user !== current) return;
      verifiedIdentity = identity;
      sourceSets = sets;
      sessions = history;
      hiddenPropertyIds = preferences.hidden_property_ids;
      selectedSetId =
        sourceSets.find((s) => !s.ga4_property_id || !hiddenPropertyIds.includes(s.ga4_property_id))
          ?.id ?? '';
      activeSession = sessions[0] ?? null;
      await restoreMessages(current, activeSession);
    } catch (reason) {
      if (user !== current) return;
      error =
        reason instanceof ApiError
          ? reason.code
          : '分析履歴を読み込めませんでした。再読み込みしてください。';
    } finally {
      if (user === current) loading = false;
    }
  }

  async function prepareModel(current: User) {
    if (user !== current || modelAbort || qwenRuntime.ready) return;
    if (!window.isSecureContext || !navigator.gpu) {
      modelPreparation = 'unsupported';
      modelProgress = '端末での推論に非対応です。対応する定型の集計は利用できます。';
      return;
    }
    const controller = new AbortController();
    modelAbort = controller;
    modelPreparation = 'preparing';
    modelProgress = '分析の準備中。初回は約2.8 GBを読み込みます。';
    try {
      await qwenRuntime.run('warmup', '', '', controller.signal, ({ text }) => {
        if (modelAbort === controller) modelProgress = text;
      });
      if (modelAbort === controller) {
        modelPreparation = 'ready';
        modelProgress = '';
      }
    } catch (reason) {
      if (modelAbort === controller) {
        if (reason instanceof Error && reason.message.startsWith('この端末ではWebGPU')) {
          modelPreparation = 'unsupported';
          modelProgress = '端末での推論に非対応です。対応する定型の集計は利用できます。';
        } else {
          modelPreparation = 'failed';
          modelProgress = reason instanceof Error ? reason.message : '分析の準備に失敗しました。';
        }
      }
    } finally {
      if (modelAbort === controller) modelAbort = null;
    }
  }

  function resetModel() {
    const controller = modelAbort;
    modelAbort = null;
    controller?.abort();
    qwenRuntime.dispose();
    modelPreparation = 'idle';
    modelProgress = '';
  }

  async function signIn() {
    error = '';
    try {
      // The Firebase observer starts workspace loading exactly once.
      await loginWithGoogle();
    } catch (reason) {
      error = reason instanceof Error ? reason.message : 'login_failed';
    }
  }

  async function newSession() {
    if (!user || !selectedSetId) return;
    loading = true;
    error = '';
    try {
      activeSession = await api.createSession(user, selectedSetId);
      sessions = [activeSession, ...sessions];
      clearHistory();
    } catch (reason) {
      error = reason instanceof ApiError ? reason.code : 'session_create_failed';
    } finally {
      loading = false;
    }
  }

  async function selectSession(session: AnalysisSession) {
    if (!user || loading || activeSession?.id === session.id) return;
    const current = user;
    activeSession = session;
    selectedSetId = session.data_source_set_id;
    clearHistory();
    loading = true;
    error = '';
    try {
      await restoreMessages(current, session);
    } catch (reason) {
      if (user === current)
        error = reason instanceof ApiError ? reason.code : '分析履歴を読み込めませんでした。';
    } finally {
      if (user === current) loading = false;
    }
  }

  async function deleteAnalysisSession(session: AnalysisSession) {
    if (!user || loading) return;
    const confirmed = window.confirm(
      'この分析セッションを一覧から削除しますか？監査のためデータはアーカイブされます。'
    );
    if (!confirmed) return;
    loading = true;
    error = '';
    try {
      await api.deleteSession(user, session.id);
      const remainingSessions = sessions.filter((item) => item.id !== session.id);
      sessions = remainingSessions;
      if (activeSession?.id === session.id) {
        activeSession = remainingSessions[0] ?? null;
        if (activeSession) selectedSetId = activeSession.data_source_set_id;
        clearHistory();
        await restoreMessages(user, activeSession);
      }
    } catch (reason) {
      error = reason instanceof ApiError ? reason.code : 'session_delete_failed';
    } finally {
      loading = false;
    }
  }

  function handleComposerKeydown(event: KeyboardEvent) {
    if (event.isComposing || event.key !== 'Enter' || (!event.ctrlKey && !event.metaKey)) {
      return;
    }
    event.preventDefault();
    if (!loading && question.trim()) void sendQuestion();
  }

  function showLocalMessage(id: string, role: 'user' | 'assistant', content: string) {
    messages = [...messages, { id, role, content, localOnly: true }];
    void tick().then(() => {
      if (messageStream) messageStream.scrollTop = messageStream.scrollHeight;
    });
  }

  function showBrowserResult(result: AnalysisResponse, originalId?: string) {
    if (activeSession?.id !== result.session.id || user?.uid !== result.session.user_id) return;
    if (messages.some((m) => m.id === result.assistant_message.id)) return;
    const original = messages.some((m) => m.id === originalId)
      ? messages.map((m) => (m.id === originalId ? result.user_message : m))
      : [...messages, result.user_message];
    messages = [...original, result.assistant_message];
    activeSession = result.session;
    sessions = sessions.map((s) => (s.id === result.session.id ? result.session : s));
    latestFindings = result.findings;
    latestEvidence = result.evidence;
    if (result.visualization)
      messageResults = {
        ...messageResults,
        [result.assistant_message.id]: {
          message_id: result.assistant_message.id,
          findings: result.findings,
          evidence: result.evidence,
          visualization: result.visualization,
          rows: result.rows,
          cache_hit: result.cache_hit
        }
      };
  }

  async function sendQuestion() {
    if (verifiedIdentity?.analysis_provider === 'browser_qwen') return;
    const content = question.trim();
    if (!user || !activeSession || !content || loading) return;
    const currentUser = user;
    const currentSession = activeSession;
    let agentStreamCompleted = false;
    messages = [...messages, { id: `pending_user_${crypto.randomUUID()}`, role: 'user', content }];
    question = '';
    loading = true;
    streamStage = 'accepted';
    try {
      const idempotencyKey = crypto.randomUUID();
      if (agentMode) {
        const currentSessionId = activeSession.id;
        agentAbort = new AbortController();
        agentWorkspace = await streamAgent(
          user,
          currentSessionId,
          content,
          (event) => {
            if (event.event === 'status') streamStage = event.data.stage;
            if (event.event === 'analysis') {
              const result = event.data;
              activeSession = result.session;
              messages = [
                ...messages,
                {
                  id: result.user_message.id,
                  role: result.user_message.role,
                  kind: result.user_message.kind,
                  content: result.user_message.content
                },
                {
                  id: result.assistant_message.id,
                  role: 'assistant',
                  content_format: result.assistant_message.content_format,
                  content: result.assistant_message.content
                }
              ];
              latestFindings = result.findings;
              latestEvidence = result.evidence;
              if (result.visualization)
                messageResults = {
                  ...messageResults,
                  [result.assistant_message.id]: {
                    message_id: result.assistant_message.id,
                    findings: result.findings,
                    evidence: result.evidence,
                    visualization: result.visualization,
                    rows: result.rows,
                    cache_hit: result.cache_hit
                  }
                };
            }
          },
          agentAbort.signal,
          idempotencyKey
        );
        agentStreamCompleted = true;
        sessions = await api.sessions(user);
        activeSession = sessions.find((s) => s.id === currentSessionId) ?? activeSession;
        await restoreMessages(user, activeSession);
        return;
      }
      const onEvent = (event: AnalysisStreamEvent) => {
        if (event.event === 'status') streamStage = event.data.stage;
        if (event.event === 'finding') latestFindings = [...latestFindings, event.data];
        if (event.event === 'evidence') latestEvidence = [...latestEvidence, event.data];
      };
      const result = await api.streamMessage(
        user,
        activeSession.id,
        content,
        idempotencyKey,
        onEvent
      );
      messages = [
        ...messages,
        {
          id: result.assistant_message.id,
          role: 'assistant',
          content_format: result.assistant_message.content_format,
          content: result.assistant_message.content
        }
      ];
      activeSession = result.session;
      sessions = sessions.map((session) =>
        session.id === result.session.id ? result.session : session
      );
      latestFindings = result.findings;
      latestEvidence = result.evidence;
      if (result.visualization) {
        messageResults = {
          ...messageResults,
          [result.assistant_message.id]: {
            message_id: result.assistant_message.id,
            findings: result.findings,
            evidence: result.evidence,
            visualization: result.visualization,
            rows: result.rows,
            cache_hit: result.cache_hit
          }
        };
      }
      streamStage = 'done';
    } catch (reason) {
      const code = agentStreamCompleted
        ? 'agent_history_refresh_failed'
        : analysisErrorCode(reason, agentAbort?.signal.aborted);
      let historyRecovered = false;
      let recoveryFailed = false;
      if (agentMode && canRecoverAgentHistory(code)) {
        streamStage = '保存済みの履歴を確認中';
        try {
          // Read-only, once per failure: never replay an analytics execution automatically.
          historyRecovered = await restoreMessages(currentUser, currentSession);
        } catch {
          recoveryFailed = true;
        }
      }
      if (user?.uid !== currentUser.uid || activeSession?.id !== currentSession.id) return;
      messages = [
        ...messages,
        {
          id: `error_${crypto.randomUUID()}`,
          role: 'assistant',
          content:
            analysisFailureMessage(code, historyRecovered) +
            (recoveryFailed
              ? ' 履歴の再取得にも失敗しました。画面に表示済みの結果は残しています。接続を確認してから再読み込みしてください。'
              : '')
        }
      ];
    } finally {
      loading = false;
      agentAbort = null;
    }
  }

  async function checkSavedOperation() {
    if (!user || !activeSession || loading) return;
    const current = user;
    const session = activeSession;
    loading = true;
    operationNotice = '';
    error = '';
    try {
      const saved = await recoverAgentOperation(current, session.id);
      if (user?.uid !== current.uid || activeSession?.id !== session.id) return;
      if (saved?.result) agentWorkspace = saved.result;
      await restoreMessages(current, session);
      operationNotice =
        !saved || saved.status === 'complete'
          ? '保存済みの回答とグラフを確認しました。追加のデータ取得はしていません。'
          : saved.status === 'running' || saved.status === 'prepared'
            ? '処理中です。少し待ってから保存結果を確認してください。'
            : '処理は中断または失敗しています。保存済みの結果は保持しています。外部APIへの自動再送は行いません。';
    } catch (e) {
      error = e instanceof ApiError ? e.code : 'operation_recovery_failed';
    } finally {
      loading = false;
    }
  }

  onMount(() => {
    const callback = new URL(window.location.href);
    const ga4Result = callback.searchParams.get('ga4');
    const callbackError =
      ga4Result === 'error' ? (callback.searchParams.get('code') ?? 'ga4_connect_failed') : '';
    if (ga4Result) {
      callback.searchParams.delete('ga4');
      callback.searchParams.delete('code');
      window.history.replaceState({}, '', callback);
    }
    const auth = firebaseAuth();
    if (!auth) {
      authReady = true;
      return;
    }
    const unsubscribe = onAuthStateChanged(auth, (current) => {
      if (user !== current) {
        resetModel();
        clearHistory();
        verifiedIdentity = null;
        sourceSets = [];
        sessions = [];
        activeSession = null;
        selectedSetId = '';
        hiddenPropertyIds = [];
        ga4Connection = null;
        ga4Properties = [];
        ga4Error = '';
        ga4Loading = false;
        ga4StatusRequest = null;
        ga4PropertiesLoading = false;
        ga4PropertiesChecked = false;
        loading = false;
      }
      user = current;
      authReady = true;
      if (current) {
        void loadWorkspace(current).finally(() => {
          if (user === current && callbackError) error = callbackError;
        });
      }
    });
    return () => {
      unsubscribe();
      resetModel();
    };
  });
</script>

<svelte:head>
  <title>Analytics AI Workspace</title>
</svelte:head>

{#if !authReady}
  <main class="centered"><div class="spinner" aria-label="読み込み中"></div></main>
{:else if !firebaseConfigured()}
  <main class="centered">
    <section class="setup-card">
      <div class="brand-mark">A</div>
      <p class="eyebrow">Configuration required</p>
      <h1>Analytics AI</h1>
      <p>Firebaseの公開設定を環境変数へ追加すると、Google Loginを開始できます。</p>
      <code>PUBLIC_FIREBASE_PROJECT_ID</code>
      <span class="setup-link">docs/local-development.md を参照</span>
    </section>
  </main>
{:else if !user}
  <main class="login-shell">
    <section class="login-copy">
      <div class="brand"><span class="brand-mark">A</span> Analytics AI</div>
      <p class="eyebrow">Conversational intelligence for your business</p>
      <h1>データを探す時間を、<br /><span>意思決定の時間へ。</span></h1>
      <p class="lead">
        Google Analytics 4のデータを日本語で分析し、グラフと数値の根拠を確認できます。
      </p>
      <div class="trust-row">
        <span>Firebase Auth</span><span>Read only</span><span>Evidence linked</span>
      </div>
    </section>
    <section class="login-card">
      <p class="eyebrow">Secure workspace</p>
      <h2>分析を始める</h2>
      <p>許可されたData Source Setだけが表示されます。</p>
      <button class="google-button" on:click={signIn}>
        <span class="google-g">G</span> Googleでログイン
      </button>
      <p class="empty-small">
        端末内分析を利用する場合、ログイン後に初回約2.8 GBのモデルを読み込みます。
      </p>
      <nav class="public-links" aria-label="サービス情報">
        <a href={resolve('/about')}>サービス紹介</a>
        <a href={resolve('/privacy')}>プライバシーポリシー</a>
        <a href={resolve('/terms')}>利用規約</a>
      </nav>
      {#if error}<p class="error" role="alert">{error}</p>{/if}
    </section>
  </main>
{:else}
  <div class="app-shell">
    <header>
      <div class="brand"><span class="brand-mark small">A</span> Analytics AI</div>
      <div class="header-scope">
        <span>Data Source Set</span>
        <button
          class="connection-trigger"
          aria-haspopup="dialog"
          aria-label={'GA4接続・プロパティ：' + ga4Label}
          aria-busy={ga4Loading || ga4Busy}
          on:click={() => {
            connectionDialog.showModal();
            if (user) void openGa4Connection(user);
          }}
          ><span
            class:connected={ga4Connection?.status === 'connected'}
            class:checking={ga4Loading || ga4Busy}
            class="connection-dot"
          ></span><span aria-live="polite">{ga4Label}</span></button
        >
        <select bind:value={selectedSetId} aria-label="Data Source Set">
          {#each visibleSources as sourceSet (sourceSet.id)}
            <option value={sourceSet.id}>{sourceSet.name}</option>
          {/each}
        </select>
        <button class="primary compact" disabled={!selectedSetId || loading} on:click={newSession}>
          新しい分析
        </button>
      </div>
      <div class="account">
        <div>
          <strong>{user.displayName ?? 'User'}</strong><span>{user.email}</span
          >{#if verifiedIdentity}<code class="verified-uid">UID {verifiedIdentity.uid}</code>{/if}
        </div>
        <button class="icon-button" aria-label="ログアウト" on:click={logout}>↗</button>
      </div>
    </header>

    <aside class="sessions">
      <div class="aside-heading"><span>Sessions</span><span>{sessions.length}</span></div>
      {#if sessions.length === 0}
        <p class="empty-small">分析Sessionはまだありません。</p>
      {:else}
        {#each sessions as session (session.id)}
          <div class:active={activeSession?.id === session.id} class="session-row">
            <button
              class="session-select"
              disabled={loading}
              on:click={() => selectSession(session)}
            >
              <span>{sessionLabel(session)}</span>
              <small>{new Date(session.updated_at).toLocaleString('ja-JP')}</small>
            </button>
            <button
              class="session-delete"
              aria-label="分析セッションを削除"
              title="セッションを削除"
              disabled={loading}
              on:click={() => deleteAnalysisSession(session)}>×</button
            >
          </div>
        {/each}
      {/if}
      {#if verifiedIdentity?.can_manage_data_sources === true}
        <a class="admin-link" href={resolve('/admin')}>データソース管理 <span>→</span></a>
      {/if}
      <a class="admin-link" href={resolve('/settings')}
        >{verifiedIdentity?.analysis_provider === 'browser_qwen'
          ? 'サイト設定'
          : 'サイト設定・定期レポート'} <span>→</span></a
      >
    </aside>

    <main class="workspace">
      {#if error}<div class="banner error" role="alert">
          {error === 'access_denied' && verifiedIdentity
            ? 'Firebase認証は成功しました。User未登録、またはin-memory再起動で登録が消えています。dev bootstrap／Firestore overrideを確認してください。'
            : error}
        </div>{/if}
      {#if !activeSession}
        <section class="empty-state">
          <div class="orb">✦</div>
          <p class="eyebrow">Ready for a new analysis</p>
          <h1>事業について、何を知りたいですか？</h1>
          <p>Data Source Setを選択し「新しい分析」を押してください。</p>
          {#if verifiedIdentity?.analysis_provider === 'browser_qwen' && modelProgress}
            <p role="status">{modelProgress}</p>
            {#if modelPreparation === 'failed' && user}
              <button type="button" class="secondary" on:click={() => user && prepareModel(user)}>
                読み込みをやり直す
              </button>
            {/if}
          {/if}
          <div class="prompt-grid">
            <span>今月のユーザー数は？</span>
            <span>Organicを深掘りして</span>
            <span>前月と比較して</span>
          </div>
        </section>
      {:else}
        <section class="conversation">
          <div class="conversation-head">
            <div>
              <p class="eyebrow">GA4 Property</p>
              <h1>{sourceSetName(activeSession.data_source_set_id)}</h1>
              {#if sourceSetPropertyId(activeSession.data_source_set_id)}
                <span class="property-id">
                  Property {sourceSetPropertyId(activeSession.data_source_set_id)}
                </span>
              {/if}
              <small>{sessionLabel(activeSession)}</small>
            </div>
            <span class="session-id">{activeSession.id.slice(0, 14)}…</span>
            <div class="analysis-context">
              <button
                type="button"
                class="analysis-trigger"
                aria-haspopup="dialog"
                on:click={() => stateDialog.showModal()}>分析条件・状態 <span>LIVE</span></button
              >
              <button
                type="button"
                class="analysis-trigger"
                aria-haspopup="dialog"
                on:click={() => evidenceDialog.showModal()}
                >根拠データ <span>{latestEvidence.length}件</span></button
              >
            </div>
          </div>
          <div class="message-stream" bind:this={messageStream} aria-live="polite">
            {#if verifiedIdentity?.analysis_provider !== 'browser_qwen'}
              <button
                class="secondary compact recovery-action"
                type="button"
                disabled={loading}
                on:click={checkSavedOperation}>保存済みの結果を確認（再取得なし）</button
              >
            {/if}
            {#if operationNotice}<p role="status">{operationNotice}</p>{/if}
            {#if loading && messages.length === 0 && !streamStage}
              <p role="status">保存済みの履歴を読み込み中…</p>
            {:else if messages.length === 0 && !error}
              <div class="welcome-message">
                <span class="assistant-avatar">A</span>
                <div>
                  <strong>分析の準備ができました</strong>
                  <p>期間・指標・フィルタはAnalysis Stateとして引き継がれます。</p>
                </div>
              </div>
            {/if}
            {#each messages as message (message.id)}
              <article class:from-user={message.role === 'user'}>
                <span>{message.role === 'assistant' ? 'AI' : 'You'}</span>
                {#if message.role === 'assistant' && message.kind !== 'agent_execution' && !message.localOnly}
                  <MessageDocument
                    content={message.content}
                    format={message.content_format ?? 'plain'}
                    messageId={message.id}
                  />
                {:else}
                  <p class="plain-message">
                    {#if message.kind === 'agent_execution'}<strong>AIが実行した調査</strong><br
                      />{/if}{message.content}
                  </p>
                {/if}
              </article>
              {#each Object.values(messageResults).filter((result) => (result.parent_message_id ?? result.message_id) === message.id) as result (result.message_id)}
                <VisualizationResult {result} />
              {/each}
            {/each}
            {#if browserActivity}
              <div class="stream-status local-progress" role="status">
                <span class="spinner" aria-hidden="true"></span>
                <p>{browserActivity}</p>
              </div>
            {/if}
            {#if loading && streamStage}
              <div class="stream-status" role="status">
                <span class="spinner" aria-hidden="true"></span>
                <p>分析中: {streamStage}</p>
              </div>
            {/if}
          </div>
          {#if user && verifiedIdentity?.analysis_provider === 'browser_qwen'}
            {#key user.uid + ':' + activeSession.id}
              <BrowserAnalysis
                current={user}
                session={activeSession}
                runtime={qwenRuntime}
                {modelPreparation}
                {modelProgress}
                retryPreparation={() => user && prepareModel(user)}
                agentAvailable={verifiedIdentity.browser_agent_available === true}
                onAgentComplete={async () => {
                  const current = user;
                  const session = activeSession;
                  if (!current || !session) return;
                  const updated = await api.sessions(current);
                  if (user !== current || activeSession?.id !== session.id) return;
                  sessions = updated;
                  activeSession = updated.find((item) => item.id === session.id) ?? session;
                  await restoreMessages(current, activeSession);
                }}
                bind:activity={browserActivity}
                bind:question
                busy={loading}
                onMessage={showLocalMessage}
                onResult={showBrowserResult}
              />
            {/key}
          {:else}
            <form on:submit|preventDefault={sendQuestion}>
              <div class="agent-controls">
                <label
                  ><input
                    type="checkbox"
                    bind:checked={agentMode}
                    disabled={loading}
                  />追加調査・レポート・施策相談</label
                >
                {#if agentAbort}<button type="button" on:click={() => agentAbort?.abort()}
                    >調査を停止</button
                  >{/if}
              </div>
              <textarea
                bind:value={question}
                on:keydown={handleComposerKeydown}
                aria-keyshortcuts="Control+Enter Meta+Enter"
                rows="2"
                maxlength="8000"
                placeholder="例：今月のユーザー数を日別で見せて（Ctrl + Enterで送信）"></textarea>
              <button class="primary" disabled={!question.trim() || loading}>分析する →</button>
            </form>
          {/if}
        </section>
      {/if}
    </main>

    <aside class="context" aria-label="レポート・改善提案">
      <div class="aside-heading">
        <span>レポート・改善提案</span><span aria-live="polite"
          >{loading && streamStage ? '調査中' : 'WORKSPACE'}</span
        >
      </div>
      {#if agentWorkspaceError}
        <p class="empty-small" role="status">レポート・改善提案を読み込めませんでした。</p>
      {/if}
      {#if agentWorkspace && (agentWorkspace.report_requested || agentWorkspace.report_message_id || agentWorkspace.proposals.length || agentWorkspace.memory?.entries.length || agentWorkspace.staged_plan)}<AgentReport
          workspace={agentWorkspace}
          reportMarkdown={messages.find(
            (message) =>
              message.id === agentWorkspace?.report_message_id &&
              message.role === 'assistant' &&
              message.content_format === 'markdown'
          )?.content}
          busy={loading || !!browserActivity}
          forget={async (kind, text) => {
            if (!user || !activeSession || !agentWorkspace || loading) return;
            const currentUser = user;
            const sessionId = activeSession.id;
            loading = true;
            try {
              const saved = await agentApi.deleteWorkingMemory(
                currentUser,
                sessionId,
                agentWorkspace.revision,
                kind,
                text
              );
              if (user?.uid === currentUser.uid && activeSession?.id === sessionId)
                agentWorkspace = saved;
            } catch (reason) {
              error = String(reason);
            } finally {
              loading = false;
            }
          }}
          results={messageResults}
          discuss={(text) => {
            question =
              verifiedIdentity?.analysis_provider === 'browser_qwen'
                ? '新しいデータ取得はせず、' + text
                : text;
            agentMode = true;
          }}
        />{/if}

      {#if !agentWorkspace || (!agentWorkspace.report_requested && !agentWorkspace.report_message_id && !agentWorkspace.proposals.length && !agentWorkspace.memory?.entries.length && !agentWorkspace.staged_plan)}
        <section class="report-placeholder">
          <h2>会話から、次のアクションへ</h2>
          <p>
            レポートの作成を依頼すると、ここに表示します。通常の分析回答は左の会話に表示されます。
          </p>
          <p>改善案ができたら、このパネルから具体化を相談できます。</p>
        </section>
      {/if}
    </aside>
    {#if activeSession}
      <dialog class="analysis-modal" bind:this={stateDialog} aria-labelledby="state-title">
        <div class="modal-heading">
          <h2 id="state-title">分析条件・状態</h2>
          <button
            type="button"
            class="icon-button"
            aria-label="分析条件・状態を閉じる"
            on:click={() => stateDialog.close()}>×</button
          >
        </div>
        <StateSummary state={activeSession.state} />
      </dialog>
      <dialog class="analysis-modal" bind:this={evidenceDialog} aria-labelledby="evidence-title">
        <div class="modal-heading">
          <h2 id="evidence-title">根拠データ</h2>
          <button
            type="button"
            class="icon-button"
            aria-label="根拠データを閉じる"
            on:click={() => evidenceDialog.close()}>×</button
          >
        </div>
        <section class="evidence-empty">
          {#if latestEvidence.length === 0}<p>
              分析結果を取得すると、ここから元データを確認できます。
            </p>{:else}
            {#each latestEvidence as evidence (evidence.id)}<p>
                <code>{evidence.id}</code>{evidence.rows} rows / {evidence.source}
              </p>{/each}
          {/if}
        </section>
      </dialog>
    {/if}
    <dialog
      class="connection-modal"
      bind:this={connectionDialog}
      aria-labelledby="connection-title"
    >
      <div class="modal-heading">
        <div>
          <p class="eyebrow">DATA CONNECTION</p>
          <h2 id="connection-title">Google Analyticsの接続</h2>
        </div>
        <button
          class="icon-button"
          aria-label="接続設定を閉じる"
          on:click={() => connectionDialog.close()}>×</button
        >
      </div>
      {#if ga4Loading}<p role="status">接続情報を確認中…</p>{/if}
      {#if !ga4Loading && ga4Connection}
        <section class="ga4-connection">
          <div class="aside-heading"><span>Google Analytics</span><span>OAuth</span></div>
          {#if ga4Connection.status === 'connected'}
            <strong>接続済み</strong>
            <p>{ga4Connection.google_email}</p>
            {#if ga4PropertiesLoading}
              <p role="status">接続済み · プロパティ候補を取得中…</p>
            {:else if ga4PropertiesChecked && !ga4Error}
              <small>Google側で閲覧可能な候補: {ga4Properties.length}</small>
            {/if}
            <p>
              この一覧は接続確認用です。分析はヘッダーの登録済みData Source
              Setから開始してください。候補の表示だけでは閲覧許可は付与されません。
            </p>
            {#if ga4Properties.length > 0}
              <ul class="ga4-property-list" aria-label="Google側で閲覧可能なGA4プロパティ候補">
                {#each visibleProperties as property (property.property_id)}
                  <li>
                    <span>{property.property_display_name}</span>
                    <small>{property.account_display_name} · {property.property_id}</small>
                  </li>
                {/each}
              </ul>
            {/if}
            <div class="ga4-actions">
              <button class="ga4-reconnect" disabled={ga4Busy} on:click={connectGa4}>再接続</button>
              <button class="ga4-disconnect" disabled={ga4Busy} on:click={disconnectGa4}
                >接続解除</button
              >
            </div>
          {:else}
            <strong>
              {ga4Connection.status === 'reauthorization_required' ? '再接続が必要です' : '未接続'}
            </strong>
            <p>本人が閲覧できるGA4 Propertyへread-onlyで接続します。</p>
            <button class="primary compact" disabled={ga4Busy} on:click={connectGa4}>
              Google Analyticsを接続
            </button>
          {/if}
        </section>
      {/if}
      {#if !ga4Connection && !ga4Loading && user}
        <button class="secondary compact" on:click={() => user && openGa4Connection(user)}
          >状態を再確認</button
        >
      {/if}
      <p class="public-links">
        <a href={resolve('/privacy')}>GA4データの取り扱い（プライバシーポリシー）</a>
      </p>
      {#if ga4Error || error}<p class="error" role="alert">{ga4Error || error}</p>{/if}
    </dialog>
  </div>
{/if}

<style>
  .public-links {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem 0.875rem;
    margin-top: 1rem;
    font-size: 0.875rem;
  }
</style>
