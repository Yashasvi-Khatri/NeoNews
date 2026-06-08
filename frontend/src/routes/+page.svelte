<script>
  import { onMount } from 'svelte';
  import { apiDelete, apiGet, apiPost, apiPut, articleQuery } from '$lib/api.js';

  const pageSize = 25;
  const trendingLimit = 10;
  const viewedLimit = 20;
  const viewedPageSize = 5;
  const skeletons = Array.from({ length: 6 });

  let articles = [];
  let clusters = [];
  let notices = [];
  let trending = [];
  let viewed = [];
  let categories = [];
  let sources = [];
  let sourceConfigs = [];
  let sourceHealth = [];
  let regions = [];
  let noticeSources = [];
  let noticeTypes = [];
  let noticeStats = { total: 0, sources: 0 };
  let notice = '';
  let error = '';
  let loading = true;
  let loadingMore = false;
  let noticeIngesting = false;
  let hasMore = false;
  let clusterHasMore = false;
  let noticeHasMore = false;
  let offset = 0;
  let clusterOffset = 0;
  let noticeOffset = 0;
  let ingestStatus = { status: 'idle', remaining_cards: 0, processed_cards: 0 };
  let pollTimer = null;
  let newBoundaryAt = '';
  let currentNewsWindowStart = '';
  let activeSection = 'news';
  let activePanel = '';
  let editingSource = '';
  let offlineLoaded = false;
  let storyMode = true;
  let summarizingClusterId = null;
  let viewedStart = 0;

  let sourceForm = emptySource();

  let filters = {
    view: 'latest',
    region: '',
    source: '',
    category: '',
    q: '',
    sort: 'date'
  };

  let noticeFilters = {
    source: '',
    document_type: ''
  };

  $: trendingCards = trending.slice(0, trendingLimit);
  $: viewedMaxStart = Math.max(0, viewed.length - viewedPageSize);
  $: visibleViewed = viewed.slice(viewedStart, viewedStart + viewedPageSize);

  onMount(() => {
    loadInitial();
    checkIngestionStatus();
    if (!navigator.onLine) {
      loadOfflineReadLater();
    }
    window.addEventListener('online', handleOnline);
    window.addEventListener('offline', handleOffline);
    return () => {
      stopPolling();
      window.removeEventListener('online', handleOnline);
      window.removeEventListener('offline', handleOffline);
    };
  });

  async function loadInitial() {
    if (activeSection === 'notices') {
      await loadNoticeSection(true);
      return;
    }
    loading = true;
    error = '';
    offlineLoaded = false;
    try {
      const [categoryPayload, facetPayload, trendingPayload, viewedPayload, sourcePayload, healthPayload] =
        await Promise.all([
          apiGet('/api/categories'),
          apiGet('/api/facets'),
          apiGet(`/api/articles/trending?limit=${trendingLimit}`),
          apiGet(`/api/articles/viewed?limit=${viewedLimit}`),
          apiGet('/api/sources'),
          apiGet('/api/feed-health')
        ]);
      categories = categoryPayload.categories || [];
      sources = facetPayload.sources || [];
      regions = facetPayload.regions || [];
      trending = (trendingPayload.articles || []).slice(0, trendingLimit);
      viewed = (viewedPayload.articles || []).slice(0, viewedLimit);
      viewedStart = 0;
      sourceConfigs = sourcePayload.sources || [];
      sourceHealth = healthPayload.sources || [];
      await Promise.all([loadArticles(true), loadStoryClusters(true)]);
    } catch (err) {
      error = err.message;
      if (!navigator.onLine) {
        await loadOfflineReadLater();
      }
    } finally {
      loading = false;
    }
  }

  async function loadArticles(reset = false) {
    const nextOffset = reset ? 0 : offset;
    const payload = await apiGet(
      articleQuery({
        ...filters,
        limit: pageSize,
        offset: nextOffset,
        exclude_clustered: filters.view !== 'read_later'
      })
    );
    const nextArticles = payload.articles || [];
    articles = reset ? nextArticles : [...articles, ...nextArticles];
    offset = nextOffset + nextArticles.length;
    hasMore = Boolean(payload.has_more);
    currentNewsWindowStart = payload.window_start || currentNewsWindowStart;
    validateNewBoundary();
    if (filters.view === 'read_later') {
      await storeOfflineReadLater(articles);
    }
  }

  async function loadStoryClusters(reset = false) {
    if (filters.view === 'read_later') {
      clusters = [];
      clusterHasMore = false;
      return;
    }
    const nextOffset = reset ? 0 : clusterOffset;
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries({
      region: filters.region,
      source: filters.source,
      category: filters.category,
      q: filters.q,
      limit: pageSize,
      offset: nextOffset
    })) {
      if (value !== null && value !== undefined && value !== '') query.set(key, value);
    }
    const payload = await apiGet(`/api/story-clusters?${query.toString()}`);
    const nextClusters = payload.clusters || [];
    clusters = reset ? nextClusters : [...clusters, ...nextClusters];
    clusterOffset = nextOffset + nextClusters.length;
    clusterHasMore = Boolean(payload.has_more);
    currentNewsWindowStart = payload.window_start || currentNewsWindowStart;
    validateNewBoundary();
  }

  async function loadMore() {
    loadingMore = true;
    error = '';
    try {
      if (activeSection === 'notices') {
        await loadMoreNotices();
      } else if (storyMode && filters.view !== 'read_later') {
        await loadStoryClusters(false);
      } else {
        await loadArticles(false);
      }
    } catch (err) {
      error = err.message;
    } finally {
      loadingMore = false;
    }
  }

  async function applyFilters() {
    await loadInitial();
  }

  async function clearFilters() {
    filters = { ...filters, region: '', source: '', category: '', q: '', sort: 'date' };
    noticeFilters = { source: '', document_type: '' };
    if (activeSection === 'notices') {
      await loadNoticeSection(true);
    } else {
      await loadInitial();
    }
  }

  async function handleSearchKey(event) {
    if (event.key === 'Enter') {
      await applyFilters();
    }
  }

  async function setView(view) {
    activeSection = 'news';
    filters.view = view;
    await loadInitial();
  }

  async function setNoticeSection() {
    activeSection = 'notices';
    await loadNoticeSection(true);
  }

  async function setStoryMode(value) {
    storyMode = value;
    if (storyMode && !clusters.length && filters.view !== 'read_later') {
      await loadStoryClusters(true);
    }
  }

  async function setCategory(category) {
    filters.category = category;
    await loadInitial();
  }

  async function toggleBookmark(article) {
    try {
      if (article.is_bookmarked) {
        await apiDelete(`/api/bookmarks/${article.id}`);
        article.is_bookmarked = false;
        if (filters.view === 'read_later') {
          articles = articles.filter((item) => item.id !== article.id);
        } else {
          articles = [...articles];
        }
      } else {
        await apiPost(`/api/bookmarks/${article.id}`);
        article.is_bookmarked = true;
        articles = [...articles];
      }
      await refreshOfflineReadLater();
    } catch (err) {
      error = err.message;
    }
  }

  async function shareArticle(article) {
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

  async function summarizeCluster(cluster) {
    summarizingClusterId = cluster.id;
    error = '';
    try {
      const payload = await apiPost(`/api/story-clusters/${cluster.id}/summary`);
      clusters = clusters.map((item) => (item.id === cluster.id ? payload : item));
    } catch (err) {
      error = err.message;
    } finally {
      summarizingClusterId = null;
    }
  }

  async function ingestRecent() {
    await startIngestion('/api/ingest/recent', null, 'Full recent-news ingestion started. Load More will only paginate saved cards.');
  }

  async function startIngestion(path, body, message) {
    notice = '';
    error = '';
    try {
      const payload = await apiPost(path, body);
      ingestStatus = payload.status === 'accepted' ? { ...payload, status: 'running' } : payload;
      if (payload.status === 'accepted') {
        newBoundaryAt = '';
      }
      notice = payload.status === 'already_running' ? 'Ingestion is already running.' : message;
      startPolling();
    } catch (err) {
      error = err.message;
    }
  }

  async function checkIngestionStatus() {
    try {
      const payload = await apiGet('/api/ingest/status');
      ingestStatus = payload;
      if (payload.status === 'running') {
        startPolling();
      } else if (payload.status === 'completed') {
        applyNewBoundaryFromStatus(payload);
      }
    } catch {
      // Status polling should not block normal reading.
    }
  }

  function startPolling() {
    stopPolling();
    pollTimer = window.setInterval(async () => {
      const payload = await apiGet('/api/ingest/status').catch((err) => {
        error = err.message;
        return null;
      });
      if (!payload) return;
      const previous = ingestStatus.status;
      ingestStatus = payload;
      if (payload.status === 'completed') {
        stopPolling();
        applyNewBoundaryFromStatus(payload);
        notice = `Ingestion completed: ${payload.stats?.inserted || 0} inserted, ${payload.stats?.updated || 0} updated, ${payload.stats?.important_cards_selected || 0} important story cards processed.`;
        await loadInitial();
      } else if (payload.status === 'failed') {
        stopPolling();
        error = payload.error || 'Ingestion failed.';
      } else if (previous !== 'running') {
        notice = 'Ingestion running.';
      }
    }, 3000);
  }

  function stopPolling() {
    if (pollTimer) {
      window.clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  function applyNewBoundaryFromStatus(payload) {
    const boundary = payload?.stats?.previous_ingested_until || payload?.previous_ingested_until || '';
    if (!boundary) return;
    newBoundaryAt = boundary;
    validateNewBoundary();
  }

  function validateNewBoundary() {
    if (!newBoundaryAt || !currentNewsWindowStart) return;
    const boundary = Date.parse(newBoundaryAt);
    const windowStart = Date.parse(currentNewsWindowStart);
    if (Number.isNaN(boundary) || Number.isNaN(windowStart) || boundary < windowStart) {
      newBoundaryAt = '';
    }
  }

  function shouldShowNewBoundary(items, index) {
    if (!newBoundaryAt || activeSection !== 'news' || filters.view === 'read_later') return false;
    const boundary = Date.parse(newBoundaryAt);
    if (Number.isNaN(boundary)) return false;
    const current = Date.parse(cardPublishedAt(items[index]));
    if (Number.isNaN(current) || current > boundary) return false;
    if (index === 0) return true;
    const previous = Date.parse(cardPublishedAt(items[index - 1]));
    return Number.isNaN(previous) || previous > boundary;
  }

  function cardPublishedAt(item) {
    return item?.latest_published_at || item?.published_at || '';
  }

  function togglePanel(name) {
    activePanel = activePanel === name ? '' : name;
  }

  function emptySource() {
    return { name: '', country: '', region: '', language: 'en', type: 'publisher', homepage: '', feed_url: '', enabled: true };
  }

  function editSource(source) {
    editingSource = source.name;
    sourceForm = { ...source };
    activePanel = 'sources';
  }

  function cancelSourceEdit() {
    editingSource = '';
    sourceForm = emptySource();
  }

  async function saveSource() {
    try {
      if (editingSource) {
        await apiPut(`/api/sources/${encodeURIComponent(editingSource)}`, sourceForm);
        notice = 'Source updated.';
      } else {
        await apiPost('/api/sources', sourceForm);
        notice = 'Source added.';
      }
      cancelSourceEdit();
      await loadSourceAdminData();
    } catch (err) {
      error = err.message;
    }
  }

  async function disableSource(source) {
    try {
      await apiDelete(`/api/sources/${encodeURIComponent(source.name)}`);
      notice = 'Source disabled.';
      await loadSourceAdminData();
    } catch (err) {
      error = err.message;
    }
  }

  async function testSource(source) {
    try {
      const payload = await apiPost(`/api/sources/${encodeURIComponent(source.name)}/test`);
      notice = payload.success ? `Feed OK: ${payload.items_seen} items found.` : payload.failure_reason;
      await loadSourceAdminData();
    } catch (err) {
      error = err.message;
    }
  }

  async function loadSourceAdminData() {
    const [sourcePayload, healthPayload, facetPayload] = await Promise.all([
      apiGet('/api/sources'),
      apiGet('/api/feed-health'),
      apiGet('/api/facets')
    ]);
    sourceConfigs = sourcePayload.sources || [];
    sourceHealth = healthPayload.sources || [];
    sources = facetPayload.sources || [];
    regions = facetPayload.regions || [];
  }

  function healthFor(sourceName) {
    return sourceHealth.find((item) => item.source_name === sourceName);
  }

  function emptyMessage() {
    if (offlineLoaded) return 'No cached read-later articles are available yet.';
    if (filters.view === 'read_later') return 'Bookmark articles to build this list.';
    if (storyMode && articles.length && !clusters.length) return 'No multi-source stories exist for the latest 24-hour news yet.';
    return 'Run latest 24-hour ingestion to collect news.';
  }

  function loadMoreLabel() {
    return loadingMore ? 'Loading...' : 'Load More';
  }

  function cardPath(item) {
    return item?.story_path || item?.article_path || item?.article_url || '#';
  }

  function moveViewed(delta) {
    viewedStart = Math.max(0, Math.min(viewedStart + delta, viewedMaxStart));
  }

  function formatDateTime(value) {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return '';
    const months = [
      'jan', 'feb', 'mar', 'apr', 'may', 'jun',
      'jul', 'aug', 'sep', 'oct', 'nov', 'dec'
    ];
    const hours = date.getHours();
    const hour12 = hours % 12 || 12;
    const minutes = String(date.getMinutes()).padStart(2, '0');
    const meridiem = hours >= 12 ? 'pm' : 'am';
    return `${date.getDate()} ${months[date.getMonth()]} ${date.getFullYear()}, ${hour12}:${minutes} ${meridiem}`;
  }

  function imageFor(item) {
    return item?.thumbnail_path || fallbackImageFor(item?.primary_category || item?.document_type || 'General');
  }

  function handleImageError(event, item) {
    const fallback = fallbackImageFor(item?.primary_category || item?.document_type || 'General');
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

  async function loadNoticeSection(reset = false) {
    loading = true;
    error = '';
    try {
      const [noticePayload, metaPayload] = await Promise.all([loadNotices(reset), apiGet('/api/notices/meta')]);
      noticeSources = metaPayload.sources || [];
      noticeTypes = metaPayload.document_types || [];
      noticeStats = metaPayload.stats || { total: 0, sources: 0 };
      return noticePayload;
    } catch (err) {
      error = err.message;
    } finally {
      loading = false;
    }
  }

  async function loadNotices(reset = false) {
    const nextOffset = reset ? 0 : noticeOffset;
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries({
      ...noticeFilters,
      limit: pageSize,
      offset: nextOffset
    })) {
      if (value !== null && value !== undefined && value !== '') query.set(key, value);
    }
    const payload = await apiGet(`/api/notices?${query.toString()}`);
    const nextNotices = payload.notices || [];
    notices = reset ? nextNotices : [...notices, ...nextNotices];
    noticeOffset = nextOffset + nextNotices.length;
    noticeHasMore = Boolean(payload.has_more);
    return payload;
  }

  async function loadMoreNotices() {
    loadingMore = true;
    error = '';
    try {
      await loadNotices(false);
    } catch (err) {
      error = err.message;
    } finally {
      loadingMore = false;
    }
  }

  async function ingestNotices() {
    noticeIngesting = true;
    notice = '';
    error = '';
    try {
      const stats = await apiPost('/api/notices/ingest');
      notice = `Notices updated: ${stats.inserted || 0} inserted, ${stats.updated || 0} updated.`;
      await loadNoticeSection(true);
    } catch (err) {
      error = err.message;
    } finally {
      noticeIngesting = false;
    }
  }

  function noticeEmptyMessage() {
    return 'Run Update Notices to collect government documents and notices.';
  }

  function handleOnline() {
    notice = 'Back online.';
    refreshOfflineReadLater();
  }

  function handleOffline() {
    notice = 'Offline mode active. Showing cached read-later articles when available.';
    loadOfflineReadLater();
  }

  function openOfflineDb() {
    return new Promise((resolve, reject) => {
      const request = indexedDB.open('news-read-later-v1', 1);
      request.onupgradeneeded = () => {
        request.result.createObjectStore('articles', { keyPath: 'id' });
      };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
  }

  async function storeOfflineReadLater(items) {
    const db = await openOfflineDb();
    const transaction = db.transaction('articles', 'readwrite');
    const store = transaction.objectStore('articles');
    store.clear();
    items.forEach((item) => store.put(item));
  }

  async function readOfflineReadLater() {
    const db = await openOfflineDb();
    return new Promise((resolve, reject) => {
      const request = db.transaction('articles', 'readonly').objectStore('articles').getAll();
      request.onsuccess = () => resolve(request.result || []);
      request.onerror = () => reject(request.error);
    });
  }

  async function refreshOfflineReadLater() {
    if (!navigator.onLine) return;
    try {
      const payload = await apiGet('/api/bookmarks?limit=100');
      await storeOfflineReadLater(payload.articles || []);
    } catch {
      // Offline read-later caching is best-effort.
    }
  }

  async function loadOfflineReadLater() {
    try {
      const cached = await readOfflineReadLater();
      if (cached.length) {
        filters.view = 'read_later';
        articles = cached;
        hasMore = false;
        offlineLoaded = true;
        loading = false;
      }
    } catch {
      // Ignore IndexedDB failures.
    }
  }
</script>

<svelte:head>
  <title>Local News Aggregator</title>
</svelte:head>

<div class="page-wrapper">
  <div class="page-header">
    <div>
      <p class="eyebrow">Local News Aggregator</p>
      <h1>Your Daily Brief</h1>
      <p class="header-subtitle">
        {#if activeSection === 'notices'}
          {noticeStats.total || notices.length} notices from {noticeStats.sources || noticeSources.length} sources
        {:else if storyMode && filters.view !== 'read_later'}
          {clusters.length} stories · {trendingCards.length} trending
        {:else}
          {articles.length} headlines
        {/if}
      </p>
    </div>
    <div class="header-actions">
      {#if activeSection === 'notices'}
        <button class="button" on:click={ingestNotices} disabled={noticeIngesting}>
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/><circle cx="5" cy="12" r="1"/>
          </svg>
          {noticeIngesting ? 'Updating...' : 'Update Notices'}
        </button>
      {:else}
        <button class="button" on:click={ingestRecent} disabled={ingestStatus.status === 'running'}>
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <polyline points="23 4 23 10 17 10"></polyline><path d="M20.49 15a9 9 0 1 1-2-8.83"></path>
          </svg>
          {ingestStatus.status === 'running' ? 'Ingesting...' : 'Refresh'}
        </button>
      {/if}
    </div>
  </div>

  <!-- Section tabs -->
  <div class="section-tabs">
    <button 
      class:active={activeSection === 'news' && filters.view === 'latest'} 
      on:click={() => setView('latest')}
    >
      <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
        <path d="M3 3h18a1 1 0 011 1v16a1 1 0 01-1 1H3a1 1 0 01-1-1V4a1 1 0 011-1zm1 2v14h16V5H4z"/>
      </svg>
      Latest
    </button>
    <button 
      class:active={activeSection === 'news' && filters.view === 'read_later'} 
      on:click={() => setView('read_later')}
    >
      <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
        <path d="M5 2h14a1 1 0 011 1v18a1 1 0 01-1.45.9L12 18.9l-6.55 2.99A1 1 0 015 21V3a1 1 0 011-1m1 2v13.36l5.55-2.51 5.45 2.51V4H6z"/>
      </svg>
      Saved
    </button>
    <button 
      class:active={activeSection === 'notices'} 
      on:click={setNoticeSection}
    >
      <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
        <path d="M7 3h10a2 2 0 012 2v14a2 2 0 01-2 2H7a2 2 0 01-2-2V5a2 2 0 012-2zm1 2v14h8V5H8z"/>
      </svg>
      Notices
    </button>
  </div>

  <!-- Filters -->
  <div class="filters-section">
    {#if activeSection === 'notices'}
      <label class="field">
        <span>Source</span>
        <select class="filter-input" bind:value={noticeFilters.source}>
          <option value="">All sources</option>
          {#each noticeSources as source}
            <option value={source}>{source}</option>
          {/each}
        </select>
      </label>
      <label class="field">
        <span>Type</span>
        <select class="filter-input" bind:value={noticeFilters.document_type}>
          <option value="">All types</option>
          {#each noticeTypes as type}
            <option value={type}>{type}</option>
          {/each}
        </select>
      </label>
      <button class="button" on:click={() => loadNoticeSection(true)}>Apply</button>
      <button class="button button-secondary" on:click={clearFilters}>Reset</button>
    {:else}
      <label class="field search-field">
        <span>Search</span>
        <input class="filter-input" bind:value={filters.q} placeholder="Search headlines, topics, sources" on:keydown={handleSearchKey} />
      </label>
      <label class="field">
        <span>Region</span>
        <select class="filter-input" bind:value={filters.region}>
          <option value="">All regions</option>
          {#each regions as region}
            <option value={region}>{region}</option>
          {/each}
        </select>
      </label>
      <label class="field">
        <span>Source</span>
        <select class="filter-input" bind:value={filters.source}>
          <option value="">All sources</option>
          {#each sources as source}
            <option value={source}>{source}</option>
          {/each}
        </select>
      </label>
      <label class="field compact-field">
        <span>Sort</span>
        <select class="filter-input" bind:value={filters.sort}>
          <option value="date">Newest</option>
          <option value="relevance">Relevant</option>
        </select>
      </label>
      <button class="button" on:click={applyFilters}>Apply</button>
      <button class="button button-secondary" on:click={clearFilters}>Reset</button>
    {/if}
  </div>

  <!-- Alerts/Notices -->
  {#if notice}
    <div class="alert alert-info">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor">
        <circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/>
      </svg>
      <span>{notice}</span>
      <button on:click={() => notice = ''}>×</button>
    </div>
  {/if}
  {#if error}
    <div class="alert alert-error">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor">
        <circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/>
      </svg>
      <span>{error}</span>
      <button on:click={() => error = ''}>×</button>
    </div>
  {/if}

  <!-- Content sections -->
  {#if activeSection === 'news'}
    <!-- Trending section -->
    {#if filters.view !== 'read_later' && trendingCards.length > 0}
      <section class="section trending-section">
        <div class="section-header">
          <h2>Trending Now</h2>
          <span class="section-count">{trendingCards.length}</span>
        </div>
        <div class="trending-grid">
          {#each trendingCards as item, index}
            <a href={cardPath(item)} class="trending-card">
              <div class="trend-rank">{String(index + 1).padStart(2, '0')}</div>
              <h3>{item.headline || item.title}</h3>
              <div class="trend-meta">
                {#if item.kind === 'story'}
                  <span class="badge">{item.source_count} sources</span>
                {:else}
                  <span class="badge">{item.primary_category}</span>
                {/if}
              </div>
            </a>
          {/each}
        </div>
      </section>
    {/if}

    <!-- Mode tabs (for latest news section) -->
    {#if filters.view !== 'read_later'}
      <div class="mode-tabs">
        <button 
          class:active={storyMode} 
          on:click={() => setStoryMode(true)}
        >
          Stories
        </button>
        <button 
          class:active={!storyMode} 
          on:click={() => setStoryMode(false)}
        >
          Articles
        </button>
      </div>
    {/if}

    <!-- Category chips -->
    <div class="category-chips">
      <button 
        class:active={!filters.category} 
        on:click={() => setCategory('')}
      >
        All Topics
      </button>
      {#each categories as category}
        <button 
          class:active={filters.category === category.name}
          on:click={() => setCategory(category.name)}
        >
          {category.name}
          {#if category.count}
            <span class="count">{category.count}</span>
          {/if}
        </button>
      {/each}
    </div>

    <!-- Utility panels -->
    <div class="utility-tabs">
      <button 
        class:active={activePanel === 'health'}
        on:click={() => togglePanel('health')}
      >
        Feed Status
      </button>
      <button 
        class:active={activePanel === 'sources'}
        on:click={() => togglePanel('sources')}
      >
        Manage Sources
      </button>
    </div>

    {#if activePanel === 'health'}
      <section class="panel health-panel">
        <h3>Feed Health</h3>
        <div class="health-grid">
          {#each sourceConfigs as source}
            {@const health = healthFor(source.name)}
            <div class="health-item">
              <div class="health-header">
                <strong>{source.name}</strong>
                <span class="health-status" class:status-ok={health?.last_status === 'success'} class:status-fail={health?.last_status === 'failed'}>
                  {health?.last_status || 'unknown'}
                </span>
              </div>
              <p class="health-meta">{health?.items_seen ?? 0} items · Last: {health?.last_checked_at ? formatDateTime(health.last_checked_at) : 'Never'}</p>
              {#if health?.failure_reason}
                <p class="health-error">{health.failure_reason}</p>
              {/if}
            </div>
          {/each}
        </div>
      </section>
    {/if}

    {#if activePanel === 'sources'}
      <section class="panel sources-panel">
        <h3>{editingSource ? 'Edit Source' : 'Add New Source'}</h3>
        <div class="source-form">
          <input class="form-input" bind:value={sourceForm.name} placeholder="Source Name" />
          <input class="form-input" bind:value={sourceForm.feed_url} placeholder="RSS Feed URL" />
          <input class="form-input" bind:value={sourceForm.country} placeholder="Country" />
          <input class="form-input" bind:value={sourceForm.region} placeholder="Region" />
          <div class="form-actions">
            <button class="button" on:click={saveSource}>
              {editingSource ? 'Update' : 'Add'} Source
            </button>
            {#if editingSource}
              <button class="button button-secondary" on:click={cancelSourceEdit}>Cancel</button>
            {/if}
          </div>
        </div>

        <div class="source-list">
          {#each sourceConfigs as source}
            <div class="source-item">
              <div>
                <strong>{source.name}</strong>
                <p>{source.region || 'Global'} · {source.enabled ? 'Active' : 'Inactive'}</p>
              </div>
              <div class="source-actions">
                <button class="button button-small" on:click={() => testSource(source)}>Test</button>
                <button class="button button-small" on:click={() => editSource(source)}>Edit</button>
                {#if source.enabled}
                  <button class="button button-small button-secondary" on:click={() => disableSource(source)}>Disable</button>
                {/if}
              </div>
            </div>
          {/each}
        </div>
      </section>
    {/if}
  {/if}

  <!-- Main content -->
  {#if loading}
    <section class="articles-section">
      {#each skeletons as _}
        <div class="article-card skeleton">
          <div class="article-image"></div>
          <div class="article-content">
            <div class="skeleton-line" style="width: 30%; height: 12px;"></div>
            <div class="skeleton-line" style="width: 85%; height: 18px; margin-top: 8px;"></div>
            <div class="skeleton-line" style="width: 60%; height: 12px; margin-top: 12px;"></div>
          </div>
        </div>
      {/each}
    </section>
  {:else if activeSection === 'notices' && notices.length === 0}
    <section class="empty-state">
      <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
        <path d="M7 3h10a2 2 0 012 2v14a2 2 0 01-2 2H7a2 2 0 01-2-2V5a2 2 0 012-2z"/>
      </svg>
      <h2>No Notices</h2>
      <p>{noticeEmptyMessage()}</p>
    </section>
  {:else if (storyMode && filters.view !== 'read_later' ? clusters.length === 0 : articles.length === 0)}
    <section class="empty-state">
      <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
        <path d="M3 3h18a1 1 0 011 1v16a1 1 0 01-1 1H3a1 1 0 01-1-1V4a1 1 0 011-1z"/>
      </svg>
      <h2>No Headlines</h2>
      <p>{emptyMessage()}</p>
    </section>
  {:else if activeSection === 'notices'}
    <section class="articles-section">
      {#each notices as item}
        <article class="article-card notice-card">
          <a href={item.document_url} class="article-image" target="_blank" rel="noreferrer">
            <img src={imageFor(item)} alt="" loading="lazy" on:error={(e) => handleImageError(e, item)} />
          </a>
          <div class="article-content">
            <div class="article-meta">
              <span class="badge">{item.document_type}</span>
              <span class="badge">{item.source_region}</span>
            </div>
            <h3><a href={item.document_url} target="_blank" rel="noreferrer">{item.title}</a></h3>
            <p class="article-source">
              <strong>{item.source_name}</strong>
              <time>{formatDateTime(item.published_at)}</time>
            </p>
            {#if item.description}
              <p class="article-description">{item.description}</p>
            {/if}
          </div>
          <div class="article-actions">
            <a href={item.document_url} class="button button-small button-secondary" target="_blank" rel="noreferrer">View</a>
          </div>
        </article>
      {/each}
    </section>
    {#if noticeHasMore}
      <div class="load-more-section">
        <button class="button" on:click={loadMoreNotices} disabled={loadingMore}>
          {loadingMore ? 'Loading...' : 'Load More'}
        </button>
      </div>
    {/if}
  {:else if storyMode && filters.view !== 'read_later'}
    <section class="articles-section">
      {#each clusters as cluster, index}
        {#if shouldShowNewBoundary(clusters, index)}
          <div class="ingestion-divider">
            <span>Previously ingested news</span>
          </div>
        {/if}
        <article class="article-card story-card">
          <a href={cluster.story_path} class="article-image">
            <img src={imageFor(cluster)} alt="" loading="lazy" on:error={(e) => handleImageError(e, cluster)} />
          </a>
          <div class="article-content">
            <div class="article-meta">
              <span class="badge">{cluster.primary_category}</span>
              <span class="badge">{cluster.source_count} sources</span>
            </div>
            <h3><a href={cluster.story_path}>{cluster.title}</a></h3>
            {#if cluster.summary}
              <p class="article-description">{cluster.summary}</p>
            {/if}
            <div class="source-tags">
              {#each cluster.articles.slice(0, 3) as article}
                <a href={article.article_url} class="source-tag" target="_blank" rel="noreferrer">
                  {article.source_name}
                </a>
              {/each}
            </div>
          </div>
          <div class="article-actions">
            <button class="button button-small" on:click={() => summarizeCluster(cluster)} disabled={summarizingClusterId === cluster.id}>
              {summarizingClusterId === cluster.id ? 'Summarizing...' : 'Summarize'}
            </button>
            <a href={cluster.story_path} class="button button-small button-secondary">Read</a>
          </div>
        </article>
      {/each}
    </section>
    {#if clusterHasMore}
      <div class="load-more-section">
        <button class="button" on:click={loadMore} disabled={loadingMore || ingestStatus.status === 'running'}>
          {loadMoreLabel()}
        </button>
      </div>
    {/if}
  {:else}
    <section class="articles-section">
      {#each articles as article, index}
        {#if shouldShowNewBoundary(articles, index)}
          <div class="ingestion-divider">
            <span>Previously ingested news</span>
          </div>
        {/if}
        <article class="article-card">
          <a href={article.article_path} class="article-image">
            <img src={imageFor(article)} alt="" loading="lazy" on:error={(e) => handleImageError(e, article)} />
          </a>
          <div class="article-content">
            <div class="article-meta">
              <span class="badge">{article.primary_category}</span>
              <span class="badge">{article.reading_time_minutes} min read</span>
            </div>
            <h3><a href={article.article_path}>{article.headline}</a></h3>
            <p class="article-source">
              <strong>{article.source_name}</strong>
              <span>{article.source_region}</span>
              <time>{formatDateTime(article.published_at)}</time>
            </p>
            {#if article.description}
              <p class="article-description">{article.description}</p>
            {/if}
          </div>
          <div class="article-actions">
            <button 
              class="button button-small" 
              class:active={article.is_bookmarked}
              on:click={() => toggleBookmark(article)}
            >
              {article.is_bookmarked ? 'Saved' : 'Save'}
            </button>
            <button class="button button-small button-secondary" on:click={() => shareArticle(article)}>
              Share
            </button>
            <a href={article.article_url} class="button button-small button-secondary" target="_blank" rel="noreferrer">
              Source
            </a>
          </div>
        </article>
      {/each}
    </section>
    {#if hasMore}
      <div class="load-more-section">
        <button class="button" on:click={loadMore} disabled={loadingMore || ingestStatus.status === 'running'}>
          {loadMoreLabel()}
        </button>
      </div>
    {/if}
  {/if}
</div>

<style>
  :global(:root) {
    color-scheme: light;
    --color-bg: #f4f1ea;
    --color-bg-secondary: #fffdf8;
    --color-surface: #ffffff;
    --color-text: #151515;
    --color-text-secondary: #60615f;
    --color-text-muted: #8a8b86;
    --color-border: #ded8cc;
    --color-border-light: #eee9df;
    --color-accent: #0b6b60;
    --color-accent-strong: #063f38;
    --color-accent-light: #e3f1ed;
    --color-warning: #b45f06;
    --color-danger: #b42318;
    --shadow-soft: 0 16px 50px rgba(42, 33, 18, 0.08);
    --spacing-xs: 4px;
    --spacing-sm: 8px;
    --spacing-md: 12px;
    --spacing-lg: 18px;
    --spacing-xl: 28px;
    --spacing-2xl: 42px;
    --spacing-3xl: 64px;
    --radius-sm: 4px;
    --radius-md: 6px;
    --radius-lg: 8px;
    --text-xs: 12px;
    --text-sm: 14px;
    --text-md: 16px;
    --text-lg: 18px;
    --text-xl: 22px;
    --text-2xl: 28px;
    --text-3xl: 42px;
    --line-height-tight: 1.12;
    --line-height-normal: 1.45;
    --transition-fast: 140ms ease;
    --transition-normal: 220ms ease;
  }

  :global(html) {
    background: var(--color-bg);
  }

  :global(body) {
    margin: 0;
    min-width: 320px;
    background:
      linear-gradient(180deg, rgba(255, 253, 248, 0.92), rgba(244, 241, 234, 0.96) 360px),
      repeating-linear-gradient(90deg, rgba(21, 21, 21, 0.035) 0 1px, transparent 1px 88px);
    color: var(--color-text);
    font-family: Georgia, "Times New Roman", serif;
  }

  :global(*) {
    box-sizing: border-box;
  }

  button,
  input,
  select {
    font: inherit;
  }

  .page-wrapper {
    width: min(1180px, calc(100vw - 32px));
    margin: 0 auto;
    padding: 28px 0 56px;
  }

  .page-header {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: var(--spacing-xl);
    align-items: end;
    padding: 26px 0 20px;
    border-bottom: 2px solid var(--color-text);
  }

  .eyebrow {
    margin: 0 0 8px;
    color: var(--color-accent);
    font-family: ui-sans-serif, system-ui, sans-serif;
    font-size: 12px;
    font-weight: 800;
    letter-spacing: 0;
    text-transform: uppercase;
  }

  .page-header h1 {
    margin: 0;
    font-size: clamp(34px, 6vw, 72px);
    line-height: 0.95;
    letter-spacing: 0;
  }

  .header-subtitle {
    margin: 12px 0 0;
    color: var(--color-text-secondary);
    font-family: ui-sans-serif, system-ui, sans-serif;
    font-size: var(--text-sm);
  }

  .header-actions {
    display: flex;
    gap: var(--spacing-sm);
    flex-wrap: wrap;
    justify-content: flex-end;
  }

  .button,
  .article-actions a {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-height: 38px;
    gap: 8px;
    padding: 9px 13px;
    background: var(--color-accent);
    color: white;
    border: 1px solid var(--color-accent);
    border-radius: var(--radius-md);
    font-family: ui-sans-serif, system-ui, sans-serif;
    font-size: var(--text-sm);
    font-weight: 750;
    text-decoration: none;
    cursor: pointer;
    transition: background var(--transition-fast), border-color var(--transition-fast), transform var(--transition-fast);
    white-space: nowrap;
  }

  .button:hover:not(:disabled),
  .article-actions a:hover {
    background: var(--color-accent-strong);
    border-color: var(--color-accent-strong);
    transform: translateY(-1px);
    text-decoration: none;
  }

  .button:disabled {
    opacity: 0.6;
    cursor: progress;
    transform: none;
  }

  .button-secondary {
    background: var(--color-bg-secondary);
    color: var(--color-text);
    border-color: var(--color-border);
  }

  .button-secondary:hover:not(:disabled) {
    background: var(--color-border-light);
    border-color: var(--color-text-muted);
    color: var(--color-text);
  }

  .button-small {
    min-height: 32px;
    padding: 6px 10px;
    font-size: var(--text-xs);
  }

  .button.active {
    background: var(--color-accent-light);
    color: var(--color-accent-strong);
    border-color: var(--color-accent);
  }

  .section-tabs,
  .mode-tabs,
  .utility-tabs,
  .category-chips {
    display: flex;
    gap: 8px;
    overflow-x: auto;
    padding: 12px 0;
    -webkit-overflow-scrolling: touch;
  }

  .section-tabs {
    position: sticky;
    top: 0;
    z-index: 5;
    margin: 0 0 12px;
    background: rgba(244, 241, 234, 0.94);
    border-bottom: 1px solid var(--color-border);
    backdrop-filter: blur(14px);
  }

  .section-tabs button,
  .mode-tabs button,
  .utility-tabs button,
  .category-chips button {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    min-height: 38px;
    padding: 8px 12px;
    background: var(--color-bg-secondary);
    color: var(--color-text-secondary);
    border: 1px solid var(--color-border);
    border-radius: var(--radius-md);
    font-family: ui-sans-serif, system-ui, sans-serif;
    font-size: var(--text-sm);
    font-weight: 700;
    cursor: pointer;
    white-space: nowrap;
    transition: color var(--transition-fast), background var(--transition-fast), border-color var(--transition-fast);
  }

  .section-tabs button.active,
  .mode-tabs button.active,
  .utility-tabs button.active,
  .category-chips button.active {
    background: var(--color-text);
    color: var(--color-bg-secondary);
    border-color: var(--color-text);
  }

  .section-tabs button:hover,
  .mode-tabs button:hover,
  .utility-tabs button:hover,
  .category-chips button:hover {
    border-color: var(--color-accent);
    color: var(--color-accent-strong);
  }

  .mode-tabs {
    padding-top: 0;
  }

  .filters-section {
    display: grid;
    grid-template-columns: minmax(240px, 1.4fr) repeat(3, minmax(150px, 0.7fr)) auto auto;
    gap: 10px;
    align-items: end;
    margin: 4px 0 22px;
    padding: 14px;
    background: rgba(255, 253, 248, 0.9);
    border: 1px solid var(--color-border);
    border-radius: var(--radius-lg);
    box-shadow: var(--shadow-soft);
  }

  .field {
    display: grid;
    gap: 6px;
    min-width: 0;
    font-family: ui-sans-serif, system-ui, sans-serif;
  }

  .field span {
    color: var(--color-text-secondary);
    font-size: 11px;
    font-weight: 800;
    letter-spacing: 0;
    text-transform: uppercase;
  }

  .search-field {
    min-width: 240px;
  }

  .compact-field {
    min-width: 130px;
  }

  .filter-input,
  .form-input {
    width: 100%;
    min-height: 40px;
    padding: 9px 11px;
    background: var(--color-surface);
    color: var(--color-text);
    border: 1px solid var(--color-border);
    border-radius: var(--radius-md);
    font-family: ui-sans-serif, system-ui, sans-serif;
    font-size: var(--text-sm);
  }

  .filter-input:focus,
  .form-input:focus {
    outline: 2px solid var(--color-accent-light);
    border-color: var(--color-accent);
  }

  .alert {
    display: grid;
    grid-template-columns: auto 1fr auto;
    gap: 10px;
    align-items: center;
    padding: 12px 14px;
    margin-bottom: 18px;
    background: #eef7f4;
    color: var(--color-accent-strong);
    border: 1px solid #badbd3;
    border-radius: var(--radius-lg);
    font-family: ui-sans-serif, system-ui, sans-serif;
    font-size: var(--text-sm);
  }

  .alert-error {
    background: #fff1f0;
    color: var(--color-danger);
    border-color: #f1b8b2;
  }

  .alert button {
    width: 28px;
    height: 28px;
    background: transparent;
    border: 0;
    color: inherit;
    cursor: pointer;
    font-size: 20px;
  }

  .section {
    margin: 8px 0 28px;
  }

  .section-header {
    display: flex;
    justify-content: space-between;
    align-items: end;
    gap: 14px;
    margin-bottom: 12px;
  }

  .section-header h2 {
    margin: 0;
    font-size: var(--text-xl);
    line-height: var(--line-height-tight);
  }

  .section-count {
    color: var(--color-text-secondary);
    font-family: ui-sans-serif, system-ui, sans-serif;
    font-size: var(--text-sm);
  }

  .trending-grid {
    display: grid;
    grid-template-columns: 1.25fr repeat(2, minmax(0, 1fr));
    gap: 12px;
  }

  .trending-card {
    display: grid;
    align-content: space-between;
    min-height: 154px;
    gap: 14px;
    padding: 16px;
    background: var(--color-surface);
    border: 1px solid var(--color-border);
    border-radius: var(--radius-lg);
    color: var(--color-text);
    text-decoration: none;
    box-shadow: 0 10px 28px rgba(42, 33, 18, 0.05);
    transition: border-color var(--transition-fast), transform var(--transition-fast), box-shadow var(--transition-fast);
  }

  .trending-card:first-child {
    grid-row: span 2;
    min-height: 320px;
    background:
      linear-gradient(135deg, rgba(11, 107, 96, 0.13), transparent 56%),
      var(--color-surface);
  }

  .trending-card:hover {
    border-color: var(--color-accent);
    box-shadow: 0 18px 38px rgba(42, 33, 18, 0.11);
    transform: translateY(-2px);
  }

  .trend-rank {
    color: var(--color-accent);
    font-family: ui-sans-serif, system-ui, sans-serif;
    font-size: 28px;
    font-weight: 900;
  }

  .trending-card h3 {
    margin: 0;
    font-size: var(--text-lg);
    line-height: 1.22;
  }

  .trending-card:first-child h3 {
    font-size: var(--text-2xl);
  }

  .trend-meta,
  .article-meta,
  .source-tags {
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
  }

  .badge,
  .source-tag {
    display: inline-flex;
    align-items: center;
    max-width: 100%;
    padding: 4px 8px;
    background: var(--color-accent-light);
    color: var(--color-accent-strong);
    border: 1px solid rgba(11, 107, 96, 0.16);
    border-radius: var(--radius-sm);
    font-family: ui-sans-serif, system-ui, sans-serif;
    font-size: var(--text-xs);
    font-weight: 800;
    line-height: 1.2;
    text-decoration: none;
  }

  .source-tag:hover {
    background: var(--color-accent);
    color: white;
  }

  .category-chips {
    margin-bottom: 6px;
    padding-bottom: 14px;
  }

  .category-chips .count {
    opacity: 0.72;
    font-size: 11px;
  }

  .utility-tabs {
    justify-content: flex-end;
    padding-top: 0;
    margin-bottom: 10px;
  }

  .utility-tabs button {
    min-height: 34px;
    font-size: var(--text-xs);
  }

  .panel {
    margin-bottom: 22px;
    padding: 16px;
    background: var(--color-bg-secondary);
    border: 1px solid var(--color-border);
    border-radius: var(--radius-lg);
    box-shadow: var(--shadow-soft);
  }

  .panel h3 {
    margin: 0 0 14px;
    font-size: var(--text-lg);
  }

  .health-grid,
  .source-list,
  .source-form,
  .articles-section {
    display: grid;
    gap: 12px;
  }

  .health-grid {
    grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  }

  .health-item,
  .source-item {
    padding: 12px;
    background: var(--color-surface);
    border: 1px solid var(--color-border-light);
    border-radius: var(--radius-md);
  }

  .health-header,
  .source-item {
    display: flex;
    justify-content: space-between;
    gap: 12px;
    align-items: flex-start;
  }

  .health-status {
    padding: 3px 8px;
    background: var(--color-border-light);
    border-radius: var(--radius-sm);
    font-family: ui-sans-serif, system-ui, sans-serif;
    font-size: 11px;
    font-weight: 800;
    text-transform: uppercase;
  }

  .health-status.status-ok {
    background: #dff3e8;
    color: #146c43;
  }

  .health-status.status-fail,
  .health-error {
    background: #fff1f0;
    color: var(--color-danger);
  }

  .health-meta,
  .source-item p,
  .article-source,
  .article-description {
    color: var(--color-text-secondary);
    font-family: ui-sans-serif, system-ui, sans-serif;
    font-size: var(--text-sm);
  }

  .health-meta,
  .health-error,
  .source-item p {
    margin: 8px 0 0;
  }

  .health-error {
    padding: 8px;
    border-radius: var(--radius-sm);
  }

  .source-form {
    grid-template-columns: repeat(2, minmax(0, 1fr));
    margin-bottom: 16px;
  }

  .form-actions,
  .source-actions,
  .article-actions {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
  }

  .form-actions {
    grid-column: 1 / -1;
  }

  .source-actions,
  .article-actions {
    justify-content: flex-end;
    align-content: start;
  }

  .articles-section {
    margin-top: 16px;
  }

  .ingestion-divider {
    display: flex;
    align-items: center;
    gap: 12px;
    margin: 4px 0;
    color: var(--color-text-muted);
    font-family: ui-sans-serif, system-ui, sans-serif;
    font-size: var(--text-xs);
    font-weight: 850;
    letter-spacing: 0;
    text-transform: uppercase;
  }

  .ingestion-divider::before,
  .ingestion-divider::after {
    content: "";
    height: 1px;
    min-width: 24px;
    flex: 1;
    background: var(--color-border);
  }

  .article-card {
    display: grid;
    grid-template-columns: 190px minmax(0, 1fr) minmax(106px, auto);
    gap: 16px;
    padding: 14px;
    background: var(--color-surface);
    border: 1px solid var(--color-border);
    border-radius: var(--radius-lg);
    box-shadow: 0 10px 26px rgba(42, 33, 18, 0.045);
    transition: border-color var(--transition-fast), transform var(--transition-fast), box-shadow var(--transition-fast);
  }

  .article-card:hover:not(.skeleton) {
    border-color: var(--color-accent);
    box-shadow: 0 18px 38px rgba(42, 33, 18, 0.09);
    transform: translateY(-1px);
  }

  .story-card {
    grid-template-columns: 230px minmax(0, 1fr) minmax(108px, auto);
  }

  .article-image {
    display: block;
    width: 100%;
    min-width: 0;
    aspect-ratio: 16 / 10;
    overflow: hidden;
    background: var(--color-border-light);
    border-radius: var(--radius-md);
    text-decoration: none;
  }

  .article-image img {
    width: 100%;
    height: 100%;
    object-fit: cover;
    transition: transform var(--transition-normal);
  }

  .article-card:hover .article-image img {
    transform: scale(1.035);
  }

  .article-content {
    display: grid;
    align-content: start;
    gap: 8px;
    min-width: 0;
  }

  .article-card h3 {
    margin: 0;
    font-size: var(--text-xl);
    line-height: 1.18;
  }

  .article-card h3 a {
    color: var(--color-text);
    text-decoration: none;
  }

  .article-card h3 a:hover {
    color: var(--color-accent-strong);
    text-decoration: underline;
    text-decoration-thickness: 1px;
    text-underline-offset: 3px;
  }

  .article-source {
    display: flex;
    gap: 8px 12px;
    flex-wrap: wrap;
    margin: 0;
  }

  .article-source time {
    color: var(--color-text-muted);
  }

  .article-description {
    margin: 2px 0 0;
    line-height: 1.45;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }

  .empty-state {
    margin: 20px 0;
    padding: 56px 20px;
    text-align: center;
    background: var(--color-bg-secondary);
    border: 1px dashed var(--color-border);
    border-radius: var(--radius-lg);
  }

  .empty-state svg {
    color: var(--color-text-muted);
    margin-bottom: 14px;
  }

  .empty-state h2 {
    margin: 0 0 8px;
    font-size: var(--text-xl);
  }

  .empty-state p {
    max-width: 460px;
    margin: 0 auto;
    color: var(--color-text-secondary);
    font-family: ui-sans-serif, system-ui, sans-serif;
    line-height: 1.5;
  }

  .load-more-section {
    display: flex;
    justify-content: center;
    padding: 28px 0 0;
  }

  .skeleton {
    pointer-events: none;
  }

  .skeleton .article-image,
  .skeleton-line {
    background: linear-gradient(90deg, #e5ded2 25%, #f6f0e6 50%, #e5ded2 75%);
    background-size: 900px 100%;
    animation: shimmer 1.8s infinite;
    border-radius: var(--radius-sm);
  }

  @keyframes shimmer {
    0% { background-position: -900px 0; }
    100% { background-position: 900px 0; }
  }

  @media (max-width: 980px) {
    .page-header {
      grid-template-columns: 1fr;
      align-items: start;
    }

    .header-actions {
      justify-content: flex-start;
    }

    .filters-section {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }

    .search-field {
      grid-column: 1 / -1;
    }

    .trending-grid {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }

    .trending-card:first-child {
      grid-row: auto;
      grid-column: 1 / -1;
      min-height: 210px;
    }

    .article-card,
    .story-card {
      grid-template-columns: 160px minmax(0, 1fr);
    }

    .article-actions {
      grid-column: 1 / -1;
      justify-content: flex-start;
    }
  }

  @media (max-width: 680px) {
    .page-wrapper {
      width: min(100vw - 20px, 1180px);
      padding-top: 12px;
    }

    .page-header {
      padding-top: 12px;
    }

    .page-header h1 {
      font-size: 40px;
    }

    .header-actions,
    .header-actions .button {
      width: 100%;
    }

    .section-tabs {
      margin-left: -10px;
      margin-right: -10px;
      padding-left: 10px;
      padding-right: 10px;
    }

    .filters-section,
    .source-form {
      grid-template-columns: 1fr;
    }

    .search-field {
      min-width: 0;
    }

    .trending-grid {
      grid-template-columns: 1fr;
    }

    .trending-card,
    .trending-card:first-child {
      min-height: auto;
    }

    .trending-card:first-child h3 {
      font-size: var(--text-xl);
    }

    .utility-tabs {
      justify-content: flex-start;
    }

    .article-card,
    .story-card {
      grid-template-columns: 1fr;
      padding: 12px;
    }

    .article-image {
      aspect-ratio: 16 / 9;
    }

    .article-actions,
    .source-actions {
      justify-content: stretch;
    }

    .article-actions .button,
    .article-actions a,
    .source-actions .button {
      flex: 1 1 120px;
    }

    .source-item {
      display: grid;
    }
  }
</style>
