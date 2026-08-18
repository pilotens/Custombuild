import { beforeEach, describe, expect, it } from "vitest";
import {
  clearProductionSession,
  clearLegacyProductionStorage,
  productionSessionKey,
  readProductionSession,
  writeProductionSession,
  type ProductionSessionSnapshot,
} from "./production-session-storage";

const userA = { organization_id: "org-a", user_id: "user-a" };
const userB = { organization_id: "org-a", user_id: "user-b" };
const userAInAnotherOrganization = { organization_id: "org-b", user_id: "user-a" };

beforeEach(() => {
  window.localStorage.clear();
  window.sessionStorage.clear();
});

describe("production browser session isolation", () => {
  it("never exposes user A production metadata to user B or another organization", () => {
    const keyA = productionSessionKey(userA, "project-1", "design-1");
    writeProductionSession(window.sessionStorage, keyA, {
      version: { id: "version-a" } as ProductionSessionSnapshot["version"],
      job: { id: "job-a", status: "succeeded" } as ProductionSessionSnapshot["job"],
      release: { id: "release-a" } as unknown as ProductionSessionSnapshot["release"],
      designApproved: true,
      releaseNumber: "R1",
    });

    expect(readProductionSession(window.sessionStorage, keyA)?.job?.id).toBe("job-a");
    expect(readProductionSession(
      window.sessionStorage,
      productionSessionKey(userB, "project-1", "design-1"),
    )).toBeUndefined();
    expect(readProductionSession(
      window.sessionStorage,
      productionSessionKey(userAInAnotherOrganization, "project-1", "design-1"),
    )).toBeUndefined();
    expect(window.localStorage.length).toBe(0);
  });

  it("does not persist signed artifact URLs even if an unexpected caller supplies them", () => {
    const key = productionSessionKey(userA, "project-1", "design-1");
    const unexpected = {
      designApproved: true,
      releaseNumber: "R1",
      artifacts: [{ download_url: "https://files.example.test/package.zip?signature=secret" }],
    } as Omit<ProductionSessionSnapshot, "schemaVersion"> & {
      artifacts: Array<{ download_url: string }>;
    };

    writeProductionSession(window.sessionStorage, key, unexpected);

    const raw = window.sessionStorage.getItem(key) ?? "";
    expect(raw).not.toContain("download_url");
    expect(raw).not.toContain("signature=secret");
  });

  it("clears every production key for the principal on logout without deleting another scope", () => {
    const keyA1 = productionSessionKey(userA, "project-1", "design-1");
    const keyA2 = productionSessionKey(userA, "project-2", "design-2");
    const keyB = productionSessionKey(userB, "project-1", "design-1");
    const snapshot = { designApproved: false, releaseNumber: "R1" };
    writeProductionSession(window.sessionStorage, keyA1, snapshot);
    writeProductionSession(window.sessionStorage, keyA2, snapshot);
    writeProductionSession(window.sessionStorage, keyB, snapshot);

    clearProductionSession(window.sessionStorage, userA);

    expect(window.sessionStorage.getItem(keyA1)).toBeNull();
    expect(window.sessionStorage.getItem(keyA2)).toBeNull();
    expect(window.sessionStorage.getItem(keyB)).not.toBeNull();
  });

  it("purges legacy local-storage entries that may contain signed URLs", () => {
    window.localStorage.setItem(
      "custombuild:production:project:project-1:design-1",
      JSON.stringify({ artifacts: [{ download_url: "https://files.test/a?signature=old" }] }),
    );
    window.localStorage.setItem("custombuild:workspace:v2:anonymous:project:local-draft:draft", "keep");

    clearLegacyProductionStorage(window.localStorage);

    expect(window.localStorage.getItem("custombuild:production:project:project-1:design-1")).toBeNull();
    expect(window.localStorage.getItem("custombuild:workspace:v2:anonymous:project:local-draft:draft")).toBe("keep");
  });
});
