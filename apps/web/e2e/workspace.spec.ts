import { expect, test, type Page } from '@playwright/test';
import { readFile } from 'node:fs/promises';

test('proposal measurement reasoning survives reload and Markdown export with legacy compatibility', async ({
  page
}) => {
  const { responses } = await setupWorkspace(page);
  const legacy = {
    title: '旧提案',
    hypothesis: '未検証の仮説',
    changes: '案内の確認',
    surfaces: ['content'],
    content_action: 'none',
    suggested_title: '',
    suggested_description: '',
    keywords: [],
    validation_plan: '計測方法を確認',
    success_metric: 'page_views',
    evidence_ids: ['evidence-test'],
    priority: 'medium'
  };
  responses['/api/sessions/ui-session/agent'] = {
    revision: 1,
    status: 'complete',
    objective: '案内を探しやすくする',
    context_revision: 1,
    report_requested: true,
    questions: [],
    open_questions: [],
    sections: [{ message_id: 'm', title: '取得結果', evidence_ids: ['evidence-test'], facts: [] }],
    proposals: [
      legacy,
      {
        ...legacy,
        title: '案内改善',
        success_metric: null,
        evaluation: {
          goal_alignment: '来店準備に必要な情報を探しやすくする',
          metric_reason: '適切な計測指標は未確定',
          metric_limitations: 'PV増だけでは到達の容易さを判断できない',
          measurement_status: 'needs_confirmation',
          prerequisites: ['既存導線と計測方法を確認'],
          goal_checks: [
            {
              goal: '必要な案内への到達',
              required_evidence: '案内を見つけられたかの確認',
              evidence_status: 'unmeasured',
              gap: '閲覧数だけでは迷いを判断できない',
              next_check: '担当者と利用時の課題を確認',
              next_check_kind: 'human_review'
            }
          ]
        }
      }
    ]
  };
  await page.goto('/');
  for (let attempt = 0; attempt < 2; attempt++) {
    const report = page.locator('.agent-report');
    await expect(report).toContainText('来店準備に必要な情報を探しやすくする');
    await expect(report).toContainText('PV増だけでは到達の容易さを判断できない');
    await expect(report).toContainText('未確定（計測・定義の確認が必要）');
    await expect(report).toContainText('旧形式のため未記録');
    await expect(report).toContainText('評価したい目的: 必要な案内への到達');
    await expect(report).toContainText('閲覧数だけでは迷いを判断できない');
    await expect(report).toContainText('担当者と利用時の課題を確認');
    if (!attempt) await page.reload();
  }
  const downloaded = page.waitForEvent('download');
  await page.getByRole('button', { name: 'レポートをMarkdownで保存' }).click();
  const file = await downloaded;
  const markdown = await readFile((await file.path())!, 'utf8');
  expect(markdown).toContain('目的との関係: 来店準備に必要な情報を探しやすくする');
  expect(markdown).toContain('指標の限界: PV増だけでは到達の容易さを判断できない');
  expect(markdown).toContain('実施前の確認: 既存導線と計測方法を確認');
  expect(markdown).not.toContain('評価指標候補: null');
  expect(markdown).toContain('必要な根拠: 案内を見つけられたかの確認');
  expect(markdown).toContain('必要な根拠は未取得・未計測');
  expect(markdown).toContain('担当者と利用時の課題を確認');
});

// Synthetic credentials and intercepted APIs only: never bypass application authentication.
test.beforeEach(() => {
  test.skip(
    process.env.PUBLIC_FIREBASE_API_KEY !== 'ui-test-key',
    'Requires synthetic UI configuration'
  );
});

test('restored historical charts stay attached to the answer after reload without a report', async ({
  page
}) => {
  const { responses } = await setupWorkspace(page);
  responses['/api/sessions/ui-session/agent'] = {
    ...(responses['/api/sessions/ui-session/agent'] as Record<string, unknown>),
    report_requested: false
  };
  const results = Array.from({ length: 6 }, (_, i) => ({
    message_id: `attached-${i}`,
    parent_message_id: 'answer',
    source_session_id: 'older-session',
    source_message_id: `source-${i}`,
    findings: [],
    evidence: [],
    cache_hit: true,
    visualization: {
      type: 'bar',
      title: `2026年5月 ページ「/pages/${'long-path-'.repeat(18)}${i}/」｜参照元ごとのページビュー数`,
      subtitle: 'ページビュー数上位5件（取得範囲。全件の内訳ではありません）',
      x: 'sessionSource',
      series: [{ field: 'screenPageViews', label: 'ページビュー数' }],
      dataset_ids: [],
      evidence_ids: []
    },
    rows: [{ sessionSource: 'google', screenPageViews: 100 + i }]
  }));
  responses['/api/sessions/ui-session/history'] = {
    messages: [
      { id: 'answer', role: 'assistant', kind: 'chat', content: '取得済みの根拠を説明します。' }
    ],
    results,
    latest_result: results[5]
  };
  let releaseWorkspace!: () => void;
  const workspaceGate = new Promise<void>((resolve) => (releaseWorkspace = resolve));
  await page.route('**/api/sessions/ui-session/agent', async (route) => {
    await workspaceGate;
    await route.fallback();
  });
  let propertyDiscovery = 0;
  await page.route('**/api/integrations/ga4/properties', async (route) => {
    if (route.request().method() === 'GET') propertyDiscovery++;
    await route.fallback();
  });
  try {
    await page.goto('/');
    await expect(page.getByText('取得済みの根拠を説明します。')).toBeVisible();
    await expect(page.locator('.message-stream .analysis-result')).toHaveCount(6);
    await expect(page.locator('.message-stream .chart canvas')).toHaveCount(6);
    expect(propertyDiscovery).toBe(0);
  } finally {
    releaseWorkspace();
  }
  await expect(page.locator('.visualization-subtitle')).toHaveCount(6);
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page
      .locator('.result-heading > strong')
      .evaluateAll((titles) => titles.every((title) => title.scrollWidth <= title.clientWidth + 1))
  ).toBe(true);
  await expect(page.locator('.result-heading > strong').last()).toContainText('5/');
  await page.reload();
  await expect(page.locator('.message-stream .analysis-result')).toHaveCount(6);
  await expect(page.locator('.message-stream .chart canvas')).toHaveCount(6);
  await expect(
    page.getByText('保存済みの分析結果を再表示しています。追加取得は行っていません。')
  ).toHaveCount(6);
});

