import { afterEach, describe, expect, it, vi } from "vitest";
import {
  ApiError,
  CustombuildApiClient,
  normalizePreviewResponse,
  toPreviewRequest,
} from "./api-client";
import { DEFAULT_DESIGN_SPEC, type DesignSpec } from "./design-types";

describe("API preview normalization", () => {
  it("merges a tolerant server response with deterministic local output", () => {
    const result = normalizePreviewResponse(
      {
        design_hash: "a".repeat(64),
        status: "warning",
        rule_evaluations: [
          {
            rule_id: "SERVER-001",
            rule_version: "2.0.0",
            status: "WARNING",
            title: "Serverregel",
            summary: "Verifierad av servern",
            calculation: "1 + 1 = 2",
            assumptions: ["Test"],
            affected_part_ids: ["side-left"],
          },
        ],
      },
      DEFAULT_DESIGN_SPEC,
    );

    expect(result.source).toBe("server-preview");
    expect(result.design_hash).toBe("a".repeat(64));
    expect(result.parts.length).toBeGreaterThan(0);
    expect(result.rule_evaluations[0]?.rule_id).toBe("SERVER-001");
    expect(result.status).toBe("WARNING");
  });

  it("rejects a non-object server payload", () => {
    expect(() => normalizePreviewResponse(null, DEFAULT_DESIGN_SPEC)).toThrow(/okänt format/i);
  });

  it("sends only the strict server contract and never client-borne safety evidence", () => {
    const request = toPreviewRequest({
      ...DEFAULT_DESIGN_SPEC,
      fixed_shelves: false,
      joint_system: "dado",
      wall_anchor_verified: true,
    });

    expect(request.shelf_mount).toBe("adjustable");
    expect(request.joint_system).toBe("dado");
    expect(request.wall_anchor_verified).toBe(false);
    expect(request).not.toHaveProperty("schema_version");
    expect(request).not.toHaveProperty("machine_profile_id");
  });

  it("rejects an unsupported joint instead of silently normalizing it to DADO", () => {
    const unsupported = {
      ...DEFAULT_DESIGN_SPEC,
      joint_system: "confirmat",
    } as unknown as DesignSpec;

    expect(() => toPreviewRequest(unsupported)).toThrow(/confirmat.*stöds inte/i);
  });

  it("maps canonical server rules, autofix spec and diff without mixing local rules", () => {
    const result = normalizePreviewResponse(
      {
        design_hash: "b".repeat(64),
        status: "PASS",
        spec: {
          material: { material_id: "birch-plywood", name: "Björkplywood 18 mm" },
          parameters: {
            width_um: 1_200_000,
            height_um: 2_100_000,
            depth_um: 320_000,
            nominal_thickness_um: 18_000,
            actual_thickness_um: 17_800,
            shelf_count: 5,
            shelf_mount: "fixed",
            shelf_load_n: 314,
            back_panel: "inset_groove",
            plinth_height_um: 80_000,
            vertical_divider_count: 1,
            reinforcement_mode: "auto",
            joint_system: "dado",
            edge_band_thickness_um: 1_000,
          },
        },
        rule_evaluations: [
          {
            rule_id: "CB-DEFLECTION-001",
            rule_version: "1.2.0",
            status: "PASS",
            title: "Hyllnedböjning",
            applies_to_part_ids: ["server-shelf-id"],
            assumptions: ["Serverantagande"],
            trace: [{ expression: "5wL^4/(384EI)", result: "1.2", unit: "mm" }],
            calculated_value: 1200,
            allowed_value: 3000,
            unit: "µm",
            safety_margin_permille: 600,
            suggested_actions: [
              {
                action_type: "add_vertical_divider",
                description: "Lägg till en vertikal avdelare",
                changes: [
                  { path: "parameters.vertical_divider_count", before: 0, after: 1 },
                ],
              },
            ],
          },
        ],
        change_diff: [
          {
            explanation: "Avdelaren minskar fri spännvidd.",
            changes: [
              { path: "parameters.vertical_divider_count", before: 0, after: 1 },
            ],
          },
        ],
      },
      DEFAULT_DESIGN_SPEC,
    );

    expect(result.spec.divider_count).toBe(1);
    expect(result.spec.reinforcement_mode).toBe("auto");
    expect(result.rule_evaluations).toHaveLength(1);
    expect(result.rule_evaluations[0]?.calculation).toContain("5wL^4");
    expect(result.rule_evaluations[0]?.affected_part_ids).toEqual(["server-shelf-id"]);
    expect(result.rule_evaluations[0]?.suggestion).toMatchObject({
      action: "set_divider_count",
      value: 1,
    });
    expect(result.change_diff).toEqual([
      expect.objectContaining({ field: "divider_count", before: 0, after: 1 }),
    ]);
  });

  it("rejects an unsupported joint returned by the server", () => {
    expect(() => normalizePreviewResponse(
      {
        design_hash: "c".repeat(64),
        status: "PASS",
        spec: {
          parameters: { joint_system: "dowel" },
        },
      },
      DEFAULT_DESIGN_SPEC,
    )).toThrow(/dowel.*inte stöds/i);
  });
});

