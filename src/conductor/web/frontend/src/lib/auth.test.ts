import { afterEach, describe, expect, it } from 'vitest';
import { authHeaders, getToken, withToken } from './auth';

// vitest runs this suite under the `node` environment (no DOM), so `window`
// must be stubbed for auth.ts's `window.__CONDUCTOR_TOKEN__` reads to work.
if (typeof window === 'undefined') {
  (globalThis as unknown as { window: object }).window = {};
}

function setToken(value: string | undefined) {
  if (value === undefined) {
    delete window.__CONDUCTOR_TOKEN__;
  } else {
    window.__CONDUCTOR_TOKEN__ = value;
  }
}

afterEach(() => {
  setToken(undefined);
});

describe('getToken', () => {
  it('returns undefined when no token was injected (replay mode)', () => {
    expect(getToken()).toBeUndefined();
  });

  it('returns the injected token', () => {
    setToken('abc123');
    expect(getToken()).toBe('abc123');
  });
});

describe('authHeaders', () => {
  it('returns an empty object when there is no token', () => {
    expect(authHeaders()).toEqual({});
  });

  it('returns an Authorization header carrying the token', () => {
    setToken('abc123');
    expect(authHeaders()).toEqual({ Authorization: 'Bearer abc123' });
  });
});

describe('withToken', () => {
  it('returns the URL unchanged when there is no token', () => {
    expect(withToken('ws://host/ws')).toBe('ws://host/ws');
  });

  it('appends ?token= when the URL has no existing query string', () => {
    setToken('abc123');
    expect(withToken('ws://host/ws')).toBe('ws://host/ws?token=abc123');
  });

  it('appends &token= when the URL already has a query string', () => {
    setToken('abc123');
    expect(withToken('ws://host/ws?foo=bar')).toBe('ws://host/ws?foo=bar&token=abc123');
  });

  it('URL-encodes the token', () => {
    setToken('a b/c');
    expect(withToken('ws://host/ws')).toBe('ws://host/ws?token=a%20b%2Fc');
  });
});