test('workspace session loading starts without waiting for the identity or GA4 metadata calls', async ({
  page
}) => {
  await setupWorkspace(page);
  let releaseRequests!: () => void;
  const requestGate = new Promise<void>((resolve) => (releaseRequests = resolve));
  for (const endpoint of ['me', 'integrations/ga4/status']) {
    await page.route(`**/api/${endpoint}`, async (route) => {
      await requestGate;
      await route.fallback();
    });
  }
  const sessionListStarted = page.waitForRequest(
    (request) => request.method() === 'GET' && new URL(request.url()).pathname === '/api/sessions'
  );
  try {
    await page.goto('/');
    await sessionListStarted;
  } finally {
    releaseRequests();
  }
  await expect(page.locator('.context .agent-report')).toBeVisible();
  await page.getByRole('button', { name: /GA4接続・プロパティ/ }).click();
  await expect(page.getByText('Google側で閲覧可能な候補: 0')).toBeVisible();
});

test('quality warnings and rounded comparison labels survive reload', async ({ page }) => {
  const { responses } = await setupWorkspace(page);
  responses['/api/sessions/ui-session/history'] = {
    messages: [
      {
        id: 'quality-answer',
        role: 'assistant',
        kind: 'chat',
        content: '取得範囲のみを示します。',
        content_format: 'markdown'
      }
    ],
    results: [
      {
        message_id: 'quality-chart',
        parent_message_id: 'quality-answer',
        source_message_id: 'saved-result',
        cache_hit: true,
        findings: [
          {
            id: 'quality-finding',
            metric_label: 'セッション数',
            value: '100',
            change_rate: '-0.287654321',
            comparison_kind: 'custom'
          }
        ],
        evidence: [
          {
            quality: {
              sampled: true,
              subject_to_thresholding: true,
              data_loss_from_other_row: true,
              notes: ['result_row_limit_applied', 'unique_count_may_be_approximate']
            }
          }
        ],
        visualization: {
          type: 'kpi',
          title: '検証用KPI',
          subtitle: '取得範囲。全体合計ではありません。',
          series: [],
          dataset_ids: [],
          evidence_ids: []
        },
        rows: []
      }
    ],
    latest_result: null
  };
  await page.goto('/');
  for (let attempt = 0; attempt < 2; attempt++) {
    const result = page.locator('.message-stream .analysis-result');
    await expect(result).toHaveCount(1);
    await expect(result).toContainText('比較期間比 -28.77%');
    await expect(result).toContainText('全体合計ではありません');
    await expect(result).toContainText('サンプリングされた結果');
    await expect(result).toContainText('しきい値');
    await expect(result).toContainText('「(other)」');
    await expect(result).toContainText('全件ではありません');
    await expect(result).not.toContainText('result_row_limit_applied');
    if (attempt === 0) await page.reload();
  }
});

test('Google property discovery is read-only and cannot create application access', async ({
  page
}) => {
  const { responses } = await setupWorkspace(page);
  responses['/api/integrations/ga4/properties'] = [
    {
      property_id: '999999999',
      property_display_name: '未登録の候補',
      account_display_name: '候補アカウント'
    }
  ];
  const mutations: string[] = [];
  page.on('request', (request) => {
    if (request.url().includes('/api/') && !['GET', 'OPTIONS'].includes(request.method())) {
      mutations.push(request.url());
    }
  });
  await page.goto('/');
  await page.getByRole('button', { name: /GA4接続・プロパティ/ }).click();
  const candidates = page.getByRole('list', { name: 'Google側で閲覧可能なGA4プロパティ候補' });
  await expect(candidates.getByText('未登録の候補')).toBeVisible();
  await candidates.getByText('未登録の候補').click();
  await expect(candidates.getByRole('button')).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'このPropertyで分析を開始' })).toHaveCount(0);
  await expect(
    page.getByText('候補の表示だけでは閲覧許可は付与されません。', { exact: false })
  ).toBeVisible();
  await page.getByRole('button', { name: '接続設定を閉じる' }).click();
  await expect(page.locator('select option', { hasText: '未登録の候補' })).toHaveCount(0);
  expect(mutations).toEqual([]);
});

test('administrator explicitly reviews legacy migration and grants access separately', async ({
  page
}) => {
  const { responses } = await setupWorkspace(page);
  responses['/api/admin/companies'] = [{ id: 'company_a', name: 'Company A' }];
  const source = {
    id: 'legacy_web',
    company_id: 'company_a',
    name: '旧Web',
    enabled: true,
    semantic_version: 'ui-v1',
    ga4: {
      property_id: '222222222',
      display_name: '旧Web',
      auth_mode: 'USER_OAUTH',
      timezone: 'Asia/Tokyo',
      currency_code: 'JPY',
      stream_scopes: [{ stream_id: '3333333333', platform: 'web' }]
    }
  };
  const item = {
    source_set: source,
    registration: null as unknown,
    source_hash: 'a'.repeat(64),
    grants_hash: 'b'.repeat(64),
    grants_truncated: false,
    grants: [{ id: 'old-grant', user_id: 'ui-user', enabled: true }]
  };
  responses['/api/admin/companies/company_a/data-source-inventory'] = {
    items: [item],
    next_cursor: null
  };
  const writes: string[] = [];
  let record = {
    source_set: source,
    status: 'draft',
    revision: 1,
    legacy_source_hash: 'a'.repeat(64)
  };
  await page.route(
    '**/api/admin/companies/company_a/data-source-registrations/**',
    async (route) => {
      if (route.request().method() === 'OPTIONS') {
        await route.fulfill({
          status: 200,
          headers: {
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'GET,POST,PUT,OPTIONS',
            'Access-Control-Allow-Headers': '*'
          }
        });
        return;
      }
      const action = new URL(route.request().url()).pathname.split('/').at(-1)!;
      if (route.request().method() !== 'GET') {
        writes.push(action);
        if (action === 'verify') record = { ...record, status: 'verified', revision: 3 };
        if (action === 'activate') {
          record = { ...record, status: 'active', revision: 4 };
          item.grants[0].enabled = false;
        }
        if (action === 'grant') {
          expect(route.request().postDataJSON().user_id).toBe('ui-user');
          record = { ...record, revision: 5 };
        }
        item.registration = record;
      }
      await route.fulfill({ json: record, headers: { 'Access-Control-Allow-Origin': '*' } });
    }
  );
  await page.goto('/admin');
  await page.getByRole('button', { name: /旧Web —/ }).click();
  await expect(page.getByRole('button', { name: '下書きを登録' })).toBeDisabled();
  await page.getByRole('checkbox').check();
  expect(
    await page
      .locator('form')
      .evaluate((form: HTMLFormElement) =>
        [...form.querySelectorAll('input')]
          .filter((input) => !input.checkValidity())
          .map((input) => ({ value: input.value, message: input.validationMessage }))
      )
  ).toEqual([]);
  await page.getByRole('button', { name: '下書きを登録' }).click();
  await expect(page.getByRole('heading', { name: '登録状態：draft' })).toBeVisible();
  await expect(page.getByRole('button', { name: '検証済みの対象を承認・有効化' })).toBeDisabled();
  await page.getByRole('button', { name: '自分の接続で検証' }).click();
  await expect(page.getByRole('heading', { name: '登録状態：verified' })).toBeVisible();
  await page.getByRole('button', { name: '検証済みの対象を承認・有効化' }).click();
  await expect(page.getByRole('heading', { name: '登録状態：active' })).toBeVisible();
  expect(writes).toEqual(['legacy', 'verify', 'activate']);
  await page.getByLabel('閲覧者のFirebase UID').fill('ui-user');
  await page.getByRole('button', { name: '指定UIDに付与' }).click();
  await expect(page.getByRole('status')).toContainText('指定UIDへ閲覧許可');
  expect(writes).toEqual(['legacy', 'verify', 'activate', 'grant']);
});

