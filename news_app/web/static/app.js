const summaryPanel = document.querySelector("#summary-panel");
const summaryContent = document.querySelector("#summary-content");
const summarySource = document.querySelector("#summary-source");
const summaryLink = document.querySelector("#summary-link");
const notice = document.querySelector("#notice");
const offlineState = document.querySelector("#offline-state");

function showNotice(message, isError = false) {
  if (!notice) return;
  notice.textContent = message;
  notice.hidden = false;
  notice.classList.toggle("error", isError);
}

function clearNotice() {
  if (!notice) return;
  notice.hidden = true;
  notice.textContent = "";
  notice.classList.remove("error");
}

function sendArticleEvent(articleId, eventType, eventValue = null) {
  if (!articleId) return;
  const payload = JSON.stringify({
    article_id: Number(articleId),
    event_type: eventType,
    event_value: eventValue,
  });
  const blob = new Blob([payload], { type: "application/json" });
  if (navigator.sendBeacon && navigator.sendBeacon("/api/events", blob)) {
    return;
  }
  fetch("/api/events", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: payload,
    keepalive: true,
  }).catch(() => {});
}

function bindSummaryButtons(root = document) {
  root.querySelectorAll(".summary-button:not([data-bound])").forEach((button) => {
    button.dataset.bound = "true";
    button.addEventListener("click", async () => {
      const articleId = button.dataset.articleId;
      if (!summaryContent || !summarySource || !summaryLink) return;
      summaryContent.textContent = "Generating summary...";
      summarySource.textContent = "";
      summaryLink.hidden = true;
      clearNotice();

      try {
        const response = await fetch(`/api/articles/${articleId}/summary`, { method: "POST" });
        const payload = await response.json();
        if (!response.ok) {
          throw new Error(payload.detail || "Summary failed");
        }
        summaryContent.textContent = payload.summary || "No summary returned.";
        summarySource.textContent = `${payload.source_name} · ${payload.source_country}`;
        summaryLink.href = payload.article_url;
        summaryLink.hidden = false;
        summaryPanel?.scrollIntoView({ behavior: "smooth", block: "nearest" });
      } catch (error) {
        summaryContent.textContent = "Summary unavailable.";
        showNotice(error.message, true);
      }
    });
  });
}

function bindBookmarkButtons(root = document) {
  root.querySelectorAll(".bookmark-button:not([data-bound])").forEach((button) => {
    button.dataset.bound = "true";
    button.addEventListener("click", async () => {
      const articleId = button.dataset.articleId;
      const wasBookmarked = button.dataset.bookmarked === "true";
      button.disabled = true;
      clearNotice();

      try {
        const response = await fetch(`/api/bookmarks/${articleId}`, {
          method: wasBookmarked ? "DELETE" : "POST",
        });
        const payload = await response.json();
        if (!response.ok) {
          throw new Error(payload.detail || "Bookmark failed");
        }
        const isBookmarked = !wasBookmarked;
        button.dataset.bookmarked = isBookmarked ? "true" : "false";
        button.setAttribute("aria-pressed", isBookmarked ? "true" : "false");
        button.classList.toggle("active", isBookmarked);
        button.textContent = isBookmarked ? "Saved" : "Read Later";
        if (!isBookmarked && new URLSearchParams(window.location.search).get("view") === "read_later") {
          button.closest(".article-row")?.remove();
        }
      } catch (error) {
        showNotice(error.message, true);
      } finally {
        button.disabled = false;
      }
    });
  });
}

function bindShareButtons(root = document) {
  root.querySelectorAll(".share-button:not([data-bound])").forEach((button) => {
    button.dataset.bound = "true";
    button.addEventListener("click", async () => {
      const articleId = button.dataset.articleId;
      button.disabled = true;
      clearNotice();

      try {
        const response = await fetch(`/api/articles/${articleId}/share`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ platform: null }),
        });
        const payload = await response.json();
        if (!response.ok) {
          throw new Error(payload.detail || "Share failed");
        }

        if (navigator.share) {
          await navigator.share({
            title: payload.message,
            text: payload.message,
            url: payload.share_url,
          });
        } else if (navigator.clipboard) {
          await navigator.clipboard.writeText(payload.share_url);
          showNotice("Share link copied.");
        } else {
          window.open(payload.whatsapp_url, "_blank", "noopener");
        }
      } catch (error) {
        if (error.name !== "AbortError") {
          showNotice(error.message, true);
        }
      } finally {
        button.disabled = false;
      }
    });
  });
}

function bindSourceLinks(root = document) {
  root.querySelectorAll(".source-link:not([data-bound])").forEach((link) => {
    link.dataset.bound = "true";
    link.addEventListener("click", () => {
      sendArticleEvent(link.dataset.articleId, "source_open");
    });
  });
}

function observeArticleViews() {
  const articleNodes = document.querySelectorAll(".article-row[data-article-id], .article-detail[data-article-id]");
  if (!articleNodes.length || !("IntersectionObserver" in window)) return;
  const seen = new Set();
  const observer = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        const articleId = entry.target.dataset.articleId;
        if (seen.has(articleId)) return;
        seen.add(articleId);
        sendArticleEvent(articleId, "view");
        observer.unobserve(entry.target);
      });
    },
    { threshold: 0.5 }
  );
  articleNodes.forEach((node) => observer.observe(node));
}

