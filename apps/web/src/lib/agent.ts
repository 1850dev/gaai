import type { User } from 'firebase/auth';
import { apiBase, ApiError, request } from './api';
import type { AnalysisResponse } from './types';

export const surfaces = {
  home: 'トップページ',
  listing: '一覧ページ',
  detail: '詳細ページ',
  content: '記事・コンテンツ',
  cart: 'カート',
  checkout: '決済画面',
  payment: '決済手段',
  speed: 'サイト速度',
  advertising: '広告施策・予算',
  device: 'デバイス限定改善'
} as const;
export type Surface = keyof typeof surfaces;
export type SiteContext = {
  revision: number;
  site_basics: string;
  technical_constraints: string;
  content_policy: string;
  goals: string;
  allowed_surfaces: Surface[];
  allow_new_content: boolean;
  allow_rewrite: boolean;
  updated_at?: string;
};
export const emptySite = (): SiteContext => ({
  revision: 0,
  site_basics: '',
  technical_constraints: '',
  content_policy: '',
  goals: '',
  allowed_surfaces: ['home', 'listing', 'detail', 'content'],
  allow_new_content: true,
  allow_rewrite: true
});
export type Preferences = { revision: number; hidden_property_ids: string[] };
export const goalStatuses = {
  proxy_only: '代理指標のみ。目的達成は未確認',
  unmeasured: '必要な根拠は未取得・未計測',
  goal_unconfirmed: '事業目的の確認が必要'
} as const;
export const goalNextKinds = {
  registered_analysis: '登録済みデータでの分析候補（自動実行しません）',
  measurement_check: '計測・定義を担当者に確認',
  business_question: '事業上の意図を確認',
  human_review: '担当者による確認・調査'
} as const;
export const goalBases = {
  user_request: '今回の依頼',
  site_context: 'サイト設定',
  user_memory: 'ユーザー発言・確認済みの合意',
  unconfirmed: '仮の目的（未確認）'
} as const;
export type GoalCheck = {
  goal: string;
  goal_basis?: keyof typeof goalBases;
  goal_quote?: string | null;
  required_evidence: string;
  evidence_status: keyof typeof goalStatuses;
  gap: string;
  next_check: string;
  next_check_kind: keyof typeof goalNextKinds;
};
export function goalCheckMarkdown(check: GoalCheck): string[] {
  return [
    `### 評価したい目的: ${check.goal}`,
    `- 目的の出典: ${goalBases[check.goal_basis ?? 'unconfirmed']}${check.goal_quote ? `「${check.goal_quote}」` : ''}`,
    `- 必要な根拠: ${check.required_evidence}`,
    `- 現在の確認状況: ${goalStatuses[check.evidence_status]}`,
    `- まだ分からないこと: ${check.gap}`,
    `- 次の確認（${goalNextKinds[check.next_check_kind]}）: ${check.next_check}`,
    ''
  ];
}
export type Proposal = {
  title: string;
  hypothesis: string;
  changes: string;
  surfaces: Surface[];
  content_action: string;
  suggested_title: string;
  suggested_description: string;
  keywords: string[];
  validation_plan: string;
  success_metric: string | null;
  evaluation?: {
    goal_alignment: string;
    metric_reason: string;
    metric_limitations: string;
    measurement_status: 'proxy' | 'needs_confirmation';
    prerequisites: string[];
    goal_checks?: GoalCheck[];
  } | null;
  evidence_ids: string[];
  priority: string;
};
export type AgentWorkspace = {
  memory?: {
    entries: { text: string; kind: string; origin: string; message_id: string }[];
    pending_tasks: string[];
    trimmed: boolean;
  };
  staged_plan?: {
    phase: string;
    objective: string;
    items: { value: string; status: string }[];
  } | null;
  report_requested: boolean;
  report_message_id?: string | null;
  revision: number;
  objective: string;
  status: string;
  context_revision: number;
  proposals_site_revision?: number | null;
  sections: {
    title: string;
    message_id: string;
    finding_ids: string[];
    evidence_ids: string[];
    facts: string[];
    quality_notes: string[];
  }[];
  proposals: Proposal[];
  questions: string[];
  open_questions: string[];
  completed_queries: number;
  last_error: string | null;
};
export type Schedule = {
  id: string;
  revision: number;
  data_source_set_id: string;
  name: string;
  prompt: string;
  frequency: 'weekly' | 'monthly';
  weekday: number;
  monthday: number;
  hour: number;
  timezone: string;
  enabled: boolean;
  next_run_at?: string | null;
  last_session_id?: string | null;
  last_status?: string | null;
  last_error?: string | null;
};
export const agentApi = {
  preferences: (user: User) => request<Preferences>(user, '/api/settings/properties'),
  savePreferences: (user: User, data: Preferences) =>
    request<Preferences>(user, '/api/settings/properties', {
      method: 'PUT',
      body: JSON.stringify(data)
    }),
  site: (user: User, source: string) =>
    request<SiteContext>(user, `/api/data-source-sets/${encodeURIComponent(source)}/site-context`),
  saveSite: (user: User, source: string, data: SiteContext) =>
    request<SiteContext>(user, `/api/data-source-sets/${encodeURIComponent(source)}/site-context`, {
      method: 'PUT',
      body: JSON.stringify(data)
    }),
  workspace: (user: User, session: string) =>
    request<AgentWorkspace>(user, `/api/sessions/${encodeURIComponent(session)}/agent`),
  deleteWorkingMemory: (
    user: User,
    session: string,
    revision: number,
    kind: string,
    text: string
  ) =>
    request<AgentWorkspace>(user, `/api/sessions/${encodeURIComponent(session)}/working-memory`, {
      method: 'POST',
      body: JSON.stringify({ action: 'delete', revision, kind, text })
    }),
  schedules: (user: User) => request<Schedule[]>(user, '/api/schedules'),
  saveSchedule: (user: User, data: Schedule) =>
    request<Schedule>(user, '/api/schedules', { method: 'POST', body: JSON.stringify(data) })
};

