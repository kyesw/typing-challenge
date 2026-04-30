/**
 * Unit tests for ``src/api/client.ts``.
 *
 * Covers the behaviors task 12.1 / 12.2 rely on:
 *   * Session token is attached from ``localStorage`` on authenticated
 *     calls.
 *   * ``POST /players`` helper sends the nickname body and surfaces
 *     400 / 409 ``ApiError`` envelopes as :class:`ApiRequestError`.
 *   * 401 responses clear local session state and redirect to ``/``.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  apiFetch,
  ApiRequestError,
  clearSession,
  getLeaderboard,
  getSessionToken,
  NICKNAME_KEY,
  PLAYER_ID_KEY,
  registerPlayer,
  SESSION_TOKEN_KEY,
  setSession,
} from "./client";

function mockFetchOnce(response: { status?: number; json?: unknown }): void {
  const text =
    response.json !== undefined ? JSON.stringify(response.json) : "";
  const status = response.status ?? 200;
  const res = {
    ok: status >= 200 && status < 300,
    status,
    text: () => Promise.resolve(text),
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(() => Promise.resolve(res as unknown as Response)),
  );
}

describe("session storage helpers", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  it("round-trips a session", () => {
    setSession({
      sessionToken: "tok",
      playerId: "pid",
      nickname: "Alice",
    });
    expect(getSessionToken()).toBe("tok");
    expect(window.localStorage.getItem(PLAYER_ID_KEY)).toBe("pid");
    expect(window.localStorage.getItem(NICKNAME_KEY)).toBe("Alice");
  });

  it("clears session state", () => {
    setSession({
      sessionToken: "tok",
      playerId: "pid",
      nickname: "Alice",
    });
    clearSession();
    expect(getSessionToken()).toBeNull();
    expect(window.localStorage.getItem(PLAYER_ID_KEY)).toBeNull();
  });
});

describe("apiFetch", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("attaches Authorization header when a token is stored", async () => {
    window.localStorage.setItem(SESSION_TOKEN_KEY, "tok");
    const fetchMock = vi.fn(
      (_url: RequestInfo | URL, _init?: RequestInit) =>
        Promise.resolve({
          ok: true,
          status: 200,
          text: () => Promise.resolve(JSON.stringify({ ok: true })),
        } as unknown as Response),
    );
    vi.stubGlobal("fetch", fetchMock);

    await apiFetch("/games", { method: "POST", json: {} });

    const init = fetchMock.mock.calls[0]![1]!;
    const headers = new Headers(init.headers);
    expect(headers.get("Authorization")).toBe("Bearer tok");
    expect(headers.get("Content-Type")).toBe("application/json");
  });

  it("omits Authorization when skipAuth is set", async () => {
    window.localStorage.setItem(SESSION_TOKEN_KEY, "tok");
    const fetchMock = vi.fn(
      (_url: RequestInfo | URL, _init?: RequestInit) =>
        Promise.resolve({
          ok: true,
          status: 200,
          text: () => Promise.resolve(JSON.stringify({})),
        } as unknown as Response),
    );
    vi.stubGlobal("fetch", fetchMock);

    await apiFetch("/players", {
      method: "POST",
      json: { nickname: "Alice" },
      skipAuth: true,
    });

    const init = fetchMock.mock.calls[0]![1]!;
    const headers = new Headers(init.headers);
    expect(headers.get("Authorization")).toBeNull();
  });

  it("throws ApiRequestError preserving the envelope on 4xx", async () => {
    mockFetchOnce({
      status: 409,
      json: {
        code: "nickname_taken",
        message: "That nickname is already in use.",
      },
    });

    await expect(apiFetch("/players", { method: "POST" })).rejects.toMatchObject({
      name: "ApiRequestError",
      status: 409,
      error: {
        code: "nickname_taken",
        message: "That nickname is already in use.",
      },
    });
  });

  it("clears session and redirects on 401", async () => {
    window.localStorage.setItem(SESSION_TOKEN_KEY, "tok");
    window.localStorage.setItem(PLAYER_ID_KEY, "pid");
    mockFetchOnce({
      status: 401,
      json: { code: "session_expired", message: "Session expired." },
    });
    const assignSpy = vi.fn();
    // jsdom lets us redefine window.location for the duration of the test.
    const originalLocation = window.location;
    Object.defineProperty(window, "location", {
      configurable: true,
      value: { ...originalLocation, assign: assignSpy, pathname: "/ready" },
    });

    try {
      await expect(
        apiFetch("/games", { method: "POST" }),
      ).rejects.toBeInstanceOf(ApiRequestError);
      expect(getSessionToken()).toBeNull();
      expect(assignSpy).toHaveBeenCalledWith("/");
    } finally {
      Object.defineProperty(window, "location", {
        configurable: true,
        value: originalLocation,
      });
    }
  });

  it("produces a generic ApiRequestError when the body is not JSON", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve({
          ok: false,
          status: 500,
          text: () => Promise.resolve("<html>nope</html>"),
        } as unknown as Response),
      ),
    );

    const caught = await apiFetch("/players").catch((e) => e);
    expect(caught).toBeInstanceOf(ApiRequestError);
    expect((caught as ApiRequestError).error.code).toBe("internal_error");
    expect((caught as ApiRequestError).status).toBe(500);
  });
});

describe("registerPlayer", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("returns the decoded body on 201", async () => {
    mockFetchOnce({
      status: 201,
      json: {
        playerId: "p1",
        sessionToken: "tok",
        nickname: "Alice",
        sessionExpiresAt: "2030-01-01T00:00:00Z",
      },
    });

    const result = await registerPlayer("Alice");
    expect(result).toEqual({
      playerId: "p1",
      sessionToken: "tok",
      nickname: "Alice",
      sessionExpiresAt: "2030-01-01T00:00:00Z",
    });
  });

  it("maps a 400 validation error to ApiRequestError", async () => {
    mockFetchOnce({
      status: 400,
      json: {
        code: "validation_error",
        message: "Nickname must be between 2 and 20 characters.",
        details: { reason: "length" },
      },
    });

    await expect(registerPlayer("A")).rejects.toMatchObject({
      status: 400,
      error: { code: "validation_error" },
    });
  });
});

describe("getLeaderboard", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("issues a GET /leaderboard and returns the decoded snapshot", async () => {
    const body = {
      generatedAt: "2030-01-01T00:00:00Z",
      entries: [
        {
          playerId: "p1",
          nickname: "Alice",
          bestWpm: 72.4,
          bestAccuracy: 99.1,
          bestPoints: 717,
          rank: 1,
        },
      ],
    };
    const fetchMock = vi.fn(
      (_url: RequestInfo | URL, _init?: RequestInit) =>
        Promise.resolve({
          ok: true,
          status: 200,
          text: () => Promise.resolve(JSON.stringify(body)),
        } as unknown as Response),
    );
    vi.stubGlobal("fetch", fetchMock);

    const snapshot = await getLeaderboard();

    expect(snapshot).toEqual(body);
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(String(url)).toMatch(/\/leaderboard$/);
    expect((init as RequestInit | undefined)?.method).toBe("GET");
  });

  it("propagates ApiRequestError on non-2xx responses", async () => {
    mockFetchOnce({
      status: 500,
      json: {
        code: "internal_error",
        message: "Database is down.",
      },
    });

    await expect(getLeaderboard()).rejects.toMatchObject({
      status: 500,
      error: { code: "internal_error" },
    });
  });
});
