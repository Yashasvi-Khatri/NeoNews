<script>
  import { onMount } from 'svelte';
  import { page } from '$app/stores';
  import { apiDelete, apiGet, apiPost } from '$lib/api.js';

  let article = null;
  let summaryText = 'Generate a summary for this article.';
  let loading = true;
  let summaryLoading = false;
  let error = '';
  let notice = '';
  let related = [];

  $: articleId = $page.params.id;

  onMount(() => {
    setupTheme();
    loadArticle();
  });

  async function loadArticle() {
    loading = true;
    error = '';
    try {
      article = await apiGet(`/api/articles/${articleId}`);
      summaryText = article.summary || 'Generate a summary for this article.';
      const relatedPayload = await apiGet(`/api/articles/${articleId}/related?limit=6`).catch(() => ({ articles: [] }));
      related = relatedPayload.articles || [];
      await apiPost('/api/events', { article_id: Number(articleId), event_type: 'view' }).catch(() => {});
    } catch (err) {
      error = err.message;
    } finally {
      loading = false;
    }
  }

  async function summarize() {
    summaryLoading = true;
    error = '';
    try {
      article = await apiPost(`/api/articles/${articleId}/summary`);
      summaryText = article.summary || 'No summary returned.';
    } catch (err) {
      error = err.message;
    } finally {
      summaryLoading = false;
    }
  }

  async function toggleBookmark() {
    try {
      if (article.is_bookmarked) {
        await apiDelete(`/api/bookmarks/${article.id}`);
        article.is_bookmarked = false;
      } else {
        await apiPost(`/api/bookmarks/${article.id}`);
        article.is_bookmarked = true;
      }
    } catch (err) {
      error = err.message;
    }
  }

  async function shareArticle() {
    try {
      const payload = await apiPost(`/api/articles/${article.id}/share`, { platform: null });
      if (navigator.share) {
        await navigator.share({ title: payload.message, text: payload.message, url: payload.share_url });
      } else {
        await navigator.clipboard.writeText(payload.share_url);
        notice = 'Share link copied.';
      }
    } catch (err) {
      if (err.name !== 'AbortError') {
        error = err.message;
      }
    }
  }

  function setupTheme() {
    const stored = localStorage.getItem('news-theme');
    const dark = stored ? stored === 'dark' : window.matchMedia?.('(prefers-color-scheme: dark)').matches;
    document.documentElement.dataset.theme = dark ? 'dark' : 'light';
  }

  function imageFor(item) {
    return item?.thumbnail_path || fallbackImageFor(item?.primary_category || 'General');
  }

  function handleImageError(event, item) {
    const fallback = fallbackImageFor(item?.primary_category || 'General');
    if (!event.currentTarget.src.endsWith(fallback)) {
      event.currentTarget.src = fallback;
    }
  }

  function fallbackImageFor(category) {
    const value = (category || 'General').toLowerCase();
    if (value.includes('business') || value.includes('market') || value.includes('econom')) return '/static/fallbacks/business.svg';
    if (value.includes('tech') || value.includes('science')) return '/static/fallbacks/technology.svg';
    if (value.includes('health')) return '/static/fallbacks/health.svg';
    if (value.includes('sport')) return '/static/fallbacks/sports.svg';
    if (value.includes('entertainment') || value.includes('culture')) return '/static/fallbacks/culture.svg';
    if (value.includes('policy') || value.includes('notice') || value.includes('bill') || value.includes('budget') || value.includes('rule') || value.includes('regulation')) return '/static/fallbacks/policy.svg';
    return '/static/fallbacks/news.svg';
  }

  function formatDateTime(value) {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return '';
    const months = [
      'january',
      'february',
      'march',
      'april',
      'may',
      'june',
      'july',
      'august',
      'september',
      'october',
      'november',
      'december'
    ];
    const hours = date.getHours();
    const hour12 = hours % 12 || 12;
    const minutes = String(date.getMinutes()).padStart(2, '0');
    const meridiem = hours >= 12 ? 'pm' : 'am';
    return `${date.getDate()} ${months[date.getMonth()]} ${date.getFullYear()}, ${hour12}:${minutes} ${meridiem}`;
  }
</script>

<svelte:head>
  <title>{article?.headline || 'Article'} | Daily World News</title>
</svelte:head>