afterEach(() => vi.restoreAllMocks());

describe("production API contract", () => {
  it("accepts only complete, signed web artifact links from the typed endpoint", async () => {
    const payload = [{
      id: "artifact-1",
      kind: "production_bundle",
      sha256: "a".repeat(64),
      size_bytes: 2_048,
      content_type: "application/zip",
      download_url: "https://artifacts.example.test/bundle.zip?signature=fresh",
      download_path: "/v1/artifacts/artifact-1/download?signature=signed",
    }];
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(
      JSON.stringify(payload),
      { status: 200, headers: { "Content-Type": "application/json" } },
    ));
    const api = new CustombuildApiClient("https://api.example.test/", "tenant-token");

    await expect(api.listArtifacts("job /1")).resolves.toEqual(payload);
    expect(fetchMock).toHaveBeenCalledWith(
      "https://api.example.test/v1/jobs/job%20%2F1/artifacts",
      expect.objectContaining({
        method: "GET",
        headers: expect.objectContaining({ Authorization: "Bearer tenant-token" }),
      }),
    );

    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify([{
      ...payload[0],
      download_url: "javascript:alert(1)",
    }]), { status: 200, headers: { "Content-Type": "application/json" } }));
    await expect(api.listArtifacts("job-2")).rejects.toThrow(ApiError);

    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify([{
      ...payload[0],
      sha256: "not-a-sha",
    }]), { status: 200, headers: { "Content-Type": "application/json" } }));
    await expect(api.listArtifacts("job-3")).rejects.toThrow(/ogiltig artefaktlänk/i);
  });

  it("binds release confirmation and rejects incomplete release evidence", async () => {
    const release = {
      release_id: "release-1",
      release_number: "R7",
      status: "released",
      manifest_sha256: "f".repeat(64),
      machine_use: "validation_only",
    };
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(
      JSON.stringify(release),
      { status: 200, headers: { "Content-Type": "application/json" } },
    ));
    const api = new CustombuildApiClient("https://api.example.test", "tenant-token");

    await expect(api.releaseVersion("project/1", 3, "R7")).resolves.toEqual(release);
    expect(fetchMock).toHaveBeenCalledWith(
      "https://api.example.test/v1/projects/project%2F1/versions/3/release",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ release_number: "R7", confirmation: "RELEASE" }),
      }),
    );

    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify({
      ...release,
      machine_use: "production",
    }), { status: 200, headers: { "Content-Type": "application/json" } }));
    await expect(api.releaseVersion("project-1", 3, "R8")).rejects.toThrow(
      /ofullständigt frisläppningsbevis/i,
    );
  });
});