test('analyst cannot see administrator registration controls', async ({ page }) => {
  const { responses } = await setupWorkspace(page);
  responses['/api/admin/companies'] = [];
  await page.goto('/admin');
  await expect(page.getByText('管理可能なCompanyはありません。', { exact: false })).toBeVisible();
  await expect(page.getByRole('button', { name: '下書きを登録' })).toHaveCount(0);
});

test('browser tier never downloads or falls back when WebGPU is unavailable', async ({ page }) => {
  const { responses } = await setupWorkspace(page);
  responses['/api/me'] = {
    uid: 'ui-user',
    email: 'ui@example.test',
    analysis_provider: 'browser_qwen'
  };
  responses['/api/sessions/ui-session/browser/context'] = {
    revision: 0,
    today: '2026-11-27',
    terms: [],
    skills: [],
    state: {}
  };
  await page.addInitScript(() => Object.defineProperty(navigator, 'gpu', { value: undefined }));
  const unexpected: string[] = [];
  page.on('request', (r) => {
    if (/huggingface\.co|\/messages\/stream|\/agent\/stream|\/browser\/analyze/.test(r.url()))
      unexpected.push(r.url());
  });
  await page.goto('/');
  await page.getByRole('textbox', { name: 'Qwenへの分析依頼' }).fill('2026年10月の日別PV');
  await page.getByRole('button', { name: '送信' }).click();
  await expect(page.getByText(/このブラウザではWebGPUを利用できません/)).toBeVisible();
  expect(unexpected).toEqual([]);
});

async function setupWorkspace(page: Page) {
  const uid = 'ui-user';
  const email = 'ui@example.test';
  const now = Math.floor(Date.now() / 1000);
  const token =
    [{ alg: 'none' }, { sub: uid, aud: 'ui-test', iat: now, exp: now + 3600, auth_time: now }]
      .map((part) => Buffer.from(JSON.stringify(part)).toString('base64url'))
      .join('.') + '.test';
  await page.addInitScript(
    ({ uid, email, token }) => {
      localStorage.setItem(
        'firebase:authUser:ui-test-key:[DEFAULT]',
        JSON.stringify({
          uid,
          email,
          displayName: 'UI test',
          emailVerified: true,
          isAnonymous: false,
          providerData: [{ providerId: 'google.com', uid, email, displayName: 'UI test' }],
          stsTokenManager: {
            refreshToken: 'synthetic-refresh',
            accessToken: token,
            expirationTime: Date.now() + 3600000
          },
          apiKey: 'ui-test-key',
          appName: '[DEFAULT]'
        })
      );
    },
    { uid, email, token }
  );
  await page.route('https://identitytoolkit.googleapis.com/**', (route) =>
    route.fulfill({
      json: { users: [{ localId: uid, email, displayName: 'UI test', emailVerified: true }] }
    })
  );
  const source = {
    id: 'ui-web',
    company_id: 'ui-company',
    name: 'テスト情報サイト',
    has_ga4: true,
    has_bigquery: false,
    ga4_auth_mode: 'USER_OAUTH',
    ga4_property_id: '123'
  };
  const session = {
    id: 'ui-session',
    title: '流入指標の分析',
    user_id: uid,
    company_id: 'ui-company',
    data_source_set_id: source.id,
    data_access_profile_id: null,
    status: 'active',
    revision: 0,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
    state: {
      data_source_set_id: source.id,
      period: { start: '2026-05-01', end: '2026-05-31' },
      comparison_period: null,
      focus: { metric: 'active_users', dimension: null, channel: null, device: null },
      metrics: ['active_users'],
      dimensions: [],
      filters: [],
      time_grain: null,
      selected_skill: null,
      skill_version: null,
      dataset_ids: [],
      findings: [],
      semantic_version: 'ui-v1',
      router_history: []
    }
  };
  const responses: Record<string, unknown> = {
    '/api/me': { uid, email },
    '/api/data-source-sets': [source],
    '/api/sessions': [session],
    '/api/settings/properties': { revision: 0, hidden_property_ids: [] },
    '/api/sessions/ui-session/history': { messages: [], results: [], latest_result: null },
    '/api/sessions/ui-session/agent': {
      report_requested: true,
      revision: 1,
      objective: '5月の流入改善レポート',
      status: 'complete',
      context_revision: 0,
      sections: [],
      proposals: [],
      questions: [],
      open_questions: [],
      completed_queries: 0,
      last_error: null
    },
    '/api/integrations/ga4/status': {
      status: 'connected',
      google_email: email,
      granted_scopes: [],
      connected_at: null,
      last_verified_at: null
    },
    '/api/integrations/ga4/properties': []
  };
  await page.route('**/api/**', async (route) => {
    const path = new URL(route.request().url()).pathname;
    await route.fulfill({
      status: path in responses ? 200 : 404,
      headers: { 'Access-Control-Allow-Origin': '*' },
      json: responses[path] ?? { detail: path }
    });
  });
  return { responses, session };
}

