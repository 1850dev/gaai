import { AutoTokenizer, Qwen3_5ForCausalLM, TextStreamer, env } from '@huggingface/transformers';
import wasmUrl from 'onnxruntime-web/ort-wasm-simd-threaded.asyncify.wasm?url';
import factoryUrl from 'onnxruntime-web/ort-wasm-simd-threaded.asyncify.mjs?url';

const modelId = 'onnx-community/Qwen3.5-4B-ONNX-OPT';
const revision = 'bb072268d0912bdc5cf4f41c8c6f53a03fd68624';
env.allowLocalModels = false;
env.useBrowserCache = true;
if (env.backends.onnx?.wasm) {
  env.backends.onnx.wasm.numThreads = 1;
  env.backends.onnx.wasm.wasmPaths = {
    mjs: new URL(factoryUrl, self.location.href).href,
    wasm: new URL(wasmUrl, self.location.href).href
  };
}
let tokenizer: Awaited<ReturnType<typeof AutoTokenizer.from_pretrained>>;
let model: Awaited<ReturnType<typeof Qwen3_5ForCausalLM.from_pretrained>> | undefined;
let warmed = false;

async function load() {
  if (model) return model;
  const adapter = await navigator.gpu?.requestAdapter();
  if (!adapter?.features.has('shader-f16'))
    throw new Error(
      'この端末ではWebGPU（shader-f16）を利用できません。対応ブラウザ・GPUでお試しください。'
    );
  self.postMessage({ kind: 'progress', phase: 'loading', text: 'モデルを端末に読み込み中…' });
  tokenizer = await AutoTokenizer.from_pretrained(modelId, { revision });
  let last = 0;
  model = await Qwen3_5ForCausalLM.from_pretrained(modelId, {
    revision,
    device: 'webgpu',
    dtype: 'q4f16',
    progress_callback(event) {
      if (event.status === 'progress' && Date.now() - last > 500) {
        last = Date.now();
        self.postMessage({
          kind: 'progress',
          phase: 'download',
          text: `モデルファイルを読み込み中：${Math.round(event.loaded / 1e6)} MB（現在のファイル）`
        });
      }
    }
  });
  return model;
}

self.onmessage = async (
  event: MessageEvent<{
    task?: 'plan' | 'explain' | 'warmup' | 'agent';
    system: string;
    input: string;
    maxTokens?: number;
  }>
) => {
  try {
    const loaded = await load();
    if (event.data.task === 'warmup') {
      if (!warmed) {
        self.postMessage({ kind: 'progress', phase: 'loading', text: 'GPUの初期動作を確認中…' });
        const inputs = tokenizer('Hello', { add_special_tokens: true });
        const output = await loaded.generate({
          ...inputs,
          max_new_tokens: 1,
          do_sample: false
        });
        if ('dispose' in output) output.dispose();
        inputs.input_ids.dispose();
        inputs.attention_mask.dispose();
        warmed = true;
      }
      self.postMessage({ kind: 'done', text: 'ready' });
      return;
    }
    // Additional options are forwarded to the model's Jinja chat template.
    const templateOptions = {
      tokenize: false,
      add_generation_prompt: true,
      enable_thinking: false
    } as const;
    const prompt = tokenizer.apply_chat_template(
      [
        { role: 'system', content: event.data.system.replaceAll('<|', '＜|') },
        { role: 'user', content: event.data.input.replaceAll('<|', '＜|') }
      ],
      templateOptions
    );
    const inputs = tokenizer(prompt, { add_special_tokens: false });
    const agent = event.data.task === 'agent';
    if (inputs.input_ids.dims[1] > (agent ? 16384 : 8192))
      throw new Error('端末用の入力上限を超えました。質問を短くしてください。');
    const explain = event.data.task === 'explain';
    const activity = explain ? '端末で取得済みデータを解説中' : '端末で分析計画を作成中';
    self.postMessage({ kind: 'progress', phase: 'generating', text: activity + '・入力を処理中' });
    let output = '';
    let tokens = 0;
    let lastUpdate = 0;
    await loaded.generate({
      ...inputs,
      max_new_tokens: agent
        ? Math.min(1600, Math.max(128, event.data.maxTokens ?? 600))
        : explain
          ? 144
          : 320,
      do_sample: false,
      streamer: new TextStreamer(tokenizer, {
        skip_prompt: true,
        skip_special_tokens: true,
        token_callback_function: (batch) => {
          tokens += batch.length;
          if (Date.now() - lastUpdate >= 1000) {
            lastUpdate = Date.now();
            self.postMessage({
              kind: 'progress',
              phase: 'generating',
              text: activity + `・${tokens}トークン生成`
            });
          }
        },
        callback_function: (part) => {
          output += part;
        }
      })
    });
    self.postMessage({ kind: 'done', text: output.trim() });
  } catch (e) {
    const msg = e instanceof Error ? e.message : '';
    self.postMessage({
      kind: 'error',
      recoverable: msg.startsWith('端末用'),
      text: /^(この端末|端末用)/.test(msg)
        ? msg
        : 'モデルを実行できません。ネットワーク・空き容量・GPUメモリを確認してください。'
    });
  }
};
