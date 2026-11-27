import { afterEach, describe, expect, it, vi } from 'vitest';
import type { User } from 'firebase/auth';
import { ApiError } from './api';
import {
  analysisErrorCode,
  analysisFailureMessage,
  canRecoverAgentHistory,
  goalCheckMarkdown,
  streamAgent,
  type AgentWorkspace
} from './agent';

const user = { getIdToken: async () => 'synthetic-test-token' } as User;
const workspace: AgentWorkspace = {
  report_requested: false,
  revision: 1,
  objective: '5月と6月の流入元比較',
  status: 'complete',
  context_revision: 0,
  sections: [],
  proposals: [],
  questions: [],
  open_questions: [],
  completed_queries: 1,
  last_error: null
};
const encode = (text: string) => new TextEncoder().encode(text);
const frame = (event: string, data: unknown) =>
  `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;

describe('goal evidence document', () => {
  it('keeps the source, unknown evidence and a non-automatic next check', () => {
    const body = goalCheckMarkdown({
      goal: '再訪の評価',
      goal_basis: 'site_context',
      goal_quote: '再訪を育てる',
      required_evidence: '再訪の定義と計測',
      evidence_status: 'unmeasured',
      gap: 'PVだけでは再訪を判断できない',
      next_check: '登録指標の定義を確認',
      next_check_kind: 'registered_analysis'
    }).join('\n');
    expect(body).toContain('サイト設定「再訪を育てる」');
    expect(body).toContain('必要な根拠は未取得・未計測');
    expect(body).toContain('PVだけでは再訪を判断できない');
    expect(body).toContain('自動実行しません');
  });

  it('treats a legacy path without source metadata as unconfirmed', () => {
    const body = goalCheckMarkdown({
      goal: '仮の目的',
      required_evidence: '目的の確認',
      evidence_status: 'goal_unconfirmed',
      gap: '目的が未設定',
      next_check: '担当者へ確認',
      next_check_kind: 'business_question'
    }).join('\n');
    expect(body).toContain('仮の目的（未確認）');
    expect(body).not.toContain('undefined');
  });
});

afterEach(() => vi.unstubAllGlobals());

describe('agent stream terminal handling', () => {
  it('returns at done without waiting for connection close or trailing events', async () => {
    const cancel = vi.fn();
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(
          encode(frame('done', workspace) + frame('error', { code: 'must_not_override_done' }))
        );
      },
      cancel
    });
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(body)));
    const onEvent = vi.fn();
    await expect(streamAgent(user, 'session', '質問', onEvent)).resolves.toEqual(workspace);
    expect(onEvent).toHaveBeenCalledTimes(1);
    expect(cancel).toHaveBeenCalledOnce();
    expect(body.locked).toBe(false);
  });

  it('accepts fragmented UTF-8 and CRLF terminal frames', async () => {
    const bytes = encode(frame('done', workspace).replaceAll('\n', '\r\n'));
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        for (const byte of bytes) controller.enqueue(new Uint8Array([byte]));
        controller.close();
      }
    });
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(body)));
    await expect(streamAgent(user, 'session', '質問', vi.fn())).resolves.toEqual(workspace);
  });

  it('classifies missing terminal frame instead of treating it as manual stop', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(new Response(frame('status', { stage: 'planning' })))
    );
    await expect(streamAgent(user, 'session', '質問', vi.fn())).rejects.toMatchObject({
      code: 'agent_stream_incomplete'
    });
  });

  it('preserves a server error when reader cancellation also rejects', async () => {
    const releaseLock = vi.fn();
    const reader = {
      read: vi.fn().mockResolvedValue({
        done: false,
        value: encode(frame('error', { code: 'agent_interrupted' }))
      }),
      cancel: vi.fn().mockRejectedValue(new Error('secondary transport failure')),
      releaseLock
    };
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({ ok: true, body: { getReader: () => reader } })
    );
    await expect(streamAgent(user, 'session', '質問', vi.fn())).rejects.toMatchObject({
      code: 'agent_interrupted'
    });
    expect(releaseLock).toHaveBeenCalledOnce();
  });

  it('retains HTTP status when an upstream failure is not JSON', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(new Response('Gateway timeout', { status: 504 }))
    );
    await expect(streamAgent(user, 'session', '質問', vi.fn())).rejects.toMatchObject({
      status: 504,
      code: 'agent_failed'
    });
  });
});

describe('agent failure presentation', () => {
  it('only attributes an interruption to the user when the local stop signal was set', () => {
    const reason = new ApiError(502, 'agent_interrupted');
    expect(analysisErrorCode(reason)).toBe('agent_interrupted');
    expect(analysisFailureMessage(analysisErrorCode(reason))).not.toContain('調査を停止しました');
    expect(analysisFailureMessage(analysisErrorCode(reason))).toContain('アプリ側');
    expect(analysisErrorCode(reason, true)).toBe('agent_cancelled_by_user');
    expect(analysisFailureMessage(analysisErrorCode(reason, true))).toContain('停止を要求');
  });

  it('identifies network failure and does not promise an unconfirmed saved result', () => {
    const code = analysisErrorCode(new TypeError('Failed to fetch'));
    expect(code).toBe('agent_connection_lost');
    expect(analysisFailureMessage(code)).toContain('処理が続いている可能性');
    expect(analysisFailureMessage(code)).not.toContain('履歴を再読み込みしました');
    expect(analysisFailureMessage(code, true)).toContain('履歴を再読み込みしました');
  });

  it('does not attempt history recovery after authentication or grant denial', () => {
    for (const code of ['access_denied', 'invalid_authentication', 'ga4_property_access_denied']) {
      expect(canRecoverAgentHistory(code)).toBe(false);
    }
    expect(canRecoverAgentHistory('agent_interrupted')).toBe(true);
    expect(canRecoverAgentHistory('agent_stream_incomplete')).toBe(true);
  });
});
