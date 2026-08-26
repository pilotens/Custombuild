import { describe, expect, it } from "vitest";
import budget from "../performance/full-ceiling-browser-budget.json";

describe("full-ceiling browser evidence budget", () => {
  it("keeps CI collection headroom separate from product performance gates", () => {
    expect(budget.sampling).toEqual({
      warm_frame_count: 120,
      interaction_sample_count: 7,
      frame_collection_timeout_ms: 90_000,
    });
    expect(budget.budgets).toEqual({
      cold_webgl_ready_ms: 20_000,
      warm_frame_interval_p95_ms: 100,
      warm_frame_interval_max_ms: 500,
      selection_p95_ms: 750,
      view_switch_p95_ms: 1_500,
      transparency_toggle_p95_ms: 1_500,
      warm_long_task_max_ms: 1_000,
    });
  });
});