test('browser plan requires explicit approval; local explanation does not execute another report', async ({
  page
}) => {
  const { responses, session } = await setupWorkspace(page);
  const path = '/api/sessions/ui-session/browser/';
  const content = '2026年10月の日別PVを見せてください';
  const period = { start: '2026-10-01', end: '2026-10-31' };
  const plan = { skill_id: 'ga4.trend.v1', metric: 'page_views', period, grain: 'day' };
  responses['/api/me'] = {
    uid: session.user_id,
    email: 'ui@example.test',
    analysis_provider: 'browser_qwen'
  };
  responses[path + 'context'] = {
    revision: 0,
    today: '2026-11-27',
    terms: [{ kind: 'metric', name: 'page_views', aliases: ['PV'] }],
    skills: [plan.skill_id],
    state: { filters: [] }
  };
  responses[path + 'analyze'] = {
    session: { ...session, revision: 1, state: { ...session.state, period } },
    user_message: { id: 'local-q', role: 'user', content },
    assistant_message: { id: 'local-a', role: 'assistant', content: '保存済みのPV推移' },
    rows: [{ date: '20261001', screenPageViews: 42 }],
    findings: [],
    evidence: [],
    visualization: null,
    cache_hit: false
  };
  const calls: string[] = [];
  page.on('request', (r) => {
    if (r.method() === 'POST' && r.url().includes('/api/sessions/'))
      calls.push(new URL(r.url()).pathname);
  });
  await page.addInitScript(() => Object.defineProperty(navigator, 'gpu', { value: {} }));
  let workerLoads = 0;
  await page.route('**/runtime.worker-*.js', (route) => {
    workerLoads++;
    return route.fulfill({
      contentType: 'text/javascript',
      body:
        'self.onmessage=e=>{if(e.data.task==="warmup"){self.postMessage({kind:"progress",phase:"loading",text:"GPUの初期動作を確認中…"}); setTimeout(()=>self.postMessage({kind:"done",text:"ready"}),1500);return}; self.postMessage({kind:"progress",phase:"generating",text:"端末で生成中・1トークン生成"}); setTimeout(()=>self.postMessage({kind:"done",text:e.data.task==="explain" ? "端末内の解説です" : ' +
        JSON.stringify(JSON.stringify({ plan })) +
        '}),500)}'
    });
  });
  await page.goto('/');
  const composer = page.getByRole('textbox', { name: 'Qwenへの分析依頼' });
  await expect(page.getByText('GPUの初期動作を確認中…')).toBeVisible();
  await expect(composer).toBeDisabled();
  await expect(page.getByText('端末内AI · Qwen3.5')).toHaveCount(0);
  expect(workerLoads).toBe(1);
  expect(calls).toEqual([]);
  await expect(composer).toBeEnabled();
  const send = async (input: string) => {
    await composer.fill(input);
    await composer.press('Control+Enter');
  };
  await send('OK');
  await expect(page.getByText(/現在、確認待ちの操作はありません/)).toBeVisible();
  expect(workerLoads).toBe(1);
  await send(content);
  await expect(page.getByText('端末で生成中・1トークン生成')).toBeVisible();
  await expect(page.getByText(/この計画でGA4の分析を実行しますか/)).toBeVisible();
  expect(calls).toEqual([]);
  const sending = page.waitForRequest(
    (r) => r.method() === 'POST' && r.url().endsWith(path + 'analyze')
  );
  await send('はい');
  const req = await sending;
  expect(req.headers()['idempotency-key']).toBeTruthy();
  expect(req.postDataJSON()).toEqual({ revision: 0, plan, content });
  await expect(page.getByText('保存済みのPV推移')).toBeVisible();
  await expect(page.getByText(/取得済みの結果をAIで解説しますか/)).toBeVisible();
  await send('ＯＫ！');
  await expect(page.getByText(/端末内の解説です/)).toBeVisible();
  await expect(
    page.getByRole('button', { name: /Qwenで計画を作る|この計画で分析する/ })
  ).toHaveCount(0);
  expect(workerLoads).toBe(1);
  expect(calls).toEqual([path + 'analyze']);
});

test('an explicit PV ranking proposes a page table without a model and executes only on approval', async ({
  page
}) => {
  const { responses, session } = await setupWorkspace(page);
  const path = '/api/sessions/ui-session/browser/';
  const content = '2026年11月のPVランキングを上位10件で表にしてください。';
  const plan = {
    skill_id: 'ga4.breakdown_rank.v1',
    metric: 'page_views',
    dimension: 'page_path',
    period: { start: '2026-11-01', end: '2026-11-30' },
    limit: 10,
    presentation: 'table'
  };
  responses['/api/me'] = {
    uid: session.user_id,
    email: 'ui@example.test',
    analysis_provider: 'browser_qwen'
  };
  responses[path + 'context'] = {
    revision: 0,
    today: '2026-11-27',
    skills: [plan.skill_id],
    state: session.state,
    terms: [
      { name: 'page_views', kind: 'metric', aliases: ['PV'] },
      { name: 'page_path', kind: 'dimension', aliases: ['ページ'] }
    ]
  };
  responses[path + 'analyze'] = {
    session: { ...session, revision: 1 },
    user_message: { id: 'rank-q', role: 'user', content },
    assistant_message: { id: 'rank-a', role: 'assistant', content: 'ページ別PV上位10件' },
    rows: [{ pagePathPlusQueryString: '/sample', screenPageViews: 42 }],
    findings: [],
    evidence: [],
    cache_hit: false,
    visualization: {
      type: 'table',
      title: 'ページ別PVランキング',
      x: null,
      category: 'pagePathPlusQueryString',
      dimensions: ['pagePathPlusQueryString'],
      series: [{ field: 'screenPageViews', label: 'ページビュー数' }],
      dataset_ids: [],
      evidence_ids: []
    }
  };
  await page.addInitScript(() => Object.defineProperty(navigator, 'gpu', { value: undefined }));
  const dispatched: string[] = [];
  page.on('request', (r) => {
    if (/runtime\.worker-|huggingface\.co|\/browser\/analyze/.test(r.url()))
      dispatched.push(r.url());
  });
  await page.goto('/');
  const composer = page.getByRole('textbox', { name: 'Qwenへの分析依頼' });
  await composer.fill(content);
  await composer.press('Control+Enter');
  await expect(page.getByText(/上位10件.*継続フィルタ/s)).toBeVisible();
  await expect(page.getByText(/表示：表/)).toBeVisible();
  expect(dispatched).toEqual([]);
  const sent = page.waitForRequest(
    (r) => r.method() === 'POST' && r.url().endsWith(path + 'analyze')
  );
  await composer.fill('はい');
  await composer.press('Control+Enter');
  expect((await sent).postDataJSON()).toEqual({ revision: 0, plan, content });
  await expect(page.getByRole('cell', { name: '/sample' })).toBeVisible();
  expect(dispatched).toHaveLength(1);
});

