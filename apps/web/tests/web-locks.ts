/** Deterministic test-only exclusive lock queue; production uses native Web Locks. */
export class TestWebLocks {
  private readonly tails = new Map<string, Promise<void>>();

  async request<T>(
    name: string,
    options: { mode?: LockMode; signal?: AbortSignal },
    callback: (lock: Lock) => T | PromiseLike<T>,
  ): Promise<T> {
    const previous = this.tails.get(name) ?? Promise.resolve();
    let release!: () => void;
    const held = new Promise<void>(resolve => { release = resolve; });
    const tail = previous.then(() => held);
    this.tails.set(name, tail);
    let abort: (() => void) | undefined;
    try {
      await new Promise<void>((resolve, reject) => {
        abort = () => reject(new DOMException("The lock request was aborted.", "AbortError"));
        if (options.signal?.aborted) { abort(); return; }
        options.signal?.addEventListener("abort", abort, { once: true });
        void previous.then(resolve, reject);
      });
      if (options.signal?.aborted) throw new DOMException("The lock request was aborted.", "AbortError");
      return await callback({ name, mode: "exclusive" } as Lock);
    } finally {
      if (abort) options.signal?.removeEventListener("abort", abort);
      release();
      void tail.then(() => {
        if (this.tails.get(name) === tail) this.tails.delete(name);
      });
    }
  }
}