export type AgentEvent =
  | { event: 'status'; data: { stage: string } }
  | { event: 'analysis'; data: AnalysisResponse }
  | { event: 'done'; data: AgentWorkspace }
  | { event: 'error'; data: { code: string } };

export function analysisErrorCode(reason: unknown, stoppedByUser = false): string {
  if (stoppedByUser) return 'agent_cancelled_by_user';
  if (reason instanceof ApiError) return reason.code;
  if (reason instanceof TypeError) return 'agent_connection_lost';
  return 'analysis_failed';
}

export function analysisFailureMessage(code: string, historyRecovered = false): string {
  const messages: Record<string, string> = {
    analytics_tools_disabled: 'この環境ではAnalytics Toolが安全確認完了まで無効化されています。',
    ga4_user_connection_required: 'Google Analyticsを接続してから、もう一度分析してください。',
    ga4_reauthorization_required: 'Google Analyticsの再接続が必要です。',
    ga4_property_access_denied:
      'このGoogleアカウントでは現在このGA4 Propertyへアクセスできません。権限を確認してください。',
    access_denied: 'この分析へのアクセス権限を確認できませんでした。',
    agent_cancelled_by_user: '調査の停止を要求しました。',
    browser_plan_declined: '表示した計画でのGA4取得は実行しませんでした。',
    browser_plan_confirmation_timeout:
      '確認の応答期限を過ぎました。承認待ちのGA4取得は行っていません。',
    browser_client_update_required:
      '新しい分析確認画面が必要です。ページを再読み込みしてから、質問を送ってください。',
    agent_interrupted:
      'アプリ側の処理が中断され、調査を完了できませんでした（agent_interrupted）。',
    agent_execution_failed:
      'アプリ側の処理エラーにより、調査を完了できませんでした（agent_execution_failed）。',
    agent_timeout:
      'アプリ側の処理時間の上限に達しました。GA4 APIの利用上限を示すものではありません。',
    agent_stream_incomplete:
      '分析結果の受信が途中で途切れました。サーバー側では処理が続いている可能性があります。',
    agent_connection_lost:
      '分析サーバーとの通信に失敗しました。サーバー側では処理が続いている可能性があります。',
    agent_history_refresh_failed: '分析の完了通知を受信しましたが、履歴の再読み込みに失敗しました。'
  };
  const message = messages[code] ?? `分析を完了できませんでした（${code}）。`;
  if (historyRecovered) return `${message} 保存済みの履歴を再読み込みしました。`;
  return message;
}