function bindControls(root = document) {
  bindSummaryButtons(root);
  bindBookmarkButtons(root);
  bindShareButtons(root);
  bindSourceLinks(root);
}

const closeSummary = document.querySelector("#close-summary");
if (closeSummary) {
  closeSummary.addEventListener("click", () => {
    if (!summaryContent || !summarySource || !summaryLink) return;
    summaryContent.textContent = "Select a headline.";
    summarySource.textContent = "";
    summaryLink.hidden = true;
  });
}

const ingestButton = document.querySelector("#ingest-button");
if (ingestButton) {
  ingestButton.addEventListener("click", async () => {
    ingestButton.disabled = true;
    ingestButton.textContent = "Ingesting...";
    clearNotice();
    try {
      const response = await fetch("/api/ingest/recent", { method: "POST" });
      const payload = await response.json();
      if (!response.ok) {
        throw new Error(payload.detail || "Ingestion failed");
      }
      showNotice(payload.status === "already_running" ? "Ingestion already running." : "Ingestion started.");
    } catch (error) {
      showNotice(error.message, true);
    } finally {
      ingestButton.disabled = false;
      ingestButton.textContent = "Ingest Latest 24 Hours";
    }
  });
}

function openOfflineDb() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open("news-offline-v1", 1);
    request.onupgradeneeded = () => {
      request.result.createObjectStore("articles", { keyPath: "id" });
      request.result.createObjectStore("meta", { keyPath: "key" });
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

async function storeOfflineArticles(articles) {
  const db = await openOfflineDb();
  const transaction = db.transaction(["articles", "meta"], "readwrite");
  const articleStore = transaction.objectStore("articles");
  articleStore.clear();
  articles.forEach((article) => articleStore.put(article));
  transaction.objectStore("meta").put({ key: "updated_at", value: new Date().toISOString() });
}

async function readOfflineArticles() {
  const db = await openOfflineDb();
  return new Promise((resolve, reject) => {
    const request = db.transaction("articles", "readonly").objectStore("articles").getAll();
    request.onsuccess = () => resolve(request.result || []);
    request.onerror = () => reject(request.error);
  });
}

async function refreshOfflineCache() {
  if (!navigator.onLine) return;
  try {
    const response = await fetch("/api/offline/top?limit=20");
    if (!response.ok) return;
    const payload = await response.json();
    await storeOfflineArticles(payload.articles || []);
  } catch {
    // Offline caching is best-effort.
  }
}

async function renderOfflineArticles() {
  const list = document.querySelector("#article-list");
  if (!list || navigator.onLine) return;
  const articles = await readOfflineArticles();
  if (!articles.length) return;
  list.innerHTML = articles.map(renderOfflineArticle).join("");
  bindControls(list);
}

function renderOfflineArticle(article) {
  const image = article.thumbnail_path
    ? `<a class="thumbnail" href="${escapeHtml(article.article_path)}"><img src="${escapeHtml(article.thumbnail_path)}" alt=""></a>`
    : "";
  return `
    <article class="article-row" data-article-id="${article.id}">
      ${image}
      <div class="article-main">
        <p class="kicker">
          <span>${escapeHtml(article.primary_category || "General")}</span>
          <span>${article.reading_time_minutes || 1} min read</span>
        </p>
        <h2><a href="${escapeHtml(article.article_path)}">${escapeHtml(article.headline)}</a></h2>
        <p class="meta">
          <span>${escapeHtml(article.source_name)}</span>
          <span>${escapeHtml(article.source_region)}</span>
        </p>
        <p class="description">${escapeHtml(article.description || "")}</p>
      </div>
      <div class="actions">
        <button type="button" class="bookmark-button ${article.is_bookmarked ? "active" : ""}" data-article-id="${article.id}" data-bookmarked="${article.is_bookmarked ? "true" : "false"}">${article.is_bookmarked ? "Saved" : "Read Later"}</button>
        <button type="button" class="share-button" data-article-id="${article.id}">Share</button>
        <a href="${escapeHtml(article.article_url)}" target="_blank" rel="noreferrer" class="source-link" data-article-id="${article.id}">Source</a>
      </div>
    </article>
  `;
}

function escapeHtml(value) {
  return String(value || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function updateOfflineState() {
  const offline = !navigator.onLine;
  document.body.classList.toggle("offline", offline);
  if (offlineState) offlineState.hidden = !offline;
}

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/service-worker.js").catch(() => {});
  });
}

window.addEventListener("online", () => {
  updateOfflineState();
  refreshOfflineCache();
});
window.addEventListener("offline", () => {
  updateOfflineState();
  renderOfflineArticles().catch(() => {});
});

bindControls();
observeArticleViews();
updateOfflineState();
refreshOfflineCache();
renderOfflineArticles().catch(() => {});
