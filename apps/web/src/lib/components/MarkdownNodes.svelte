<script lang="ts">
  import type { MarkdownNode } from '#lib/markdown';
  export let nodes: MarkdownNode[];
</script>

{#each nodes as node, index (index)}
  {#if node.type === 'heading'}
    <svelte:element this={`h${node.depth}`}
      ><svelte:self nodes={node.children ?? []} /></svelte:element
    >
  {:else if node.type === 'paragraph'}
    <p><svelte:self nodes={node.children ?? []} /></p>
  {:else if node.type === 'strong'}
    <strong><svelte:self nodes={node.children ?? []} /></strong>
  {:else if node.type === 'em'}
    <em><svelte:self nodes={node.children ?? []} /></em>
  {:else if node.type === 'del'}
    <del><svelte:self nodes={node.children ?? []} /></del>
  {:else if node.type === 'blockquote'}
    <blockquote><svelte:self nodes={node.children ?? []} /></blockquote>
  {:else if node.type === 'list'}
    {#if node.ordered}<ol start={node.start}><svelte:self nodes={node.children ?? []} /></ol>
    {:else}<ul><svelte:self nodes={node.children ?? []} /></ul>{/if}
  {:else if node.type === 'item'}
    <li><svelte:self nodes={node.children ?? []} /></li>
  {:else if node.type === 'table'}
    <!-- Keyboard users need a focusable scroll region for wide tables. -->
    <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
    <div class="markdown-table" role="region" aria-label="分析結果の表" tabindex="0">
      <table>
        <thead
          ><tr
            >{#each node.rows?.[0] ?? [] as cell, column (column)}<th scope="col"
                ><svelte:self nodes={cell} /></th
              >{/each}</tr
          ></thead
        >
        <tbody
          >{#each node.rows?.slice(1) ?? [] as row, rowIndex (rowIndex)}<tr
              >{#each row as cell, column (column)}<td><svelte:self nodes={cell} /></td>{/each}</tr
            >{/each}</tbody
        >
      </table>
    </div>
  {:else if node.type === 'link' && node.href}
    <!-- Absolute external URLs are validated by safeLink; they are not app routes. -->
    <!-- eslint-disable-next-line svelte/no-navigation-without-resolve -->
    <a href={node.href} target="_blank" rel="noopener noreferrer" referrerpolicy="no-referrer"
      ><svelte:self nodes={node.children ?? []} /></a
    >
  {:else if node.type === 'link'}
    <svelte:self nodes={node.children ?? []} />
  {:else if node.type === 'code'}
    <pre><code>{node.text}</code></pre>
  {:else if node.type === 'codespan'}
    <code>{node.text}</code>
  {:else if node.type === 'br'}<br />
  {:else if node.type === 'hr'}<hr />
  {:else}{node.text}{/if}
{/each}
