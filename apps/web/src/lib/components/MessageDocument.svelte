<script lang="ts">
  import { messageMarkdown, parseMarkdown } from '#lib/markdown';
  import MarkdownNodes from './MarkdownNodes.svelte';
  export let content: string;
  export let format: 'plain' | 'markdown' = 'plain';
  export let messageId: string;
  let sourceVisible = false;
  let notice = '';
  $: markdown = messageMarkdown(content, format);
  $: nodes = parseMarkdown(markdown);

  async function copy() {
    try {
      await navigator.clipboard.writeText(markdown);
      notice = 'Markdownをコピーしました';
    } catch {
      sourceVisible = true;
      notice = 'コピーできませんでした。原文を選択してコピーするか、.md保存をご利用ください。';
    }
  }

  function download() {
    const url = URL.createObjectURL(new Blob([markdown], { type: 'text/markdown;charset=utf-8' }));
    const link = document.createElement('a');
    link.href = url;
    link.download = `analysis-${messageId.replace(/[^a-zA-Z0-9_-]/g, '').slice(0, 64) || 'answer'}.md`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
</script>

<div class="message-document">
  <div class="document-actions" role="group" aria-label="回答の表示・保存">
    <button type="button" aria-pressed={!sourceVisible} on:click={() => (sourceVisible = false)}
      >プレビュー</button
    >
    <button type="button" aria-pressed={sourceVisible} on:click={() => (sourceVisible = true)}
      >Markdown原文</button
    >
    <button type="button" on:click={copy}>Markdownをコピー</button>
    <button type="button" on:click={download}>.md保存</button>
  </div>
  {#if notice}<small role="status">{notice}</small>{/if}
  {#if sourceVisible}
    {#if format === 'plain'}<small
        >既存の平文回答を表示用に整形しています。保存済みの本文は変更していません。</small
      >{/if}
    <textarea class="markdown-source" aria-label="回答のMarkdown原文" readonly value={markdown}
    ></textarea>
  {:else}
    <div class="markdown-body"><MarkdownNodes {nodes} /></div>
  {/if}
</div>
