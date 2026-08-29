import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { performance } from "node:perf_hooks";
import { describe, expect, it } from "vitest";
import budget from "../performance/performance-budget.json";
import { editPartParametrically, resolveDesign } from "./design-engine";
import { DEFAULT_DESIGN_SPEC, type DesignSpec, type ResolvedDesign } from "./design-types";

interface TimingDistribution {
  median_ms: number;
  p95_ms: number;
  samples_ms: number[];
}

interface Distribution {
  wall_clock: TimingDistribution;
  thread_cpu: TimingDistribution;
  batch_iterations: number;
  calibration_thread_cpu_ms: number;
}

const normalSpec: DesignSpec = {
  ...DEFAULT_DESIGN_SPEC,
  furniture_type: "wall_library",
  width_mm: 2_400,
  height_mm: 2_400,
  depth_mm: 340,
  shelf_count: 5,
  divider_count: 4,
  bay_width_ratios: [],
  shelf_height_ratios: [],
  base_cabinet_height_mm: 680,
  base_cabinet_depth_mm: 340,
  base_cabinet_count: 5,
  stock_count: 24,
  back_stock_count: 8,
  reinforcement_mode: "manual",
};

const maximumSpec: DesignSpec = {
  ...DEFAULT_DESIGN_SPEC,
  furniture_type: "wall_library",
  width_mm: 6_000,
  height_mm: 4_000,
  depth_mm: 1_200,
  shelf_count: 40,
  divider_count: 16,
  bay_width_ratios: [],
  shelf_height_ratios: [],
  base_cabinet_height_mm: 600,
  base_cabinet_depth_mm: 1_200,
  base_cabinet_count: 17,
  stock_width_mm: 5_000,
  stock_height_mm: 2_500,
  stock_count: 200,
  back_stock_width_mm: 5_000,
  back_stock_height_mm: 2_500,
  back_stock_count: 20,
  machine_profile_id: "custombuild-router-5125-linuxcnc",
  reinforcement_mode: "manual",
};

function percentile(sorted: readonly number[], fraction: number): number {
  const index = Math.max(0, Math.ceil(sorted.length * fraction) - 1);
  return sorted[index] ?? 0;
}

function roundMilliseconds(value: number): number {
  return Number(value.toFixed(4));
}

function measureCpuMilliseconds(operation: () => unknown, iterations: number): number {
  const started = process.threadCpuUsage();
  for (let index = 0; index < iterations; index += 1) operation();
  const elapsed = process.threadCpuUsage(started);
  return (elapsed.user + elapsed.system) / 1_000;
}

function calibrateBatchIterations(operation: () => unknown): {
  batchIterations: number;
  calibrationCpuMs: number;
} {
  const sampling = budget.sampling;
  let batchIterations = 1;

  while (true) {
    const calibrationCpuMs = measureCpuMilliseconds(operation, batchIterations);
    if (
      calibrationCpuMs >= sampling.minimum_sample_duration_ms
      || batchIterations >= sampling.maximum_batch_iterations
    ) {
      return { batchIterations, calibrationCpuMs };
    }
    batchIterations = Math.min(
      batchIterations * 2,
      sampling.maximum_batch_iterations,
    );
  }
}

function timingDistribution(samples: number[]): TimingDistribution {
  const sorted = [...samples].sort((left, right) => left - right);
  return {
    median_ms: roundMilliseconds(percentile(sorted, 0.5)),
    p95_ms: roundMilliseconds(percentile(sorted, 0.95)),
    samples_ms: sorted.map(roundMilliseconds),
  };
}

function measureBatches(
  operation: () => unknown,
  batchIterations: number,
  sampleCount: number,
): Pick<Distribution, "wall_clock" | "thread_cpu"> {
  const wallSamples: number[] = [];
  const threadCpuSamples: number[] = [];

  for (let sample = 0; sample < sampleCount; sample += 1) {
    const threadCpuStarted = process.threadCpuUsage();
    const wallStarted = performance.now();
    for (let index = 0; index < batchIterations; index += 1) operation();
    const wallElapsed = performance.now() - wallStarted;
    const threadCpuElapsed = process.threadCpuUsage(threadCpuStarted);
    wallSamples.push(wallElapsed / batchIterations);
    threadCpuSamples.push(
      (threadCpuElapsed.user + threadCpuElapsed.system) / 1_000 / batchIterations,
    );
  }

  return {
    wall_clock: timingDistribution(wallSamples),
    thread_cpu: timingDistribution(threadCpuSamples),
  };
}

function measure(operation: () => unknown): Distribution {
  const sampling = budget.sampling;
  for (let index = 0; index < sampling.warmup_iterations; index += 1) operation();

  // CPU time prevents a scheduler pause from making a fast operation look as if one
  // iteration already satisfies the batching floor. Both clocks are retained below:
  // wall time owns the absolute latency gates, while thread CPU owns only the relative
  // algorithmic-scaling gate that must not depend on which batch the scheduler pauses.
  const { batchIterations, calibrationCpuMs } = calibrateBatchIterations(operation);

  return {
    ...measureBatches(operation, batchIterations, sampling.sample_count),
    batch_iterations: batchIterations,
    calibration_thread_cpu_ms: roundMilliseconds(calibrationCpuMs),
  };
}

