import { describe, expect, it } from "vitest";
import { isApiError, type ApiError } from "./types";

describe("ApiError contract", () => {
  it("accepts a minimal envelope", () => {
    const err: ApiError = { code: "validation_error", message: "bad input" };
    expect(isApiError(err)).toBe(true);
  });

  it("accepts an envelope with details", () => {
    const err: ApiError = {
      code: "game_conflict",
      message: "game already in progress",
      details: { gameId: "abc" },
    };
    expect(isApiError(err)).toBe(true);
  });

  it("rejects non-envelope values", () => {
    expect(isApiError(null)).toBe(false);
    expect(isApiError(undefined)).toBe(false);
    expect(isApiError("oops")).toBe(false);
    expect(isApiError({ code: 1, message: "x" })).toBe(false);
    expect(isApiError({ message: "x" })).toBe(false);
  });
});
