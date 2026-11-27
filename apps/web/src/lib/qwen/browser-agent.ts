import type { User } from 'firebase/auth';
import { ApiError, apiBase } from '../api';
import type { AgentEvent, AgentWorkspace } from '../agent';
import type { QwenRuntime, QwenProgress } from './runtime';

type InferenceFrame = {
  id: string;
  phase: string;
  instructions: string;
  context: string;
  schema: unknown;
  max_tokens: number;
};

export type BrowserPlanPreview = {
  id: string;
  source: string;
  period: { start: string; end: string } | null;
  comparison_period: { start: string; end: string } | null;
  metrics: string[];
  dimensions: string[];
  filters: { field: string; value: string }[];
  limit: number | null;
  presentation: string | null;
  retry_reason?: string | null;
  staged: { primary: string; secondary: string; limit: number } | null;
};

const browserPhaseLabels: Record<string, string> = {
  selection: '次の分析を判断中',
  ga4_arguments: 'GA4の取得条件を組み立て中',
  advice: 'サイトの目的と根拠から改善案を作成中',
  advice_grounding: '改善案と目的・根拠を照合中',
  summary: '取得済みの根拠から回答を作成中',
  summary_grounding: '回答と根拠を照合中'
};

export async function runBrowserAgent(
  user: User,
  session: string,
  content: string,
  runtime: QwenRuntime,
  signal: AbortSignal,
  onEvent: (event: AgentEvent) => void,
  progress: (progress: QwenProgress) => void,
  confirmPlan: (plan: BrowserPlanPreview) => Promise<boolean>
): Promise<AgentWorkspace> {
  if (!runtime.ready) await runtime.run('warmup', '', '', signal, progress);
  const token = await user.getIdToken();
  signal.throwIfAborted();
  const operation = crypto.randomUUID();
  const url = new URL(
    apiBase + '/api/sessions/' + encodeURIComponent(session) + '/browser/agent',
    window.location.origin
  );
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  const storageKey = `gaai-operation:${user.uid}:${session}`;
  try {
    sessionStorage.setItem(storageKey, operation);
  } catch {
    /* optional */
  }
  const socket = new WebSocket(url);
  const inferenceAbort = new AbortController();

  return new Promise((resolve, reject) => {
    let active = true;
    let inferring = false;
    const timeout = setTimeout(() => fail(new ApiError(504, 'agent_timeout')), 330000);
    const abort = () => fail(new ApiError(499, 'agent_cancelled_by_user'));
    const finish = () => {
      if (!active) return false;
      active = false;
      clearTimeout(timeout);
      signal.removeEventListener('abort', abort);
      inferenceAbort.abort();
      socket.close();
      return true;
    };
    const fail = (error: Error) => {
      if (finish()) reject(error);
    };
    signal.addEventListener('abort', abort, { once: true });
    socket.onopen = () =>
      socket.send(
        JSON.stringify({
          kind: 'start',
          token,
          content,
          operation_id: operation,
          approval_protocol: 1
        })
      );
    socket.onclose = socket.onerror = () => fail(new ApiError(502, 'agent_stream_incomplete'));
    socket.onmessage = async ({ data }: MessageEvent<string>) => {
      if (!active) return;
      try {
        const event = JSON.parse(data) as
          | AgentEvent
          | { event: 'inference'; data: InferenceFrame }
          | { event: 'plan_confirmation'; data: BrowserPlanPreview };
        if (event.event === 'error') {
          fail(new ApiError(502, event.data.code));
        } else if (event.event === 'done') {
          if (!finish()) return;
          try {
            sessionStorage.removeItem(storageKey);
          } catch {
            /* optional */
          }
          resolve(event.data);
        } else if (event.event === 'plan_confirmation') {
          const approved = await confirmPlan(event.data);
          if (active && socket.readyState === WebSocket.OPEN)
            socket.send(
              JSON.stringify({
                kind: 'plan_confirmation',
                request_id: event.data.id,
                approved
              })
            );
        } else if (event.event === 'inference') {
          if (inferring) throw new Error('Concurrent inference request');
          inferring = true;
          const frame = event.data;
          let output = '';
          let ok = false;
          try {
            output = await runtime.run(
              'agent',
              frame.instructions +
                '\nJSONオブジェクトのみを返してください。次のスキーマに従い、不要な任意項目は省いてください。\n' +
                JSON.stringify(frame.schema),
              frame.context,
              inferenceAbort.signal,
              (update) => {
                const label = browserPhaseLabels[frame.phase];
                progress({
                  ...update,
                  text:
                    label && update.phase === 'generating'
                      ? update.text.replace('端末で分析計画を作成中', label)
                      : update.text
                });
              },
              frame.max_tokens
            );
            ok = true;
          } finally {
            inferring = false;
            if (active && socket.readyState === WebSocket.OPEN) {
              socket.send(
                JSON.stringify({ kind: 'inference_result', request_id: frame.id, ok, output })
              );
            }
          }
        } else {
          onEvent(event);
        }
      } catch (reason) {
        fail(reason instanceof Error ? reason : new ApiError(502, 'browser_inference_failed'));
      }
    };
  });
}
