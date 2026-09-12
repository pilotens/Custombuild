import { afterEach, describe, expect, it, vi } from "vitest";
import { replaceFurnitureRecovery } from "./furniture-draft-recovery";
import { TestWebLocks } from "../tests/web-locks";

afterEach(() => vi.restoreAllMocks());

function storageWith(initial: string | null) {
  let value = initial;
  return {
    getItem: vi.fn(() => value),
    setItem: vi.fn((_key: string, next: string) => { value = next; }),
    removeItem: vi.fn(() => { value = null; }),
    value: () => value,
  };
}

describe("exclusive recovery mutations", () => {
  it.each(["other", null])("serializes a competing write and %s mutation without losing the winner", async other => {
    const storage = storageWith("before");
    const first = { current: "before" as string | null };
    const second = { current: "before" as string | null };
    const results = await Promise.allSettled([
      replaceFurnitureRecovery(storage, "same-complete-key", first, "winner", () => true),
      replaceFurnitureRecovery(storage, "same-complete-key", second, other, () => true),
    ]);
    expect(results[0]).toEqual({ status: "fulfilled", value: true });
    expect(results[1].status).toBe("rejected");
    expect(storage.value()).toBe("winner");
    expect(first.current).toBe("winner");
    expect(second.current).toBe("before");
    expect(storage.setItem).toHaveBeenCalledTimes(1);
    expect(storage.removeItem).not.toHaveBeenCalled();
  });

  it("reads the live expected ref and advances it inside the lock before its promise settles", async () => {
    const storage = storageWith("before");
    const expected = { current: "before" as string | null };
    const queue = new TestWebLocks();
    const checkedInside: (string | null)[] = [];
    Object.defineProperty(navigator, "locks", { configurable: true, value: {
      request: <T,>(name: string, options: { mode?: LockMode; signal?: AbortSignal }, callback: (lock: Lock) => T) =>
        queue.request(name, options, lock => {
          const result = callback(lock);
          checkedInside.push(expected.current);
          return result;
        }),
    } });
    await Promise.all([
      replaceFurnitureRecovery(storage, "same-ref", expected, "first", () => true),
      replaceFurnitureRecovery(storage, "same-ref", expected, "second", () => true),
    ]);
    expect(checkedInside).toEqual(["first", "second"]);
    expect(storage.value()).toBe("second");
  });

  it("rechecks currentness after waiting and does not run stale storage operations", async () => {
    const storage = storageWith("before");
    const expected = { current: "before" as string | null };
    let release!: () => void;
    const holder = navigator.locks.request("custombuild:recovery-write:delayed", { mode: "exclusive" },
      () => new Promise<void>(resolve => { release = resolve; }));
    await vi.waitFor(() => expect(release).toBeTypeOf("function"));
    let current = true;
    const queued = replaceFurnitureRecovery(storage, "delayed", expected, "obsolete", () => current);
    current = false;
    release();
    await holder;
    await expect(queued).resolves.toBe(false);
    expect(storage.getItem).not.toHaveBeenCalled();
    expect(storage.setItem).not.toHaveBeenCalled();
    expect(expected.current).toBe("before");
  });

  it("cancels queued removals without erasing another session's draft", async () => {
    const storage = storageWith("before");
    const expected = { current: "before" as string | null };
    let release!: () => void;
    const holder = navigator.locks.request("custombuild:recovery-write:discard", { mode: "exclusive" },
      () => new Promise<void>(resolve => { release = resolve; }));
    await vi.waitFor(() => expect(release).toBeTypeOf("function"));
    const controller = new AbortController();
    const queued = replaceFurnitureRecovery(storage, "discard", expected, null, () => true, controller.signal);
    const rejected = expect(queued).rejects.toMatchObject({ name: "AbortError" });
    controller.abort();
    await rejected;
    release(); await holder;
    expect(storage.removeItem).not.toHaveBeenCalled();
    expect(expected.current).toBe("before");
  });

  it("fails visibly without touching storage when Web Locks is unavailable", async () => {
    Object.defineProperty(navigator, "locks", { configurable: true, value: undefined });
    const storage = storageWith("before");
    const expected = { current: "before" as string | null };
    await expect(replaceFurnitureRecovery(storage, "unsupported", expected, null, () => true)).rejects.toThrow("säker samordning");
    expect(storage.getItem).not.toHaveBeenCalled();
    expect(storage.removeItem).not.toHaveBeenCalled();
    expect(expected.current).toBe("before");
  });

  it("does not advance the reference when storage rejects a write", async () => {
    const storage = storageWith("before");
    storage.setItem.mockImplementation(() => { throw new DOMException("Quota", "QuotaExceededError"); });
    const expected = { current: "before" as string | null };
    await expect(replaceFurnitureRecovery(storage, "quota", expected, "after", () => true)).rejects.toThrow("Quota");
    expect(expected.current).toBe("before");
    expect(storage.value()).toBe("before");
  });

  it("uses the complete storage key as the exclusive lock identity", async () => {
    const request = vi.spyOn(navigator.locks, "request");
    const storage = storageWith(null);
    await replaceFurnitureRecovery(storage, 'scope:["api","org","user","source"]', { current: null }, "draft", () => true);
    expect(request).toHaveBeenCalledWith('custombuild:recovery-write:scope:["api","org","user","source"]',
      { mode: "exclusive", signal: undefined }, expect.any(Function));
  });
});
