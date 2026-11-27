import { afterEach, expect, it, vi } from 'vitest';
import { QwenRuntime, type QwenProgress } from './runtime';

class WorkerStub {
  static instances: WorkerStub[] = [];
  onmessage: ((event: { data: QwenProgress & { kind: string } }) => void) | null = null;
  terminate = vi.fn();
  postMessage = vi.fn();
  constructor() {
    WorkerStub.instances.push(this);
  }
  send(kind: string, text: string, phase?: QwenProgress['phase']) {
    this.onmessage?.({ data: { kind, text, phase } });
  }
}

afterEach(() => {
  WorkerStub.instances = [];
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

it('reuses the loaded worker for a second task and disposes it on logout', async () => {
  vi.stubGlobal('Worker', WorkerStub);
  const runtime = new QwenRuntime();
  const signal = new AbortController().signal;
  expect(WorkerStub.instances).toHaveLength(0);
  const plan = runtime.run('plan', '', '', signal, vi.fn());
  const worker = WorkerStub.instances[0];
  worker.send('done', 'plan');
  await expect(plan).resolves.toBe('plan');
  const explanation = runtime.run('explain', '', '', signal, vi.fn());
  worker.send('done', 'done');
  await expect(explanation).resolves.toBe('done');
  expect(WorkerStub.instances).toHaveLength(1);
  expect(worker.terminate).not.toHaveBeenCalled();
  runtime.dispose();
  expect(worker.terminate).toHaveBeenCalledOnce();
});

it('bounds generation even with ongoing progress and never recreates the worker itself', async () => {
  vi.stubGlobal('Worker', WorkerStub);
  vi.useFakeTimers();
  const runtime = new QwenRuntime();
  const result = runtime.run('explain', '', '', new AbortController().signal, vi.fn());
  const checked = expect(result).rejects.toThrow('時間上限');
  const worker = WorkerStub.instances[0];
  worker.send('progress', 'started', 'generating');
  await vi.advanceTimersByTimeAsync(140000);
  worker.send('progress', 'tokens', 'generating');
  await vi.advanceTimersByTimeAsync(10000);
  await checked;
  expect(worker.terminate).toHaveBeenCalledOnce();
  expect(WorkerStub.instances).toHaveLength(1);
});
