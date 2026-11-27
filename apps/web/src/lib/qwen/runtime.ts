export type QwenProgress = { text: string; phase?: 'download' | 'loading' | 'generating' };
export type ModelPreparation = 'idle' | 'preparing' | 'ready' | 'unsupported' | 'failed';
type WorkerReply = QwenProgress & { kind: 'progress' | 'done' | 'error'; recoverable?: boolean };

// One loaded model per signed-in workspace, not one per turn or session.
export class QwenRuntime {
  private worker: Worker | undefined;
  private cancel: (() => void) | undefined;
  ready = false;

  dispose() {
    this.cancel?.();
    this.worker?.terminate();
    this.worker = undefined;
    this.ready = false;
  }

  run(
    task: 'plan' | 'explain' | 'warmup' | 'agent',
    system: string,
    input: string,
    signal: AbortSignal,
    onProgress: (progress: QwenProgress) => void,
    maxTokens?: number
  ): Promise<string> {
    if (this.cancel) return Promise.reject(new Error('別の端末内処理が動作中です。'));
    if (signal.aborted) return Promise.reject(signal.reason);
    const w = (this.worker ??= new Worker(new URL('./runtime.worker.ts', import.meta.url), {
      type: 'module'
    }));
    return new Promise((resolve, reject) => {
      const start = Date.now();
      let generatingSince: number | undefined;
      let lastProgress = start;
      const timer = setInterval(() => {
        const now = Date.now();
        if (
          generatingSince !== undefined &&
          now - generatingSince >= (task === 'explain' ? 150000 : 210000)
        ) {
          fail('端末内の生成が時間上限に達しました。自動再試行はしていません。');
        } else if (now - start >= 20 * 60000 || now - lastProgress >= 3 * 60000) {
          fail(
            '端末内の処理に進展がないため停止しました。ネットワークやGPUメモリをご確認ください。'
          );
        }
      }, 1000);
      const abort = () => fail('端末での処理を中止しました。');
      const finish = () => {
        clearInterval(timer);
        signal.removeEventListener('abort', abort);
        this.cancel = undefined;
        w.onmessage = w.onerror = w.onmessageerror = null;
      };
      const fail = (message: string, reset = true) => {
        finish();
        if (reset) this.dispose();
        reject(new Error(message));
      };
      this.cancel = abort;
      signal.addEventListener('abort', abort, { once: true });
      w.onerror = w.onmessageerror = () => fail('端末内のモデルを実行できませんでした。');
      w.onmessage = ({ data }: MessageEvent<WorkerReply>) => {
        if (data.kind === 'progress') {
          lastProgress = Date.now();
          if (data.phase === 'generating') {
            this.ready = true;
            generatingSince ??= lastProgress;
          }
          onProgress(data);
        } else if (data.kind === 'done') {
          this.ready = true;
          finish();
          resolve(data.text);
        } else fail(data.text, !data.recoverable);
      };
      w.postMessage({ task, system, input, maxTokens });
    });
  }
}
