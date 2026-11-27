// Opt-in smoke with a disposable browser profile. Only built worker assets are served.
import { chromium } from '@playwright/test';
import { planInstruction, readDraft, skills } from './src/lib/qwen/contract.ts';
import { createServer } from 'node:http';
import { readdir } from 'node:fs/promises';
import { createReadStream } from 'node:fs';
import { extname, join, relative } from 'node:path';
import { once } from 'node:events';
import { fileURLToPath } from 'node:url';
const root = fileURLToPath(
  new URL('./.svelte-kit/output/client/_app/immutable/workers/', import.meta.url)
);
const files = new Map(
  (await readdir(root, { recursive: true, withFileTypes: true }))
    .filter((f) => f.isFile())
    .map((f) => {
      const path = join(f.parentPath, f.name);
      return ['/_app/immutable/workers/' + relative(root, path).replaceAll('\\', '/'), path];
    })
);
const workerPath = [...files.keys()].find((path) => /\/runtime\.worker-[^/]+\.js$/.test(path));
if (!workerPath) throw new Error('Build the web application first.');
const server = createServer((req, res) => {
  if (req.url === '/') {
    res.setHeader('Content-Type', 'text/html');
    res.end('<title>Qwen smoke</title>');
    return;
  }
  const path = files.get(req.url ?? '');
  if (!path) {
    res.writeHead(404);
    res.end();
    return;
  }
  res.setHeader('Content-Type', extname(path) === '.wasm' ? 'application/wasm' : 'text/javascript');
  createReadStream(path).pipe(res);
});
const warmupOnly = process.argv.includes('--warmup-only');
server.listen(warmupOnly || process.argv.includes('--shared-agent') ? 4192 : 0, '127.0.0.1');
await once(server, 'listening');
let browser;
let context;
try {
  const launchOptions = {
    channel: process.env.QWEN_BROWSER_CHANNEL || undefined,
    headless: true
  };
  // Opt-in dedicated TEST profile to avoid downloading GB of weights on
  // every retry. Never pass the path to a personal Chrome profile.
  const profile = process.env.QWEN_SMOKE_TEST_PROFILE;
  if (profile) context = await chromium.launchPersistentContext(profile, launchOptions);
  else browser = await chromium.launch(launchOptions);
  const page = await (context ?? browser)?.newPage();
  if (!page) throw new Error('Could not start test browser');
  const address = server.address();
  if (!address || typeof address === 'string') throw new Error('No local TCP port');
  await page.goto('http://127.0.0.1:' + address.port);
  const capable = await page.evaluate(
    async () => (await navigator.gpu?.requestAdapter())?.features.has('shader-f16') ?? false
  );
  if (!capable) throw new Error('No WebGPU shader-f16 adapter; model was not downloaded.');
  if (!process.argv.includes('--download')) {
    console.log('WebGPU detected. Pass --download for ~2.8 GB live inference.');
  } else if (warmupOnly) {
    await page.evaluate(
      (path) =>
        new Promise((resolve, reject) => {
          const worker = new Worker(path, { type: 'module' });
          /** @param {string} message */
          const fail = (message) => {
            clearTimeout(timer);
            worker.terminate();
            reject(new Error(message));
          };
          const timer = setTimeout(() => fail('Model preparation timed out'), 180000);
          let gpuChecked = false;
          worker.onerror = () => fail('Worker failed to initialize');
          worker.onmessage = ({ data }) => {
            if (data.kind === 'progress' && data.text.startsWith('GPUの初期動作')) {
              gpuChecked = true;
            } else if (data.kind === 'error') {
              fail(data.text);
            } else if (data.kind === 'done') {
              clearTimeout(timer);
              worker.terminate();
              if (data.text === 'ready' && gpuChecked) resolve(undefined);
              else reject(new Error('GPU warmup was not completed'));
            }
          };
          worker.postMessage({ task: 'warmup', system: '', input: '' });
        }),
      workerPath
    );
    console.log('PASS: the local model and GPU were initialized before the first prompt.');
  } else if (process.argv.includes('--shared-agent')) {
    // Only a local synthetic ASGI fixture. No Google tokens or customer data.
    page.on('console', (m) => {
      if (m.text().startsWith('QWEN:')) console.log(m.text());
    });
    /** @type {{reports: import('./src/lib/types.ts').AnalysisResponse[], phases: string[], workspace?: import('./src/lib/agent.ts').AgentWorkspace}} */
    const result = await page.evaluate(
      ({ path, inspectSynthetic, stopAfterPlan, staged, siteAdvice }) =>
        new Promise((resolve, reject) => {
          const worker = new Worker(path, { type: 'module' });
          /** @type {WebSocket | undefined} */
          let socket;
          let phase = 'warmup';
          let request = '';
          let nextProgress = 0;
          /** @type {string[]} */
          const phases = [];
          /** @type {import('./src/lib/types.ts').AnalysisResponse[]} */
          const reports = [];
          const timer = setTimeout(() => fail('Shared model smoke timed out'), 1200000);
          function finish() {
            clearTimeout(timer);
            worker.terminate();
            socket?.close();
          }
          /** @param {string} message */
          function fail(message) {
            finish();
            reject(new Error(message));
          }
          worker.onerror = () => fail('Worker failed');
          worker.onmessage = ({ data }) => {
            if (data.kind === 'progress') {
              if (Date.now() > nextProgress) {
                nextProgress = Date.now() + 30000;
                console.log('QWEN: ' + phase + ' / ' + data.text);
              }
              return;
            }
            if (data.kind === 'error') {
              fail(data.text);
              return;
            }
            if (socket) {
              // Diagnostic on synthetic data only: format and length, not payload.
              console.log(
                'QWEN: ' +
                  phase +
                  ' / ' +
                  data.text.length +
                  ' generated characters / ' +
                  (data.text.startsWith('{')
                    ? 'object'
                    : data.text.startsWith('<think>')
                      ? 'thinking block'
                      : 'other prefix')
              );
              if ((inspectSynthetic || stopAfterPlan) && phase === 'ga4_arguments')
                console.log('QWEN: synthetic plan / ' + data.text.slice(0, 3000));
              if (inspectSynthetic && (phase === 'advice' || phase === 'advice_grounding'))
                console.log('QWEN: synthetic ' + phase + ' / ' + data.text.slice(0, 3000));
              if (stopAfterPlan && phase === 'ga4_arguments') {
                finish();
                resolve({ reports, phases, workspace: undefined });
                return;
              }
              socket.send(
                JSON.stringify({
                  kind: 'inference_result',
                  request_id: request,
                  ok: true,
                  output: data.text
                })
              );
              return;
            }
            socket = new WebSocket(
              'ws://127.0.0.1:8191/api/sessions/as_orchestrator/browser/agent'
            );
            socket.onerror = () => fail('Synthetic API socket failed');
            socket.onopen = () =>
              socket?.send(
                JSON.stringify({
                  kind: 'start',
                  token: 'user-a',
                  operation_id: crypto.randomUUID(),
                  approval_protocol: 1,
                  content: siteAdvice
                    ? '追加取得は不要です。保存済みの結果とサイト設定の目標・方針に沿って、既存案内の改善案を一つ提案してください。事実と仮説を分けてください。'
                    : staged
                      ? '2026年5月のページビュー数上位5ページを確定してください。その5ページそれぞれについて参照元別のPV上位5件を調べてください。期間の比較とレポート作成は不要です。'
                      : '2026年5月のページとセッションの参照元ごとのPVとUUを表にしてください。'
                })
              );
            socket.onmessage = ({ data: body }) => {
              const message = JSON.parse(body);
              if (message.event === 'inference') {
                const frame = message.data;
                request = frame.id;
                phase = frame.phase;
                phases.push(phase);
                console.log(
                  'QWEN: inference / ' +
                    phase +
                    ' / ' +
                    frame.context.length +
                    ' context characters'
                );
                worker.postMessage({
                  task: 'agent',
                  system:
                    frame.instructions +
                    '\nJSONオブジェクトのみを返してください。次のスキーマに従い、不要な任意項目は省いてください。\n' +
                    JSON.stringify(frame.schema),
                  input: frame.context,
                  maxTokens: frame.max_tokens
                });
              } else if (message.event === 'plan_confirmation') {
                socket?.send(
                  JSON.stringify({
                    kind: 'plan_confirmation',
                    request_id: message.data.id,
                    approved: true
                  })
                );
              } else if (message.event === 'analysis') {
                reports.push(message.data);
                if (inspectSynthetic)
                  console.log('QWEN: synthetic analysis / ' + message.data.visualization?.title);
              } else if (message.event === 'error') fail(message.data.code);
              else if (message.event === 'done') {
                finish();
                resolve({ reports, phases, workspace: message.data });
              }
            };
          };
          worker.postMessage({ task: 'warmup', system: '', input: '' });
        }),
      {
        path: workerPath,
        inspectSynthetic: process.argv.includes('--inspect-synthetic'),
        stopAfterPlan: process.argv.includes('--plan-only'),
        staged: process.argv.includes('--staged'),
        siteAdvice: process.argv.includes('--site-advice')
      }
    );
    if (!result.workspace) {
      if (!process.argv.includes('--plan-only')) throw new Error('Missing smoke workspace');
      console.log('Captured the plan only; GA4 execution was not verified.');
      process.exitCode = 0;
    } else {
      console.log(
        'QWEN: workspace / ' + result.workspace.status + ' / ' + result.workspace.last_error
      );
      if (process.argv.includes('--site-advice')) {
        if (
          result.workspace.status !== 'complete' ||
          result.workspace.completed_queries !== 0 ||
          result.reports.length !== 0 ||
          result.workspace.report_message_id ||
          !result.workspace.proposals.some(
            (p) =>
              p.evidence_ids.length &&
              p.evaluation?.goal_checks?.some(
                (check) =>
                  check.goal_basis === 'site_context' &&
                  check.goal_quote === '来店前の案内を見つけやすくする'
              )
          )
        )
          throw new Error(
            'No validated site proposal with evidence; see synthetic API diagnostics.'
          );
        console.log(
          'PASS: real Qwen applied the saved site goal without fetching GA4 or making a report.'
        );
      } else if (process.argv.includes('--staged')) {
        if (
          result.workspace.completed_queries !== 6 ||
          result.workspace.staged_plan?.phase !== 'complete' ||
          !result.reports[0]?.visualization?.dimensions?.includes('pagePathPlusQueryString') ||
          result.reports.slice(1).length !== 5 ||
          result.reports
            .slice(1)
            .some((r) => !r.visualization?.dimensions?.includes('sessionSource'))
        )
          throw new Error(
            'Qwen did not complete the expected parent ranking and five child reports.'
          );
        console.log(
          'PASS: Qwen planned the parent ranking and the shared agent acquired all five per-page reports.'
        );
      } else if (
        result.workspace.status !== 'complete' ||
        result.workspace.completed_queries !== 1 ||
        !result.reports.some(
          (r) =>
            r.visualization?.dimensions?.includes('pagePathPlusQueryString') &&
            r.visualization?.dimensions?.includes('sessionSource') &&
            r.findings.some((f) => f.metric_label === 'page_views') &&
            r.findings.some((f) => f.metric_label === 'active_users')
        )
      )
        throw new Error(
          'Qwen did not complete the expected joint multi-metric report in one query; phases: ' +
            result.phases.join(', ') +
            '; results: ' +
            result.reports.length
        );
      else
        console.log(
          'PASS: real Qwen sent a validated multi-axis, multi-metric plan through the shared agent.'
        );
    }
  } else {
    page.on('console', (m) => {
      if (m.text().startsWith('QWEN:')) console.log(m.text());
    });
    /** @type {import('./src/lib/qwen/contract.ts').Context} */
    const ctx = {
      revision: 0,
      today: '2026-11-27',
      skills: Object.keys(skills),
      state: { filters: [] },
      terms: [
        { kind: 'metric', name: 'page_views', aliases: ['PV', '閲覧数'] },
        { kind: 'metric', name: 'active_users', aliases: ['UU'] },
        { kind: 'dimension', name: 'page_path', aliases: ['ページ'] }
      ]
    };
    const raw = await page.evaluate(
      ({ path, system, input }) =>
        new Promise((resolve, reject) => {
          const worker = new Worker(path, { type: 'module' });
          let timer = setTimeout(() => {
            worker.terminate();
            reject(new Error('GPU smoke timed out'));
          }, 900000);
          worker.onerror = () => {
            clearTimeout(timer);
            worker.terminate();
            reject(new Error('Worker did not start'));
          };
          let nextProgress = 0;
          let plan = '';
          worker.onmessage = ({ data }) => {
            if (data.kind === 'progress') {
              if (plan && (data.phase === 'loading' || data.phase === 'download')) {
                clearTimeout(timer);
                worker.terminate();
                reject(new Error('Model reloaded between plan and explanation'));
                return;
              }
              if (Date.now() > nextProgress) {
                console.log('QWEN: ' + data.text);
                nextProgress = Date.now() + 30000;
              }
              return;
            }
            clearTimeout(timer);
            if (data.kind === 'done' && !plan) {
              plan = data.text;
              timer = setTimeout(() => {
                worker.terminate();
                reject(new Error('Explanation exceeded the application time limit'));
              }, 150000);
              worker.postMessage({
                task: 'explain',
                system: '日本語で観察と注意点を各1文で説明する。確定済みの集計値のみ根拠にする。',
                input: JSON.stringify({
                  observation: {
                    facts: [{ text: '日別PV: 10月1日 42、10月2日 50' }],
                    returnedRows: 2
                  }
                })
              });
              return;
            }
            worker.terminate();
            if (data.kind === 'done') resolve({ plan, explanation: data.text });
            else reject(new Error(data.text));
          };
          worker.postMessage({ task: 'plan', system, input });
        }),
      {
        path: workerPath,
        system: planInstruction,
        input: JSON.stringify({
          question: '2026年10月の日別PV推移をグラフで見せてください。',
          ...ctx
        })
      }
    );
    const draft = readDraft(raw.plan, ctx);
    if (
      !('plan' in draft) ||
      draft.plan.skill_id !== 'ga4.trend.v1' ||
      draft.plan.metric !== 'page_views' ||
      draft.plan.grain !== 'day' ||
      draft.plan.period.start !== '2026-10-01' ||
      draft.plan.period.end !== '2026-10-31'
    )
      throw new Error('Unexpected plan for the synthetic question.');
    console.log(
      'PASS: browser Qwen generated the correct GA4 skill, dates, metric and grain using the application prompt.'
    );
    if (!raw.explanation?.trim()) throw new Error('Explanation was empty');
    console.log(
      'PASS: explanation generated in the same browser Worker without loading the model again.'
    );
  }
} finally {
  await context?.close();
  await browser?.close();
  server.closeAllConnections();
  server.close();
}
