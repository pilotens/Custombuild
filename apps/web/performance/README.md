# Frontend performance baseline

This baseline is a regression guard for the deterministic local design path and a small set of already-rendered Chromium interactions. It is deliberately not an FPS claim, a first-load WebGL benchmark, or a network SLO.

## Measurement matrix

| Path | Fixture | Samples | Gate |
| --- | --- | ---: | --- |
| `resolveDesign` | 2,400 × 2,400 × 340 mm, 5 bays × 5 shelf rows, 5 base cabinets | 31 after warmup | median and p95 |
| `resolveDesign` | Full B1 pure-engine ceiling: 6,000 × 4,000 × 1,200 mm, 16 dividers/17 bays × 40 shelf rows, 17 base cabinets; 752 generated parts | 31 after warmup | median, p95, max/normal p95 ratio, exact part count, and terminal shelf ID |
| 5 mm numeric part edit + `resolveDesign` | normal 5 × 5 fixture | 31 after warmup | median and p95 |
| select a physical part | already-rendered normal fixture in Chromium | 7 after one warmup | p95 |
| Studio → Kontroll mode switch | already-rendered normal fixture in Chromium | 7 after one warmup | p95 |
| 5 mm divider-centre numeric edit | already-rendered normal fixture in Chromium | 7 after one warmup | p95 |

The unit sampler uses `performance.now()`, warms the code path, batches very fast operations until a sample lasts at least 12 ms, and reports per-operation median and p95. The browser journey warms the rendered UI before collecting samples and does not include initial WebGL shader compilation.

The `maximum_supported` unit fixture gates the full B1 contract ceiling through the pure `resolveDesign` engine: 6,000 mm width, 4,000 mm height, 1,200 mm depth, 16 dividers/17 bays, 40 shelves, and 17 base cabinets. This is a deterministic engine checkpoint only; it does not claim that the browser UI, WebGL rendering, interactions, export flow, or the complete end-to-end application support that ceiling.

The hot path has no apparent cubic traversal. Part generation is `O(bays × shelves)`, deterministic nesting sorts in `O(parts log parts)` and then places linearly, and the main topology/rule/BOM/operation passes are linear in part count. Base-support alignment compares at most 16 upper dividers with 16 base boundaries (`O(bays²)` under a hard 17-bay cap).

## Evidence and interpretation

Vitest writes `test-results/performance-baseline/unit.json`. Playwright attaches and writes `performance-baseline.json` in its test output directory. Both contain fixture part counts, raw samples, median/p95, and the budget version. `test-results` is ignored because timings depend on the runner; the budget and measurement method are versioned here.

The server preview has a source-controlled 500 ms debounce. Network latency is reported as non-gated because transport and backend queueing cannot be separated deterministically in the offline harness.

Run the focused checks from `apps/web`:

```text
pnpm vitest run lib/design-performance-baseline.test.ts
pnpm playwright test e2e/performance-baseline.spec.ts --project=chromium-desktop
```

Do not tighten a budget from a single workstation result. Use repeated CI evidence, keep headroom for shared runners, and investigate a regression before changing the budget.