function expectWithinWallClockBudget(
  distribution: Distribution,
  limits: { wall_median_ms: number; wall_p95_ms: number },
): void {
  expect(distribution.wall_clock.median_ms).toBeLessThanOrEqual(limits.wall_median_ms);
  expect(distribution.wall_clock.p95_ms).toBeLessThanOrEqual(limits.wall_p95_ms);
}

describe("frontend performance baseline", () => {
  it("records scheduler pauses in wall time but excludes them from thread CPU time", () => {
    const pauseSignal = new Int32Array(new SharedArrayBuffer(Int32Array.BYTES_PER_ELEMENT));
    const paused = measureBatches(
      () => { Atomics.wait(pauseSignal, 0, 0, 25); },
      1,
      5,
    );

    expect(paused.wall_clock.p95_ms).toBeGreaterThanOrEqual(20);
    expect(paused.thread_cpu.p95_ms).toBeLessThan(5);
    expect(paused.wall_clock.p95_ms - paused.thread_cpu.p95_ms).toBeGreaterThanOrEqual(15);
  });

  it("keeps normal, full B1 engine-ceiling and 5 mm edit resolution within the recorded budgets", () => {
    const pauseSignal = new Int32Array(new SharedArrayBuffer(Int32Array.BYTES_PER_ELEMENT));
    let injectSchedulerPause = true;
    const calibrationAfterPause = calibrateBatchIterations(() => {
      if (!injectSchedulerPause) return;
      injectSchedulerPause = false;
      Atomics.wait(pauseSignal, 0, 0, 25);
    });
    expect(calibrationAfterPause.batchIterations).toBeGreaterThan(1);

    let normalResult: ResolvedDesign | undefined;
    let maximumResult: ResolvedDesign | undefined;
    let editedResult: ResolvedDesign | undefined;
    const dividerPosition = resolveDesign(normalSpec).parts
      .find((part) => part.part_id === "divider-1")?.position_mm.x;
    expect(dividerPosition).toBeDefined();

    const normal = measure(() => { normalResult = resolveDesign(normalSpec); });
    const maximum = measure(() => { maximumResult = resolveDesign(maximumSpec); });
    const numericEdit = measure(() => {
      const edited = editPartParametrically(
        normalSpec,
        "divider-1",
        { position_x_mm: dividerPosition! + 5 },
      );
      editedResult = resolveDesign(edited.spec);
    });

    expect(normalResult).toBeDefined();
    expect(maximumResult).toBeDefined();
    expect(editedResult).toBeDefined();
    expect(normalResult!.parts.length).toBeGreaterThan(0);
    expect(maximumResult!.parts.length).toBeGreaterThan(normalResult!.parts.length);
    expect(maximumResult!.parts).toHaveLength(
      budget.fixtures.maximum_supported.expected_part_count,
    );
    expect(maximumResult!.parts.some((part) => part.part_id === "shelf-40-bay-17")).toBe(true);
    expect(editedResult!.design_hash).not.toBe(normalResult!.design_hash);

    const evidenceDirectory = join(process.cwd(), "test-results", "performance-baseline");
    mkdirSync(evidenceDirectory, { recursive: true });
    writeFileSync(join(evidenceDirectory, "unit.json"), `${JSON.stringify({
      schema_version: budget.schema_version,
      clocks: {
        wall_clock: "node:perf_hooks.performance.now",
        thread_cpu: "node:process.threadCpuUsage",
        calibration_thread_cpu: "node:process.threadCpuUsage",
      },
      fixtures: {
        normal_5x5: { part_count: normalResult!.parts.length, ...budget.fixtures.normal_5x5 },
        maximum_supported: { part_count: maximumResult!.parts.length, ...budget.fixtures.maximum_supported },
      },
      measurements: {
        local_resolve_design: { normal_5x5: normal, maximum_supported: maximum },
        local_numeric_edit_5mm: { normal_5x5: numericEdit },
      },
      maximum_to_normal_thread_cpu_p95_ratio: roundMilliseconds(
        maximum.thread_cpu.p95_ms / Math.max(normal.thread_cpu.p95_ms, 0.0001),
      ),
      server_preview: budget.server_preview,
    }, null, 2)}\n`, "utf8");

    expectWithinWallClockBudget(normal, budget.budgets.local_resolve_design.normal_5x5);
    expectWithinWallClockBudget(maximum, budget.budgets.local_resolve_design.maximum_supported);
    expectWithinWallClockBudget(numericEdit, budget.budgets.local_numeric_edit_5mm.normal_5x5);
    expect(maximum.thread_cpu.p95_ms / Math.max(normal.thread_cpu.p95_ms, 0.0001))
      .toBeLessThanOrEqual(
        budget.budgets.local_resolve_design.maximum_to_normal_thread_cpu_p95_ratio,
      );
  });
});
