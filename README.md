# Local News Aggregator

Production-oriented local news aggregation app using public RSS feeds, SQLite, FastAPI, and a local MLX LLM for short headlines and on-demand summaries.

## Architecture

- **Sources**: `config/sources.json` contains public RSS feeds from Indian and global publishers. Add or disable sources there without changing Python code.
- **Ingestion**: `news_app.ingestion` fetches RSS entries, keeps recent articles, extracts article text where possible, deduplicates URLs, and stores records.
- **LLM**: `news_app.llm` runs the configured model locally through MLX. The default is `mlx-community/Qwen3.5-4B-MLX-8bit` using `mlx-vlm`; Ollama is not used.
- **Database**: SQLAlchemy + SQLite stores article metadata, source links, original text, generated headlines, and generated summaries.
- **UI/API**: FastAPI serves a built SvelteKit PWA app shell plus JSON endpoints for search, categories, bookmarks, sharing, offline bundles, and personalization signals. Legacy Jinja templates remain only as a fallback if the frontend build is missing.
- **Scheduler**: APScheduler can run rolling latest-24-hour ingestion every morning.

## Setup on Mac with local Qwen 3.5 4B Q8 through MLX

Use Python 3.11+ on Apple Silicon for best MLX compatibility.

```bash
cd "NEWS APP"
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
pip install -r requirements.txt
pip install -e .
cp .env.example .env
```

The required `.env` values are already configured:

```dotenv
LLM_PROVIDER=mlx
LLM_MODEL=mlx-community/Qwen3.5-4B-MLX-8bit
MLX_ENGINE=vlm
REQUIRE_LLM=true
MLX_NO_THINK=true
MLX_TEMPERATURE=0.2
MLX_TOP_P=0.9
```

Download and warm up the model once:

```bash
source .venv/bin/activate
python scripts/download_mlx_model.py
```

Manual smoke test:

```bash
source .venv/bin/activate
python scripts/test_mlx_generation.py
```

`mlx-community/Qwen3.5-4B-MLX-8bit` is an MLX SafeTensors 8-bit conversion of `Qwen/Qwen3.5-4B`. This app uses `mlx-vlm` for that checkpoint because the Qwen 3.5 MLX community model card lists `mlx_vlm.load` / `mlx_vlm.generate` usage.

## Frontend setup

```bash
npm --prefix frontend install
npm --prefix frontend run build
```

If `npm` is not installed on your Mac:

```bash
brew install node
```

## Run

```bash
source .venv/bin/activate
uvicorn news_app.main:app --reload --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000`.

## Collect News

From the UI, press **Ingest Latest 24 Hours**. Or run:

```bash
source .venv/bin/activate
python scripts/ingest_recent.py
```

Refresh now performs one complete recent-news run: it fetches all enabled feeds for the incremental/24-hour window, ranks and filters important stories, processes the selected articles with the local MLX model, rebuilds multi-source story clusters, and saves everything to SQLite. The **Load More** button no longer starts ingestion; it only paginates the next 25 already-saved cards from the database.

Backfill existing articles after upgrading:

```bash
source .venv/bin/activate
python scripts/backfill_articles.py
python scripts/backfill_articles.py --thumbnails
```

## API

- `GET /api/articles?date=YYYY-MM-DD`
- `GET /api/articles?category=&q=&sort=relevance|date&view=latest|read_later`
- `GET /api/categories`
- `GET /api/bookmarks`
- `POST /api/bookmarks/{id}`
- `DELETE /api/bookmarks/{id}`
- `POST /api/articles/{id}/summary`
- `POST /api/articles/{id}/share`
- `GET /s/{token}`
- `POST /api/events`
- `GET /api/offline/top?limit=20`
- `GET /api/me`
- `POST /api/ingest/recent`
- `POST /api/ingest/recent/more`
- `GET /api/ingest/status`

The SvelteKit app polls `/api/ingest/status` after starting ingestion and refreshes results automatically when the background run completes. The web app registers `/service-worker.js` and exposes `/manifest.json`.

SQLite is configured with WAL mode at connection time to improve concurrent read/write behavior.

## Notes

- Public RSS feeds are preferred over arbitrary scraping. This keeps collection reliable, auditable, and easier to operate.
- Some publishers may omit precise timestamps or block full article extraction. Those items are skipped or stored with feed-provided content.
- Source links are always preserved so you can open the original article.

## Deploying to Vercel

### Prerequisites
- A [Vercel](https://vercel.com) account
- An [OpenRouter](https://openrouter.ai) API key (free tier available)

### Steps

1. Push this repository to GitHub (or GitLab / Bitbucket).

2. In the Vercel dashboard, click **Add New Project** and import your repository.

3. Vercel will detect `vercel.json` automatically. No framework preset changes are needed.

4. Under **Environment Variables**, add the following:

   | Variable | Value |
   |---|---|
   | `LLM_PROVIDER` | `openai_compatible` |
   | `LLM_MODEL` | `google/gemma-4-31b-it:free` (free) or other OpenRouter model |
   | `OPENAI_COMPATIBLE_BASE_URL` | `https://openrouter.ai/api/v1` |
   | `OPENAI_COMPATIBLE_API_KEY` | Your OpenRouter key (`sk-or-v1-...`) |
   | `OPENAI_COMPATIBLE_REFERER` | Your Vercel app URL |
   | `OPENAI_COMPATIBLE_TITLE` | `NewsApp` |
   | `REQUIRE_LLM` | `false` |
   | `SESSION_SECRET` | A long random string |
   | `SCHEDULER_ENABLED` | `false` (Vercel has no persistent scheduler) |

5. Click **Deploy**. Vercel builds the SvelteKit frontend and deploys the FastAPI backend as a serverless function.

### Important limitations on Vercel

- **SQLite is ephemeral**: The database lives in `/tmp` and is wiped on each cold start. All fetched articles and bookmarks are lost between cold starts. For persistent storage, set `DATABASE_URL` to a hosted Postgres, PlanetScale, or [Turso](https://turso.tech) (libSQL) URL.
- **No background scheduler**: APScheduler is disabled in serverless mode. Trigger ingestion manually by clicking Refresh in the UI, or set up a Vercel Cron Job that calls `POST /api/ingest/recent`.
- **Ingestion timeout**: Vercel serverless functions have a maximum execution time (10s on Hobby, 60s on Pro). Reduce `MAX_ARTICLES_PER_RUN` to `30` and `FEED_FETCH_CONCURRENCY` to `4` to stay within limits.
- **Thumbnail caching**: Set `CACHE_THUMBNAILS_DURING_INGESTION=false` to avoid writing to the filesystem during ingestion.

### Recommended environment variables for Vercel

```env
MAX_ARTICLES_PER_RUN=30
FEED_FETCH_CONCURRENCY=4
ARTICLE_FETCH_CONCURRENCY=6
CACHE_THUMBNAILS_DURING_INGESTION=false
SCHEDULER_ENABLED=false
REQUIRE_LLM=false
```
