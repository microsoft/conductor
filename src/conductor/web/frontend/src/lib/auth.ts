/**
 * Token auth for requests to the web dashboard (issue #397).
 *
 * `GET /` injects the per-run token into the page as
 * `window.__CONDUCTOR_TOKEN__` (see `web/server.py::index`). The replay
 * app's `/` injects nothing, so `getToken()` returning `undefined` there is
 * expected — `ReplayDashboard` serves no mutating routes and no `/ws`.
 */

declare global {
  interface Window {
    __CONDUCTOR_TOKEN__?: string;
  }
}

/** Return the token injected into the page, or undefined if absent (replay mode). */
export function getToken(): string | undefined {
  return window.__CONDUCTOR_TOKEN__;
}

/** Build the `Authorization` header carrying the token, or `{}` when there is none. */
export function authHeaders(): Record<string, string> {
  const token = getToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

/**
 * Append `?token=` to a URL for the WebSocket handshake, which cannot carry
 * custom headers from the browser.
 */
export function withToken(url: string): string {
  const token = getToken();
  if (!token) return url;
  const separator = url.includes('?') ? '&' : '?';
  return `${url}${separator}token=${encodeURIComponent(token)}`;
}
