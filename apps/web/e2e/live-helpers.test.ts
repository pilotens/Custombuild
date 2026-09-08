import type { APIRequestContext, TestInfo } from "@playwright/test";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { provisionLiveProject } from "./live-helpers";

function setup() {
  const get = vi.fn(async (url: string) => ({
    ok: () => true,
    json: async () => url.endsWith("/ready")
      ? { database: "ok", redis: "ok", object_storage: "ok", rule_engine: "ok" }
      : { user_id: "user", organization_id: "organization" },
  }));
  const post = vi.fn(async (_url: string, options: { data: { name: string } }) => ({
    status: () => 201,
    json: async () => ({ id: options.data.name, name: options.data.name }),
  }));
  const request = { get, post } as unknown as APIRequestContext;
  const info = { timeout: 120_000, retry: 0, setTimeout: vi.fn() } as unknown as TestInfo;
  return { get, post, request, info };
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.stubEnv("PLAYWRIGHT_API_URL", "http://api.test");
  vi.stubEnv("PLAYWRIGHT_ENVIRONMENT", "prod");
  vi.stubEnv("PLAYWRIGHT_RUN_ID", "isolation-test");
});
afterEach(() => { vi.useRealTimers(); vi.unstubAllEnvs(); });

describe("live scenario request-window isolation", () => {
  it("waits before any API traffic and isolates the next scenario separately", async () => {
    const { get, request, info } = setup();
    const first = provisionLiveProject(request, info, "first");
    await vi.advanceTimersByTimeAsync(60_000);
    expect(get).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1_000);
    await first;
    expect(get).toHaveBeenCalledTimes(2);
    expect(info.setTimeout).toHaveBeenCalledExactlyOnceWith(181_000);

    const next = { ...info, setTimeout: vi.fn() } as TestInfo;
    const second = provisionLiveProject(request, next, "next-scenario");
    await vi.advanceTimersByTimeAsync(60_000);
    expect(get).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(1_000);
    await second;
    expect(get).toHaveBeenCalledTimes(4);
  });

  it("shares one window for concurrent and later projects in the same scenario", async () => {
    const { get, post, request, info } = setup();
    const first = provisionLiveProject(request, info, "wall");
    const second = provisionLiveProject(request, info, "reference");
    await vi.advanceTimersByTimeAsync(60_000);
    expect(get).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1_000);
    const projects = await Promise.all([first, second]);
    expect(projects[0].project.id).not.toEqual(projects[1].project.id);
    await provisionLiveProject(request, info, "later");
    expect(post).toHaveBeenCalledTimes(3);
    expect(info.setTimeout).toHaveBeenCalledTimes(1);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("surfaces an in-scenario rate-limit error without retrying the mutation", async () => {
    const { post, request, info } = setup();
    post.mockResolvedValueOnce({ status: () => 429, url: () => "http://api.test/v1/projects",
      text: async () => "Rate limit exceeded" } as never);
    const result = provisionLiveProject(request, info, "rate-limited");
    const assertion = expect(result).rejects.toThrow("HTTP 429");
    await vi.advanceTimersByTimeAsync(61_000);
    await assertion;
    expect(post).toHaveBeenCalledTimes(1);
    expect(vi.getTimerCount()).toBe(0);
  });
});