test('shared browser agent waits for chat approval and answers model requests on one socket', async ({
  page
}) => {
  const { responses, session } = await setupWorkspace(page);
  responses['/api/me'] = {
    uid: session.user_id,
    email: 'ui@example.test',
    analysis_provider: 'browser_qwen',
    browser_agent_available: true
  };
  const path = '/api/sessions/ui-session/browser/';
  responses[path + 'context'] = {
    revision: 0,
    today: '2026-11-27',
    terms: [],
    skills: [],
    state: {}
  };
  await page.addInitScript(() => Object.defineProperty(navigator, 'gpu', { value: {} }));
  let workers = 0;
  await page.route('**/runtime.worker-*.js', (route) => {
    workers++;
    return route.fulfill({
      contentType: 'text/javascript',
      body: 'self.onmessage=e=>self.postMessage({kind:"done",text:e.data.task==="warmup" ? "ready" : "{}"})'
    });
  });
  let sockets = 0;
  let calls = 0;
  let approvals = 0;
  let complete: (() => void) | undefined;
  const fallback: string[] = [];
  page.on('request', (r) => {
    if (/\/agent\/stream|\/browser\/analyze/.test(r.url())) fallback.push(r.url());
  });
  const question =
    '2026年5月と6月のセッションの参照元を、セッション数で比較してください。6月を対象、5月を比較基準にして、参照元ごとの差を表で出してください。';
  await page.routeWebSocket('**' + path + 'agent', (ws) => {
    sockets++;
    ws.onMessage((raw) => {
      const frame = JSON.parse(String(raw));
      if (frame.kind === 'start') {
        expect(frame.content).toBe(question);
        expect(frame.token).toBeTruthy();
        expect(frame.approval_protocol).toBe(1);
        ws.send(
          JSON.stringify({
            event: 'inference',
            data: {
              id: 'request',
              phase: 'selection',
              instructions: 'JSON',
              context: '{}',
              schema: { type: 'object' },
              max_tokens: 480
            }
          })
        );
      } else if (frame.kind === 'inference_result') {
        calls++;
        expect(frame).toMatchObject({ request_id: 'request', ok: true, output: '{}' });
        ws.send(
          JSON.stringify({
            event: 'plan_confirmation',
            data: {
              id: 'approve-plan',
              source: 'テスト情報サイト',
              period: { start: '2026-06-01', end: '2026-06-30' },
              comparison_period: { start: '2026-05-01', end: '2026-05-31' },
              dimensions: ['session_source'],
              metrics: ['sessions'],
              filters: [],
              presentation: 'table'
            }
          })
        );
      } else if (frame.kind === 'plan_confirmation') {
        approvals++;
        expect(frame).toEqual({
          kind: 'plan_confirmation',
          request_id: 'approve-plan',
          approved: true
        });
        responses['/api/sessions/ui-session/history'] = {
          messages: [
            { id: 'assistant-1', role: 'assistant', content: '共通エージェントの保存済み結果' }
          ],
          results: [],
          latest_result: null
        };
        ws.send(
          JSON.stringify({
            event: 'analysis',
            data: {
              session,
              user_message: {
                id: 'shared-step',
                role: 'user',
                content: '指定期間の内訳を同じ項目で比較',
                kind: 'agent_step'
              },
              assistant_message: {
                id: 'shared-analysis',
                role: 'assistant',
                content: '取得済み集計'
              },
              rows: [
                { sessionSource: 'google', sessions: 120, dateRange: 'current' },
                { sessionSource: 'google', sessions: 100, dateRange: 'comparison' }
              ],
              findings: [
                {
                  id: 'compare-finding',
                  metric_field: 'sessions',
                  metric_label: 'セッション数',
                  comparison_kind: 'custom',
                  category_comparisons: [
                    {
                      dimensions: { sessionSource: 'google' },
                      current_value: '120',
                      comparison_value: '100',
                      difference: '20',
                      change_rate: '0.2',
                      status: 'matched'
                    },
                    {
                      dimensions: { sessionSource: 'new-source' },
                      current_value: '10',
                      comparison_value: null,
                      difference: null,
                      change_rate: null,
                      status: 'current_only'
                    }
                  ]
                }
              ],
              evidence: [],
              cache_hit: false,
              visualization: {
                type: 'table',
                title: '2026年6月 参照元ごとのセッション数',
                subtitle: '比較元：2026年5月',
                dimensions: ['sessionSource'],
                series: [{ field: 'sessions', label: 'セッション数' }],
                dataset_ids: [],
                evidence_ids: []
              }
            }
          })
        );
        complete = () => ws.send(JSON.stringify({ event: 'done', data: {} }));
      }
    });
  });
  await page.goto('/');
  const composer = page.getByRole('textbox', { name: 'Qwenへの分析依頼' });
  await composer.fill(question);
  await composer.press('Control+Enter');
  await expect(page.getByText(/次の条件でGA4から取得しますか/)).toBeVisible();
  await expect(page.getByText(/対象期間: 2026-06-01/)).toBeVisible();
  await expect(page.getByText(/比較基準: 2026-05-01/)).toBeVisible();
  await expect(page.getByText(/集計軸: 参照元/)).toBeVisible();
  await expect(page.getByText(/端末内AIで調査しますか/)).toHaveCount(0);
  expect([sockets, workers, calls, approvals]).toEqual([1, 1, 1, 0]);
  await expect(page.getByRole('table')).toHaveCount(0);
  await composer.fill('はい');
  await composer.press('Control+Enter');
  const google = page.getByRole('row').filter({ hasText: 'google' });
  await expect(google.getByRole('cell')).toHaveText(['google', '100', '120', '20', '20%']);
  const missing = page.getByRole('row').filter({ hasText: 'new-source' });
  await expect(missing.getByRole('cell')).toHaveText(['new-source', '未確認', '10', '—', '—']);
  await expect(page.getByRole('columnheader', { name: '日付' })).toHaveCount(0);
  expect(complete).toBeDefined();
  complete?.();
  await expect(page.getByText('共通エージェントの保存済み結果')).toBeVisible();
  expect([sockets, workers, calls]).toEqual([1, 1, 1]);
  expect(approvals).toBe(1);
  expect(fallback).toEqual([]);
});

