import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { performance } from "node:perf_hooks";
import { describe, expect, it } from "vitest";
import budget from "../performance/performance-budget.json";
import { editPartParametrically, resolveDesign } from "./design-engine";
import { DEFAULT_DESIGN_SPEC, type DesignSpec, type ResolvedDesign } from "./design-types";

interface Distribution {
  median_ms: number;
  p95_ms: number;
  samples_ms: number[];
  batch_iterations: number;
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

function measure(operation: () => unknown): Distribution {
  const sampling = budget.sampling;
  for (let index = 0; index < sampling.warmup_iterations; index += 1) operation();

  let batchIterations = 1;
  while (batchIterations < sampling.maximum_batch_iterations) {
    const started = performance.now();
    for (let index = 0; index < batchIterations; index += 1) operation();
    if (performance.now() - started >= sampling.minimum_sample_duration_ms) break;
    batchIterations *= 2;
  }

  const samples = Array.from({ length: sampling.sample_count }, () => {
    const started = performance.now();
    for (let index = 0; index < batchIterations; index += 1) operation();
    return (performance.now() - started) / batchIterations;
  }).sort((left, right) => left - right);

  return {
    median_ms: roundMilliseconds(percentile(samples, 0.5)),
    p95_ms: roundMilliseconds(percentile(samples, 0.95)),
    samples_ms: samples.map(roundMilliseconds),
    batch_iterations: batchIterations,
  };
}

function expectWithinBudget(
  distribution: Distribution,
  limits: { median_ms: number; p95_ms: number },
): void {
  expect(distribution.median_ms).toBeLessThanOrEqual(limits.median_ms);
  expect(distribution.p95_ms).toBeLessThanOrEqual(limits.p95_ms);
}

describe("frontend performance baseline", () => {
  it("keeps normal, full B1 engine-ceiling and 5 mm edit resolution within the recorded budgets", () => {
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

    expectWithinBudget(normal, budget.budgets.local_resolve_design.normal_5x5);
    expectWithinBudget(maximum, budget.budgets.local_resolve_design.maximum_supported);
    expectWithinBudget(numericEdit, budget.budgets.local_numeric_edit_5mm.normal_5x5);
    expect(maximum.p95_ms / Math.max(normal.p95_ms, 0.0001))
      .toBeLessThanOrEqual(budget.budgets.local_resolve_design.maximum_to_normal_p95_ratio);

    const evidenceDirectory = join(process.cwd(), "test-results", "performance-baseline");
    mkdirSync(evidenceDirectory, { recursive: true });
    writeFileSync(join(evidenceDirectory, "unit.json"), `${JSON.stringify({
      schema_version: budget.schema_version,
      clock: "node:perf_hooks.performance.now",
      fixtures: {
        normal_5x5: { part_count: normalResult!.parts.length, ...budget.fixtures.normal_5x5 },
        maximum_supported: { part_count: maximumResult!.parts.length, ...budget.fixtures.maximum_supported },
      },
      measurements: {
        local_resolve_design: { normal_5x5: normal, maximum_supported: maximum },
        local_numeric_edit_5mm: { normal_5x5: numericEdit },
      },
      maximum_to_normal_p95_ratio: roundMilliseconds(maximum.p95_ms / Math.max(normal.p95_ms, 0.0001)),
      server_preview: budget.server_preview,
    }, null, 2)}\n`, "utf8");
  });
});
