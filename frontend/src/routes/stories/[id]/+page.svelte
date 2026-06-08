<script>
  import { onMount } from 'svelte';
  import { page } from '$app/stores';
  import { apiGet, apiPost } from '$lib/api.js';

  let story = null;
  let summaryText = 'Generate a combined summary for this multi-source story.';
  let loading = true;
  let summaryLoading = false;
  let error = '';

  $: storyId = $page.params.id;
  $: sourceArticles = story?.articles || [];

  onMount(() => {
    setupTheme();
    loadStory();
  });

  async function loadStory() {
    loading = true;
    error = '';
    try {
      story = await apiGet(`/api/story-clusters/${storyId}`);
      summaryText = story.summary || 'Generate a combined summary for this multi-source story.';
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
      story = await apiPost(`/api/story-clusters/${storyId}/summary`);
      summaryText = story.summary || 'No combined summary returned.';
    } catch (err) {
      error = err.message;
    } finally {
      summaryLoading = false;
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
  <title>{story?.title || 'Story'} | Daily World News</title>
</svelte:head>

<main class="shell">
  <a class="back" href="/">Back to headlines</a>

  {#if loading}
    <section class="panel">Loading...</section>
  {:else if error}
    <section class="notice error">{error}</section>
  {:else if story}
    <section class="story-detail">
      <p class="kicker">
        <span>{story.primary_category}</span>
        <span>{story.source_count} sources</span>
        <span>{story.article_count} articles</span>
        <time datetime={story.latest_published_at}>{formatDateTime(story.latest_published_at)}</time>
      </p>
      <h1>{story.title}</h1>

      <img class="hero" src={imageFor(story)} alt="" on:error={(event) => handleImageError(event, story)} />

      <div class="actions">
        <button on:click={summarize} disabled={summaryLoading}>
          {summaryLoading ? 'Summarizing...' : story.summary ? 'Refresh Combined Summary' : 'Combined Summary'}
        </button>
      </div>

      <section class="panel summary-panel">
        <div class="panel-heading">
          <h2>Combined Summary</h2>
          <span>{story.source_count} source view</span>
        </div>
        <p>{summaryText}</p>
      </section>

      <section class="panel">
        <div class="panel-heading">
          <h2>Source Articles</h2>
          <span>{sourceArticles.length} options</span>
        </div>
        <div class="source-grid">
          {#each sourceArticles as sourceArticle}
            <article class="source-card">
              <img src={imageFor(sourceArticle)} alt="" loading="lazy" on:error={(event) => handleImageError(event, sourceArticle)} />
              <div>
                <p class="source-meta">
                  <span>{sourceArticle.source_name}</span>
                  <time datetime={sourceArticle.published_at}>{formatDateTime(sourceArticle.published_at)}</time>
                </p>
                <h3>{sourceArticle.headline}</h3>
                {#if sourceArticle.description}
                  <p class="description">{sourceArticle.description}</p>
                {/if}
              </div>
              <div class="source-actions">
                <a href={sourceArticle.article_path}>Open Card</a>
                <a href={sourceArticle.article_url} target="_blank" rel="noreferrer">Publisher Source</a>
              </div>
            </article>
          {/each}
        </div>
      </section>
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
  button, .source-actions a {
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
  button:hover, .source-actions a:hover { border-color: var(--accent); color: var(--accent-strong); text-decoration: none; }
  button:disabled { opacity: 0.7; cursor: progress; }
  .shell { width: min(1040px, calc(100vw - 32px)); margin: 0 auto; padding: 24px 0 56px; }
  .back {
    display: inline-flex;
    margin-bottom: 18px;
    color: var(--muted);
    font-family: ui-sans-serif, system-ui, sans-serif;
    font-size: 14px;
    text-decoration: none;
  }
  .back:hover { color: var(--accent-strong); }
  h1, h2, h3, p { margin: 0; }
  h1 { max-width: 900px; font-size: clamp(34px, 5vw, 58px); line-height: 1.02; letter-spacing: 0; }
  .kicker, .source-meta {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-bottom: 10px;
    color: var(--muted);
    font-family: ui-sans-serif, system-ui, sans-serif;
    font-size: 13px;
  }
  .kicker span, .kicker time, .source-meta span, .source-meta time {
    background: var(--soft);
    border-radius: 4px;
    padding: 4px 8px;
  }
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
  .actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 18px; }
  .panel { margin-top: 20px; background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 18px; line-height: 1.65; box-shadow: var(--shadow); }
  .panel-heading { display: flex; justify-content: space-between; gap: 12px; align-items: center; margin-bottom: 12px; }
  .panel-heading h2 { font-size: 20px; }
  .panel-heading span { color: var(--muted); font-family: ui-sans-serif, system-ui, sans-serif; font-size: 13px; }
  .summary-panel { background: linear-gradient(135deg, rgba(11, 107, 96, 0.1), transparent 52%), var(--panel); }
  .source-grid { display: grid; gap: 12px; }
  .source-card {
    display: grid;
    grid-template-columns: 170px minmax(0, 1fr) auto;
    gap: 14px;
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 12px;
    background: #fff;
  }
  .source-card img { width: 170px; aspect-ratio: 16 / 10; object-fit: cover; border-radius: 6px; background: var(--panel); }
  .source-card h3 { font-size: 18px; line-height: 1.25; }
  .description { margin-top: 8px; color: var(--muted); font-family: ui-sans-serif, system-ui, sans-serif; line-height: 1.45; }
  .source-actions { display: flex; flex-wrap: wrap; gap: 8px; justify-content: flex-end; align-content: start; max-width: 190px; }
  .notice { margin-top: 12px; padding: 11px 12px; border: 1px solid #fed7aa; border-radius: 8px; background: #fff7ed; color: #8a4b12; }
  .notice.error { background: #fff1f0; color: #b42318; border-color: #f5c2bd; }
  @media (max-width: 760px) {
    .shell { width: min(100vw - 20px, 1040px); padding: 14px 0 40px; }
    .source-card { grid-template-columns: 1fr; }
    .source-card img { width: 100%; max-height: 240px; }
    .source-actions { justify-content: flex-start; max-width: none; }
    .source-actions a { flex: 1 1 150px; }
  }
</style>
