// API base URL. In production on Vercel, this is empty (same-origin).
// For a separate backend URL, set VITE_API_BASE_URL in Vercel environment variables.
const BASE = (typeof import.meta !== 'undefined' && import.meta.env?.VITE_API_BASE_URL) || '';

export async function apiGet(path) {
  const response = await fetch(BASE + path, { credentials: 'same-origin' });
  return readJson(response);
}

export async function apiPost(path, body = null) {
  const response = await fetch(BASE + path, {
    method: 'POST',
    credentials: 'same-origin',
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined
  });
  return readJson(response);
}

export async function apiPut(path, body = null) {
  const response = await fetch(BASE + path, {
    method: 'PUT',
    credentials: 'same-origin',
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined
  });
  return readJson(response);
}

export async function apiDelete(path) {
  const response = await fetch(BASE + path, {
    method: 'DELETE',
    credentials: 'same-origin'
  });
  return readJson(response);
}

async function readJson(response) {
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.detail || `Request failed with ${response.status}`);
  }
  return payload;
}

export function articleQuery(params) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== null && value !== undefined && value !== '') {
      query.set(key, value);
    }
  }
  return `${BASE}/api/articles?${query.toString()}`;
}