test('GA4 status distinguishes checking from disconnected, and admin nav is granted only by API', async ({
  page
}) => {
  const { responses } = await setupWorkspace(page);
  let adminReads = 0;
  await page.route('**/api/admin/companies', (route) => {
    adminReads++;
    return route.fulfill({
      status: 503,
      json: { detail: { code: 'admin_connection_checks_disabled' } }
    });
  });
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  let statusReads = 0;
  const endpoint = '**/api/integrations/ga4/status';
  await page.route(endpoint, async (route) => {
    statusReads++;
    await gate;
    await route.fulfill({ json: { status: 'disconnected' } });
  });
  try {
    await page.goto('/');
    const trigger = page.getByRole('button', { name: /GA4接続・プロパティ/ });
    await expect(trigger).toHaveAccessibleName(/確認中/);
    await trigger.click();
    await expect(page.getByText('接続情報を確認中…')).toBeVisible();
    await expect(page.getByText('未接続', { exact: true })).toHaveCount(0);
    await expect(page.locator('.sessions a[href="/admin"]')).toHaveCount(0);
    release();
    await expect(trigger).toHaveAccessibleName(/未接続/);
    await expect(page.getByRole('button', { name: 'Google Analyticsを接続' })).toBeVisible();
    expect(statusReads).toBe(1);
  } finally {
    release();
  }
  responses['/api/me'] = {
    uid: 'ui-user',
    email: 'ui@example.test',
    can_manage_data_sources: true
  };
  await page.reload();
  await expect(page.locator('.sessions a[href="/admin"]')).toHaveCount(1);
  expect(adminReads).toBe(0);
});

test('saved day by channel tables default to dates and sort each group independently', async ({
  page
}) => {
  const { responses } = await setupWorkspace(page);
  responses['/api/sessions/ui-session/history'] = {
    messages: [
      { id: 'saved-channel-result', role: 'assistant', content: '取得済みの日別・チャネル別PV' }
    ],
    latest_result: null,
    results: [
      {
        message_id: 'saved-channel-result',
        findings: [],
        evidence: [],
        cache_hit: true,
        visualization: {
          type: 'table',
          title: '日別 × チャネル',
          dimensions: ['date', 'sessionDefaultChannelGroup'],
          series: [{ field: 'screenPageViews', label: 'ページビュー' }]
        },
        rows: [
          { date: '20260515', sessionDefaultChannelGroup: 'Organic Search', screenPageViews: 590 },
          { date: '20260509', sessionDefaultChannelGroup: 'Direct', screenPageViews: 100 },
          { date: '20260501', sessionDefaultChannelGroup: 'Organic Search', screenPageViews: 20 },
          { date: '20260502', sessionDefaultChannelGroup: 'Direct', screenPageViews: 5 }
        ]
      }
    ]
  };
  await page.goto('/');
  const organic = page.getByRole('region', { name: 'Organic Searchの集計表' });
  const direct = page.getByRole('region', { name: 'Directの集計表' });
  await expect(organic.getByRole('row').nth(1)).toContainText('2026-05-01');
  await expect(direct.getByRole('row').nth(1)).toContainText('2026-05-02');
  await expect(organic.getByRole('columnheader', { name: '日付' })).toHaveAttribute(
    'aria-sort',
    'ascending'
  );
  await organic.getByRole('button', { name: 'ページビュー' }).click();
  await organic.getByRole('button', { name: 'ページビュー' }).click();
  await expect(organic.getByRole('row').nth(1)).toContainText('2026-05-15');
  await expect(direct.getByRole('row').nth(1)).toContainText('2026-05-02');
  await expect(organic.getByRole('columnheader', { name: 'ページビュー' })).toHaveAttribute(
    'aria-sort',
    'descending'
  );
});

test('answer documents preview, copy, export and restore safe Markdown on mobile', async ({
  page,
  context
}) => {
  const { responses } = await setupWorkspace(page);
  const content =
    '## 分析のまとめ\n\n確認できた**事実**です。\n\n### /store/\n\n| 参照元 | PV |\n| --- | ---: |\n| google | 123 |\n\n根拠: `evidence_test`\n\n## 改善仮説\n\n- 導線を検証します。\n\n<script>window.markdownInjected=true</script>\n\n![tracking](https://tracker.invalid/pixel)\n\n[危険](javascript:alert%281%29)';
  responses['/api/sessions/ui-session/history'] = {
    messages: [
      { id: 'answer', role: 'assistant', kind: 'chat', content_format: 'markdown', content },
      { id: 'old-answer', role: 'assistant', content: '古い回答\n評価の前提・限界\n・追加取得なし' }
    ],
    results: [],
    latest_result: null
  };
  let tracking = 0;
  page.on('request', (request) => {
    if (request.url().includes('tracker.invalid')) tracking++;
  });
  await context.grantPermissions(['clipboard-read', 'clipboard-write']);
  await page.goto('/');
  const answer = page.locator('.message-document').first();
  await expect(answer.getByRole('heading', { name: '分析のまとめ' })).toBeVisible();
  await expect(answer.getByRole('cell', { name: '123', exact: true })).toBeVisible();
  await expect(answer.locator('script, img, iframe, a')).toHaveCount(0);
  expect(await page.evaluate(() => 'markdownInjected' in window)).toBe(false);
  expect(tracking).toBe(0);
  await answer.getByRole('button', { name: 'Markdown原文', exact: true }).click();
  await expect(answer.getByRole('textbox', { name: '回答のMarkdown原文' })).toHaveValue(content);
  await answer.getByRole('button', { name: 'Markdownをコピー' }).click();
  await expect(answer.getByRole('status')).toHaveText('Markdownをコピーしました');
  // The Windows system clipboard returns CRLF for the same Markdown lines.
  const copied = await page.evaluate(() => navigator.clipboard.readText());
  expect(copied.replaceAll('\r\n', '\n')).toBe(content);
  const downloading = page.waitForEvent('download');
  await answer.getByRole('button', { name: '.md保存' }).click();
  const download = await downloading;
  expect(download.suggestedFilename()).toBe('analysis-answer.md');
  const stream = await download.createReadStream();
  let saved = '';
  for await (const chunk of stream!) saved += chunk.toString();
  expect(saved).toBe(content);
  await page.setViewportSize({ width: 390, height: 844 });
  await answer.getByRole('button', { name: 'プレビュー' }).click();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.reload();
  await expect(answer.getByRole('heading', { name: '分析のまとめ' })).toBeVisible();
  await expect(
    page.locator('.message-document').last().getByRole('heading', { name: '評価の前提・限界' })
  ).toBeVisible();
  expect(tracking).toBe(0);
});