export function canRecoverAgentHistory(code: string): boolean {
  return ![
    'access_denied',
    'invalid_authentication',
    'ga4_property_access_denied',
    'ga4_user_connection_required',
    'ga4_reauthorization_required',
    'analytics_tools_disabled'
  ].includes(code);
}

export async function streamAgent(
  user: User,
  session: string,
  content: string,
  onEvent: (event: AgentEvent) => void,
  signal?: AbortSignal,
  operationId = crypto.randomUUID()
): Promise<AgentWorkspace> {
  try {
    sessionStorage.setItem(`gaai-operation:${user.uid}:${session}`, operationId);
  } catch {
    /* Storage may be disabled. Server ledger still applies. */
  }
  const response = await fetch(
    `${apiBase}/api/sessions/${encodeURIComponent(session)}/agent/stream`,
    {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${await user.getIdToken()}`,
        'Content-Type': 'application/json',
        'Idempotency-Key': operationId,
        Accept: 'text/event-stream'
      },
      body: JSON.stringify({ content }),
      signal
    }
  );
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new ApiError(response.status, payload.detail?.code ?? 'agent_failed');
  }
  if (!response.body) throw new ApiError(502, 'agent_stream_incomplete');
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let result: AgentWorkspace | undefined;
  function consume(block: string) {
    const lines = block.split('\n');
    const name = lines
      .find((l) => l.startsWith('event:'))
      ?.slice(6)
      .trim();
    const data = lines
      .filter((l) => l.startsWith('data:'))
      .map((l) => l.slice(5).trimStart())
      .join('\n');
    if (!name || !data) return;
    const event = { event: name, data: JSON.parse(data) } as AgentEvent;
    if (event.event === 'error') throw new ApiError(502, event.data.code);
    onEvent(event);
    if (event.event === 'done') {
      result = event.data;
      try {
        sessionStorage.removeItem(`gaai-operation:${user.uid}:${session}`);
      } catch {
        /* Optional browser storage. */
      }
    }
  }
  try {
    while (true) {
      const { done, value } = await reader.read();
      buffer = (buffer + decoder.decode(value, { stream: !done })).replace(/\r\n/g, '\n');
      let index: number;
      while ((index = buffer.indexOf('\n\n')) >= 0) {
        consume(buffer.slice(0, index));
        buffer = buffer.slice(index + 2);
        // A terminal frame is authoritative; a proxy may keep the connection open.
        if (result) return result;
      }
      if (done) break;
    }
    if (buffer.trim()) consume(buffer);
    if (!result) throw new ApiError(502, 'agent_stream_incomplete');
    return result;
  } finally {
    // A broken stream can reject cancellation too. Preserve the actual terminal error.
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}

export async function recoverAgentOperation(
  user: User,
  session: string
): Promise<{ status: string; result: AgentWorkspace | null } | null> {
  let id: string | null;
  try {
    id = sessionStorage.getItem(`gaai-operation:${user.uid}:${session}`);
  } catch {
    return null;
  }
  if (!id) return null;
  const saved = await request<{ status: string; result: AgentWorkspace | null }>(
    user,
    `/api/sessions/${encodeURIComponent(session)}/agent/operations/${encodeURIComponent(id)}`
  );
  if (saved.status === 'complete')
    sessionStorage.removeItem(`gaai-operation:${user.uid}:${session}`);
  return saved;
}
