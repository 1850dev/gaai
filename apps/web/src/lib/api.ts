import { PUBLIC_API_BASE_URL } from '$app/env/public';
import type { User } from 'firebase/auth';
import type {
  AnalysisMessage,
  AnalysisResponse,
  AnalysisSession,
  AnalysisStreamEvent,
  Ga4ConnectionSummary,
  Ga4PropertyCandidate,
  IdentitySummary,
  SessionHistoryResponse,
  SourceSetSummary
} from './types';

export const apiBase = PUBLIC_API_BASE_URL.replace(/\/$/, '');

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly code: string
  ) {
    super(code);
  }
}

export async function request<T>(user: User, path: string, init?: RequestInit): Promise<T> {
  const signal = init?.signal;
  signal?.throwIfAborted();
  const token = await (signal
    ? new Promise<string>((resolve, reject) => {
        const abort = () => reject(signal.reason);
        signal.addEventListener('abort', abort, { once: true });
        // The timeout budget also covers Firebase refreshing the ID token.
        void user
          .getIdToken()
          .then(resolve, reject)
          .finally(() => signal.removeEventListener('abort', abort));
      })
    : user.getIdToken());
  const response = await fetch(`${apiBase}${path}`, {
    ...init,
    headers: {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json',
      ...init?.headers
    }
  });
  const payload = (response.status === 204 ? undefined : await response.json()) as
    { detail?: { code?: string } } | undefined;
  if (!response.ok) {
    throw new ApiError(response.status, payload?.detail?.code ?? 'request_failed');
  }
  return payload as T;
}

async function streamMessage(
  user: User,
  sessionId: string,
  content: string,
  idempotencyKey: string,
  onEvent?: (event: AnalysisStreamEvent) => void
): Promise<AnalysisResponse> {
  const token = await user.getIdToken();
  const response = await fetch(`${apiBase}/api/sessions/${sessionId}/messages/stream`, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json',
      'Idempotency-Key': idempotencyKey,
      Accept: 'text/event-stream'
    },
    body: JSON.stringify({ content })
  });
  if (!response.ok) {
    const payload = (await response.json()) as { detail?: { code?: string } };
    throw new ApiError(response.status, payload.detail?.code ?? 'request_failed');
  }
  if (!response.body) throw new ApiError(502, 'stream_body_missing');

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let completed: AnalysisResponse | null = null;

  const consume = (block: string) => {
    let eventName = '';
    const dataLines: string[] = [];
    for (const line of block.split('\n')) {
      if (line.startsWith('event:')) eventName = line.slice(6).trim();
      if (line.startsWith('data:')) dataLines.push(line.slice(5).trimStart());
    }
    if (!eventName || dataLines.length === 0) return;
    const event = {
      event: eventName,
      data: JSON.parse(dataLines.join('\n')) as unknown
    } as AnalysisStreamEvent;
    onEvent?.(event);
    if (event.event === 'error') {
      const status = event.data.code === 'ga4_property_access_denied' ? 403 : 502;
      throw new ApiError(status, event.data.code);
    }
    if (event.event === 'done') completed = event.data;
  };

  while (true) {
    const { done, value } = await reader.read();
    buffer = (buffer + decoder.decode(value, { stream: !done })).replace(/\r\n/g, '\n');
    let boundary = buffer.indexOf('\n\n');
    while (boundary >= 0) {
      consume(buffer.slice(0, boundary));
      buffer = buffer.slice(boundary + 2);
      boundary = buffer.indexOf('\n\n');
    }
    if (done) break;
  }
  if (buffer.trim()) consume(buffer.trim());
  if (!completed) throw new ApiError(502, 'stream_ended_without_done');
  return completed;
}

export const api = {
  me(user: User): Promise<IdentitySummary> {
    return request(user, '/api/me');
  },
  ga4Status(user: User, signal?: AbortSignal): Promise<Ga4ConnectionSummary> {
    return request(user, '/api/integrations/ga4/status', { signal });
  },
  ga4Connect(user: User): Promise<{ authorization_url: string }> {
    return request(user, '/api/integrations/ga4/connect', { method: 'POST' });
  },
  ga4Disconnect(user: User): Promise<void> {
    return request(user, '/api/integrations/ga4/disconnect', { method: 'POST' });
  },
  ga4Properties(user: User, signal?: AbortSignal): Promise<Ga4PropertyCandidate[]> {
    return request(user, '/api/integrations/ga4/properties', { signal });
  },

  sourceSets(user: User): Promise<SourceSetSummary[]> {
    return request(user, '/api/data-source-sets');
  },
  sessions(user: User): Promise<AnalysisSession[]> {
    return request(user, '/api/sessions');
  },
  createSession(user: User, dataSourceSetId: string): Promise<AnalysisSession> {
    return request(user, '/api/sessions', {
      method: 'POST',
      body: JSON.stringify({ data_source_set_id: dataSourceSetId })
    });
  },
  deleteSession(user: User, sessionId: string): Promise<void> {
    return request(user, `/api/sessions/${sessionId}`, { method: 'DELETE' });
  },
  messages(user: User, sessionId: string): Promise<AnalysisMessage[]> {
    return request(user, `/api/sessions/${sessionId}/messages`);
  },
  history(user: User, sessionId: string): Promise<SessionHistoryResponse> {
    return request(user, `/api/sessions/${sessionId}/history`);
  },
  postMessage(
    user: User,
    sessionId: string,
    content: string,
    idempotencyKey = crypto.randomUUID()
  ): Promise<AnalysisResponse> {
    return request(user, `/api/sessions/${sessionId}/messages`, {
      method: 'POST',
      headers: { 'Idempotency-Key': idempotencyKey },
      body: JSON.stringify({ content })
    });
  },
  streamMessage(
    user: User,
    sessionId: string,
    content: string,
    idempotencyKey = crypto.randomUUID(),
    onEvent?: (event: AnalysisStreamEvent) => void
  ): Promise<AnalysisResponse> {
    return streamMessage(user, sessionId, content, idempotencyKey, onEvent);
  }
};
