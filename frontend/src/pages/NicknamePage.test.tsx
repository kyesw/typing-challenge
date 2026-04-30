/**
 * Component tests for the Nickname entry page (task 12.2).
 *
 * Covers:
 *   * Client-side validation rejects too-short / too-long / bad-charset
 *     values before any network call (Requirements 1.5, 1.6).
 *   * A successful submission persists the session and navigates to
 *     ``/ready`` (Requirements 1.2, 1.3, 1.4).
 *   * A 409 ``nickname_taken`` response renders the server message
 *     inline and keeps the player on the page (Requirements 1.7, 1.8).
 *   * A 400 ``validation_error`` response surfaces inline
 *     (Requirement 1.8).
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import {
  NICKNAME_ALLOWED_PATTERN,
  NICKNAME_MAX_LENGTH,
  NICKNAME_MIN_LENGTH,
  NicknamePage,
  validateNickname,
} from "./NicknamePage";
import { SESSION_TOKEN_KEY } from "../api/client";

function renderPage() {
  return render(
    <MemoryRouter initialEntries={["/"]}>
      <Routes>
        <Route path="/" element={<NicknamePage />} />
        <Route
          path="/ready"
          element={<div data-testid="ready-marker">ready</div>}
        />
      </Routes>
    </MemoryRouter>,
  );
}

function stubFetch(response: {
  status: number;
  body?: unknown;
}): ReturnType<typeof vi.fn> {
  const text = response.body !== undefined ? JSON.stringify(response.body) : "";
  const mock = vi.fn(
    (_url: RequestInfo | URL, _init?: RequestInit) =>
      Promise.resolve({
        ok: response.status >= 200 && response.status < 300,
        status: response.status,
        text: () => Promise.resolve(text),
      } as unknown as Response),
  );
  vi.stubGlobal("fetch", mock);
  return mock;
}

describe("validateNickname", () => {
  it("accepts a well-formed nickname", () => {
    expect(validateNickname("Alice")).toBeNull();
    expect(validateNickname("a_b-c 1")).toBeNull();
  });

  it("rejects too-short values", () => {
    expect(validateNickname("")).toEqual({ kind: "length", length: 0 });
    expect(validateNickname("A")).toEqual({ kind: "length", length: 1 });
  });

  it("rejects too-long values", () => {
    expect(validateNickname("A".repeat(21))).toEqual({
      kind: "length",
      length: 21,
    });
  });

  it("rejects disallowed characters and reports them unique-in-order", () => {
    const result = validateNickname("bad$na#me$");
    expect(result).toEqual({
      kind: "charset",
      invalidChars: ["$", "#"],
    });
  });

  it("exposes the same bounds as the module-level constants", () => {
    expect(NICKNAME_MIN_LENGTH).toBe(2);
    expect(NICKNAME_MAX_LENGTH).toBe(20);
    expect(NICKNAME_ALLOWED_PATTERN.test("abc 1_-")).toBe(true);
    expect(NICKNAME_ALLOWED_PATTERN.test("abc!")).toBe(false);
  });
});

describe("NicknamePage", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders the form fields", () => {
    renderPage();
    expect(screen.getByTestId("nickname-input")).toBeInTheDocument();
    expect(screen.getByTestId("nickname-submit")).toBeInTheDocument();
  });

  it("rejects a too-short nickname without calling fetch", async () => {
    const user = userEvent.setup();
    const fetchMock = stubFetch({ status: 500 });
    renderPage();

    await user.type(screen.getByTestId("nickname-input"), "A");
    await user.click(screen.getByTestId("nickname-submit"));

    expect(fetchMock).not.toHaveBeenCalled();
    expect(screen.getByTestId("nickname-error")).toHaveTextContent(
      /between 2 and 20/i,
    );
  });

  it("rejects a nickname with disallowed characters", async () => {
    const user = userEvent.setup();
    const fetchMock = stubFetch({ status: 500 });
    renderPage();

    await user.type(screen.getByTestId("nickname-input"), "bad$name");
    await user.click(screen.getByTestId("nickname-submit"));

    expect(fetchMock).not.toHaveBeenCalled();
    expect(screen.getByTestId("nickname-error")).toHaveTextContent(
      /letters, digits, spaces, hyphens, and underscores/i,
    );
  });

  it("registers, persists the session, and navigates on success", async () => {
    const user = userEvent.setup();
    const fetchMock = stubFetch({
      status: 201,
      body: {
        playerId: "p1",
        sessionToken: "tok",
        nickname: "Alice",
        sessionExpiresAt: "2030-01-01T00:00:00Z",
      },
    });

    renderPage();

    await user.type(screen.getByTestId("nickname-input"), "Alice");
    await user.click(screen.getByTestId("nickname-submit"));

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const call = fetchMock.mock.calls[0]!;
    const url = call[0] as string;
    const init = call[1] as RequestInit;
    expect(url).toMatch(/\/players$/);
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({
      nickname: "Alice",
    });

    expect(await screen.findByTestId("ready-marker")).toBeInTheDocument();
    expect(window.localStorage.getItem(SESSION_TOKEN_KEY)).toBe("tok");
  });

  it("shows the server's message inline on 409 nickname_taken", async () => {
    const user = userEvent.setup();
    stubFetch({
      status: 409,
      body: {
        code: "nickname_taken",
        message: "That nickname is already in use.",
      },
    });

    renderPage();
    await user.type(screen.getByTestId("nickname-input"), "Alice");
    await user.click(screen.getByTestId("nickname-submit"));

    expect(await screen.findByTestId("nickname-error")).toHaveTextContent(
      /already in use/i,
    );
    // Still on the nickname page (no navigation happened).
    expect(screen.queryByTestId("ready-marker")).toBeNull();
    expect(window.localStorage.getItem(SESSION_TOKEN_KEY)).toBeNull();
  });

  it("shows the server's message inline on 400 validation_error", async () => {
    const user = userEvent.setup();
    stubFetch({
      status: 400,
      body: {
        code: "validation_error",
        message: "Nickname must be between 2 and 20 characters.",
        details: { reason: "length" },
      },
    });

    renderPage();
    // Pass client-side validation with a reasonable candidate so the
    // server-side branch is the one rejecting.
    await user.type(screen.getByTestId("nickname-input"), "Alice");
    await user.click(screen.getByTestId("nickname-submit"));

    expect(await screen.findByTestId("nickname-error")).toHaveTextContent(
      /between 2 and 20/i,
    );
    expect(screen.queryByTestId("ready-marker")).toBeNull();
  });

  it("surfaces a generic message when the network call rejects", async () => {
    const user = userEvent.setup();
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.reject(new Error("network down"))),
    );

    renderPage();
    await user.type(screen.getByTestId("nickname-input"), "Alice");
    await user.click(screen.getByTestId("nickname-submit"));

    expect(await screen.findByTestId("nickname-error")).toHaveTextContent(
      /unable to reach the server/i,
    );
  });
});