<main class="shell">
  <a class="back" href="/">Back to headlines</a>

  {#if loading}
    <section class="panel">Loading...</section>
  {:else if error}
    <section class="notice error">{error}</section>
  {:else if article}
    <section class="article-detail">
      <p class="kicker">
        <span>{article.primary_category}</span>
        <span>{article.reading_time_minutes} min read</span>
        <span>{article.source_name}</span>
        <time datetime={article.published_at}>{formatDateTime(article.published_at)}</time>
      </p>
      <h1>{article.headline}</h1>

      <img class="hero" src={imageFor(article)} alt="" on:error={(event) => handleImageError(event, article)} />

      {#if article.description}
        <p class="lead">{article.description}</p>
      {/if}

      <div class="actions">
        <button class:active={article.is_bookmarked} on:click={toggleBookmark}>
          {article.is_bookmarked ? 'Saved' : 'Read Later'}
        </button>
        <button on:click={summarize} disabled={summaryLoading}>{summaryLoading ? 'Summarizing...' : 'Summary'}</button>
        <button on:click={shareArticle}>Share</button>
        <a href={article.article_url} target="_blank" rel="noreferrer">Source</a>
      </div>

      {#if notice}<p class="notice">{notice}</p>{/if}

      <section class="panel">
        <h2>Summary</h2>
        <p>{summaryText}</p>
      </section>

      {#if related.length}
        <section class="panel related">
          <h2>Related Articles</h2>
          <div class="related-list">
            {#each related as item}
              <a href={item.article_path}>
                <img src={imageFor(item)} alt="" loading="lazy" on:error={(event) => handleImageError(event, item)} />
                <strong>{item.headline}</strong>
                <small>{item.source_name} · {item.primary_category}</small>
              </a>
            {/each}
          </div>
        </section>
      {/if}

      <p class="private-note">Full extracted article text is kept privately in the database for summary generation. Open the source for the publisher article.</p>
    </section>
  {/if}
</main>

<style>
  :global(:root) {
    color-scheme: light;
    --bg: #f4f1ea;
    --panel: #fffdf8;
    --text: #151515;
    --muted: #64645f;
    --border: #ded8cc;
    --soft: #eee9df;
    --accent: #0b6b60;
    --accent-strong: #063f38;
    --accent-soft: #e3f1ed;
    --link: #0b6b60;
    --shadow: 0 18px 50px rgba(42, 33, 18, 0.08);
  }
  :global(body) {
    margin: 0;
    background:
      linear-gradient(180deg, rgba(255, 253, 248, 0.94), rgba(244, 241, 234, 0.98) 420px),
      repeating-linear-gradient(90deg, rgba(21, 21, 21, 0.035) 0 1px, transparent 1px 88px);
    color: var(--text);
    font-family: Georgia, "Times New Roman", serif;
  }
  :global(*) { box-sizing: border-box; }
  a { color: var(--link); text-decoration-thickness: 1px; text-underline-offset: 3px; }
  button, .actions a {
    min-height: 38px;
    border: 1px solid var(--border);
    border-radius: 6px;
    background: var(--panel);
    color: var(--text);
    padding: 8px 12px;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    font-family: ui-sans-serif, system-ui, sans-serif;
    font-size: 14px;
    font-weight: 750;
    text-decoration: none;
  }
  button:hover, .actions a:hover { border-color: var(--accent); color: var(--accent-strong); text-decoration: none; }
  button.active { background: var(--accent-soft); border-color: var(--accent); color: var(--accent-strong); }
  button:disabled { opacity: 0.7; cursor: progress; }
  .shell { width: min(980px, calc(100vw - 32px)); margin: 0 auto; padding: 24px 0 56px; }
  .back {
    display: inline-flex;
    margin-bottom: 18px;
    color: var(--muted);
    font-family: ui-sans-serif, system-ui, sans-serif;
    font-size: 14px;
    text-decoration: none;
  }
  .back:hover { color: var(--accent-strong); }
  h1, h2, p { margin: 0; }
  h1 { max-width: 860px; font-size: clamp(34px, 5vw, 58px); line-height: 1.02; letter-spacing: 0; }
  .kicker {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-bottom: 14px;
    color: var(--muted);
    font-family: ui-sans-serif, system-ui, sans-serif;
    font-size: 13px;
  }
  .kicker span, .kicker time { background: var(--soft); border-radius: 4px; padding: 4px 8px; }
  .hero {
    width: 100%;
    max-height: 500px;
    object-fit: cover;
    margin-top: 22px;
    border-radius: 8px;
    background: var(--soft);
    border: 1px solid var(--border);
    box-shadow: var(--shadow);
  }
  .lead { max-width: 760px; margin-top: 18px; color: var(--text); font-size: 20px; line-height: 1.55; }
  .actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 18px; }
  .panel { margin-top: 20px; background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 18px; line-height: 1.65; box-shadow: var(--shadow); }
  .panel h2 { font-size: 20px; margin-bottom: 10px; }
  .private-note { margin-top: 14px; color: var(--muted); font-family: ui-sans-serif, system-ui, sans-serif; line-height: 1.45; }
  .notice { margin-top: 12px; padding: 11px 12px; border: 1px solid #fed7aa; border-radius: 8px; background: #fff7ed; color: #8a4b12; }
  .notice.error { background: #fff1f0; color: #b42318; border-color: #f5c2bd; }
  .related-list { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; margin-top: 12px; }
  .related-list a { display: grid; gap: 8px; color: var(--text); border: 1px solid var(--border); border-radius: 8px; padding: 10px; text-decoration: none; background: #fff; }
  .related-list a:hover { border-color: var(--accent); }
  .related-list img { width: 100%; aspect-ratio: 16 / 9; border-radius: 6px; background: var(--soft); object-fit: cover; }
  .related-list strong { line-height: 1.25; }
  .related-list small { color: var(--muted); font-family: ui-sans-serif, system-ui, sans-serif; }
  @media (max-width: 640px) {
    .shell { width: min(100vw - 20px, 980px); padding: 14px 0 40px; }
    .actions { display: grid; grid-template-columns: 1fr; }
    .related-list { grid-template-columns: 1fr; }
  }
</style>