test('staged analysis shows progress without requiring a continuation message', async ({
  page
}) => {
  const { responses } = await setupWorkspace(page);
  responses['/api/sessions/ui-session/agent'] = {
    ...(responses['/api/sessions/ui-session/agent'] as Record<string, unknown>),
    status: 'partial',
    staged_plan: {
      phase: 'children',
      objective: 'ページごとの参照元を分析',
      items: [
        { value: '/page-a', status: 'complete' },
        { value: '/page-b', status: 'pending' }
      ]
    }
  };
  await page.goto('/');
  await expect(page.getByRole('heading', { name: '段階分析の進捗' })).toBeVisible();
  await expect(page.getByText('/page-a：内訳取得済み')).toBeVisible();
  await expect(page.getByText('/page-b：内訳未取得')).toBeVisible();
  await expect(page.getByRole('button', { name: '残りの段階分析を続ける' })).toHaveCount(0);
});

test('Qwen report preview and export use the saved document after reload', async ({ page }) => {
  const { responses, session } = await setupWorkspace(page);
  responses['/api/me'] = {
    uid: session.user_id,
    analysis_provider: 'browser_qwen',
    browser_agent_available: true
  };
  const content = '# 分析レポート｜テスト情報サイト\n\n- 確認済みのPV: 42\n- 根拠: evidence-test';
  responses['/api/sessions/ui-session/agent'] = {
    ...(responses['/api/sessions/ui-session/agent'] as object),
    report_requested: false,
    report_message_id: 'report-previous',
    objective: '別の質問のタイトル',
    proposals: [
      {
        title: '案内の見直し',
        hypothesis: '案内を見つけやすくできる',
        changes: '見出しと配置を見直す',
        surfaces: ['content'],
        content_action: 'rewrite',
        suggested_title: '',
        suggested_description: '',
        keywords: [],
        validation_plan: '担当者が利用時の迷いを確認する',
        success_metric: null,
        evidence_ids: ['evidence-test'],
        priority: 'medium'
      }
    ]
  };
  responses['/api/sessions/ui-session/history'] = {
    messages: [{ id: 'report-previous', role: 'assistant', content, content_format: 'markdown' }],
    results: [],
    latest_result: null
  };
  await page.goto('/');
  const panel = page.locator('.context');
  await expect(panel.getByRole('button', { name: 'レポートをMarkdownで保存' })).toBeVisible();
  await page.reload();
  await panel.getByText('保存済みレポートのプレビュー').click();
  await expect(panel.getByText('確認済みのPV: 42')).toBeVisible();
  const download = page.waitForEvent('download');
  await panel.getByRole('button', { name: 'レポートをMarkdownで保存' }).click();
  expect((await download).suggestedFilename()).toBe('analytics-report.md');
  const stream = await (await download).createReadStream();
  const chunks: Buffer[] = [];
  for await (const chunk of stream!) chunks.push(Buffer.from(chunk));
  expect(Buffer.concat(chunks).toString()).toBe(content);
  await panel.getByRole('button', { name: 'この施策を詰める' }).click();
  await expect(page.getByRole('textbox', { name: 'Qwenへの分析依頼' })).toHaveValue(
    /新しいデータ取得はせず.*案内の見直し/
  );
});

test('site background is saved and restored as context, not permissions', async ({ page }) => {
  const { responses, session } = await setupWorkspace(page);
  responses['/api/me'] = {
    uid: session.user_id,
    analysis_provider: 'browser_qwen',
    browser_agent_available: true
  };
  let saved = {
    revision: 0,
    site_basics: '店舗情報サイト',
    goals: '来店準備を支援',
    technical_constraints: '共通CMSを利用',
    content_policy: '案内を充実させたい',
    allowed_surfaces: [] as string[],
    allow_new_content: false,
    allow_rewrite: false
  };
  await page.route('**/api/data-source-sets/ui-web/site-context', async (route) => {
    const headers = {
      'Access-Control-Allow-Origin': '*',
      'Access-Control-Allow-Methods': 'GET,PUT,OPTIONS',
      'Access-Control-Allow-Headers': '*'
    };
    if (route.request().method() === 'OPTIONS') {
      await route.fulfill({ status: 200, headers });
      return;
    }
    if (route.request().method() === 'PUT') {
      saved = { ...route.request().postDataJSON(), revision: saved.revision + 1 };
    }
    await route.fulfill({ json: saved, headers });
  });
  await page.goto('/settings');
  await expect(page.getByText(/週次・月次の自動作成には対応していません/)).toBeVisible();
  await expect(page.getByText(/禁止事項やアクセス権の設定ではありません/)).toBeVisible();
  await expect(page.getByRole('group', { name: '関心のある改善領域（参考）' })).toBeVisible();
  await expect(page.getByLabel('新規コンテンツ案に関心がある')).not.toBeChecked();
  await page.getByLabel('技術・運営上の背景').fill('店舗担当者が催事情報を更新');
  await page.getByRole('button', { name: 'サイト設定を保存' }).click();
  await expect(page.getByRole('status')).toContainText('サイト設定を保存しました');
  expect(saved.technical_constraints).toBe('店舗担当者が催事情報を更新');
  expect(saved.allowed_surfaces).toEqual([]);
  expect(saved.allow_new_content).toBe(false);
  await page.reload();
  await expect(page.getByLabel('技術・運営上の背景')).toHaveValue('店舗担当者が催事情報を更新');
  await expect(page.getByLabel('サイト基本情報')).toHaveValue('店舗情報サイト');
  await expect(page.getByLabel('新規コンテンツ案に関心がある')).not.toBeChecked();
});

