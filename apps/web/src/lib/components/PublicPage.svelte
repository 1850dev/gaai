<script lang="ts">
  import { resolve } from '$app/paths';

  export let title: string;
  export let label: string;
</script>

<svelte:head><title>{title} | Analytics AI</title></svelte:head>

<main class="public-page">
  <nav class="public-nav" aria-label="公開ページ">
    <a href={resolve('/about')} class="brand">
      <span class="brand-mark small" aria-hidden="true">A</span> Analytics AI
    </a>
    <a href={resolve('/')}>分析画面へ</a>
  </nav>
  <article>
    <p class="eyebrow">{label}</p>
    <h1>{title}</h1>
    <slot />
  </article>
  <nav class="public-footer" aria-label="サービス情報">
    <a href={resolve('/about')}>サービス紹介</a>
    <a href={resolve('/privacy')}>プライバシーポリシー</a>
    <a href={resolve('/terms')}>利用規約</a>
  </nav>
</main>

<style>
  .public-page {
    max-width: 58rem;
    margin: auto;
    padding: 1.5rem clamp(1rem, 4vw, 2rem) 3rem;
  }
  .public-nav,
  .public-footer {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 1rem;
  }
  .public-nav {
    justify-content: space-between;
  }
  .brand {
    text-decoration: none;
  }
  article {
    margin: 2rem 0 1.5rem;
    padding: clamp(1.125rem, 4vw, 2.5rem);
    border: 1px solid var(--border);
    border-radius: 1.25rem;
    background: var(--panel);
    line-height: 1.85;
    overflow-wrap: anywhere;
  }
  h1 {
    font-size: clamp(1.6rem, 5vw, 2.7rem);
    line-height: 1.25;
    margin: 0 0 1.25rem;
  }
  article :global(h2) {
    font-size: 1.15rem;
    margin-top: 1.75rem;
  }
  article :global(a) {
    color: var(--accent);
  }
  .public-footer {
    font-size: 0.875rem;
  }
</style>