test('memory agreement requires confirmation, edits reset it, deletion and search', async ({
  page
}) => {
  const { responses } = await setupWorkspace(page);
  responses['/api/schedules'] = [];
  responses['/api/data-source-sets/ui-web/site-context'] = {
    revision: 0,
    site_basics: '',
    technical_constraints: '',
    content_policy: '',
    goals: '',
    allowed_surfaces: ['home'],
    allow_new_content: true,
    allow_rewrite: true
  };
  let item: {
    id: string;
    revision: number;
    text: string;
    status: string;
    updated_at: string;
  } | null = null;
  await page.route('**/api/data-source-sets/ui-web/memory/agreements', async (route) => {
    if (route.request().method() === 'OPTIONS') {
      await route.fulfill({
        status: 200,
        headers: {
          'Access-Control-Allow-Origin': '*',
          'Access-Control-Allow-Methods': 'GET,POST,OPTIONS',
          'Access-Control-Allow-Headers': '*'
        }
      });
      return;
    }
    if (route.request().method() === 'GET') {
      await route.fulfill({
        json: item ? [item] : [],
        headers: { 'Access-Control-Allow-Origin': '*' }
      });
      return;
    }
    const change = route.request().postDataJSON();
    item = {
      id: 'agreement-1',
      revision: (item?.revision ?? 0) + 1,
      text: change.text || item?.text || '',
      status: change.action === 'confirm' ? 'confirmed' : 'pending',
      updated_at: '2026-09-14T00:00:00Z'
    };
    if (change.action === 'delete') item = null;
    await route.fulfill({
      json: item ?? { status: 'deleted' },
      headers: { 'Access-Control-Allow-Origin': '*' }
    });
  });
  await page.route('**/api/data-source-sets/ui-web/memory/search?*', (route) =>
    route.fulfill({
      headers: { 'Access-Control-Allow-Origin': '*' },
      json: {
        truncated: false,
        hits: [
          {
            session_id: 'ui-session',
            question: '春ページのPV',
            recorded_at: '2026-05-31',
            section: {
              title: '春のページ分析',
              period: { start: '2026-05-01', end: '2026-05-31' },
              evidence_ids: ['evidence-test']
            }
          }
        ]
      }
    })
  );
  await page.goto('/settings');
  const memory = page.locator('.memory-settings');
  await expect(memory).toBeVisible();
  await memory.getByLabel('記憶する方針').fill('決済は変更不可');
  await memory.getByRole('button', { name: '確認候補として追加' }).click();
  await expect(memory.getByText('未確認の候補', { exact: true })).toBeVisible();
  await memory.getByRole('button', { name: '内容を確認して確定' }).click();
  await expect(memory.getByText('確認済み', { exact: true })).toBeVisible();
  await memory.getByRole('button', { name: '訂正', exact: true }).click();
  await memory.getByLabel('記憶する方針').fill('決済画面も変更可能');
  await memory.getByRole('button', { name: '訂正内容を候補として保存' }).click();
  await expect(memory.getByText('未確認の候補', { exact: true })).toBeVisible();
  await memory.getByRole('button', { name: '記憶から削除' }).click();
  await expect(memory.locator('article')).toHaveCount(0);
  await memory.getByLabel('期間・指標・ページなど').fill('春ページ');
  await memory.getByRole('button', { name: '検索', exact: true }).click();
  await expect(memory).toContainText('春のページ分析');
  await expect(memory).toContainText('2026-05-01');
});

test('authenticated workspace layout and connection dialog', async ({ page }) => {
  await setupWorkspace(page);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('/');
  await expect(page.locator('.context .agent-report')).toBeVisible();
  await expect(page.locator('.message-stream .agent-report')).toHaveCount(0);
  await expect(page.locator('.context')).toContainText('5月の流入改善レポート');
  const conversation = await page.locator('.conversation').boundingBox();
  const context = await page.locator('.context').boundingBox();
  expect(context!.x).toBeGreaterThanOrEqual(conversation!.x + conversation!.width - 1);
  await expect(page.locator('.conversation-head .state-panel')).toHaveCount(0);
  await expect(page.locator('.conversation-head .evidence-empty')).toHaveCount(0);
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 900 });
    for (const name of ['分析条件・状態', '根拠データ']) {
      const opener = page.getByRole('button', { name: new RegExp(`^${name} `) });
      await opener.click();
      const modal = page.getByRole('dialog', { name, exact: true });
      await expect(modal).toBeVisible();
      if (name === '分析条件・状態') {
        await expect(modal.locator('.state-panel')).toBeVisible();
        await expect(modal).toContainText('ui-v1');
      } else {
        await expect(modal.locator('.evidence-empty')).toBeVisible();
        await expect(modal).toContainText('分析結果を取得すると');
      }
      expect((await modal.boundingBox())!.width).toBeLessThanOrEqual(width);
      await page.keyboard.press('Escape');
      await expect(modal).not.toBeVisible();
      await expect(opener).toBeFocused();
      await opener.click();
      await modal.getByRole('button', { name: `${name}を閉じる` }).click();
      await expect(modal).not.toBeVisible();
      await expect(opener).toBeFocused();
    }
  }
  const trigger = page.getByRole('button', { name: /GA4接続・プロパティ/ });
  await trigger.click();
  await expect(page.getByRole('dialog')).toBeVisible();
  await expect(page.getByRole('dialog').locator('.ga4-connection')).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog')).not.toBeVisible();
  await expect(trigger).toBeFocused();
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.locator('.context')).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await trigger.click();
  const dialog = await page.getByRole('dialog').boundingBox();
  expect(dialog!.width).toBeLessThanOrEqual(390);
  await page.getByRole('button', { name: '接続設定を閉じる' }).click();
  await expect(page.getByRole('dialog')).not.toBeVisible();
});

test('server interruption restores persisted findings and is not shown as a user stop', async ({
  page
}) => {
  const { responses } = await setupWorkspace(page);
  let executions = 0;
  await page.route('**/api/sessions/ui-session/agent/stream', async (route) => {
    if (route.request().method() === 'OPTIONS') {
      await route.fulfill({
        status: 200,
        headers: {
          'Access-Control-Allow-Origin': '*',
          'Access-Control-Allow-Methods': 'POST',
          'Access-Control-Allow-Headers': '*'
        }
      });
      return;
    }
    executions++;
    responses['/api/sessions/ui-session/history'] = {
      messages: [
        { id: 'saved-question', role: 'user', content: '2026年5月と6月の流入元を比較して' },
        {
          id: 'saved-finding',
          role: 'assistant',
          content: '保存済みの確認結果：セッション数を取得しました。'
        }
      ],
      results: [],
      latest_result: null
    };
    await route.fulfill({
      status: 200,
      headers: { 'Access-Control-Allow-Origin': '*' },
      contentType: 'text/event-stream',
      body: 'event: error\ndata: {"code":"agent_interrupted"}\n\n'
    });
  });
  await page.goto('/');
  await page.getByRole('textbox').fill('2026年5月と6月の流入元を比較して');
  await page.getByRole('button', { name: '分析する →' }).click();
  await expect(page.locator('.message-stream')).toContainText('保存済みの確認結果');
  await expect(page.locator('.message-stream')).toContainText('アプリ側の処理が中断');
  await expect(page.locator('.message-stream')).toContainText('保存済みの履歴を再読み込みしました');
  await expect(page.locator('.message-stream')).not.toContainText('調査を停止しました');
  await expect(page.getByRole('button', { name: '調査を停止', exact: true })).toHaveCount(0);
  expect(executions).toBe(1);
});
