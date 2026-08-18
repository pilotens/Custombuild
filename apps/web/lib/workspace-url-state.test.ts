import { describe, expect, it } from "vitest";
import {
  parseWorkspaceUrl,
  selectWorkspaceMode,
  selectWorkspaceProject,
  serializeWorkspaceUrl,
} from "./workspace-url-state";

describe("workspace URL state", () => {
  it("parses only a bounded project id and one canonical product mode", () => {
    expect(parseWorkspaceUrl("?project=project-1&mode=check")).toEqual({
      projectId: "project-1",
      mode: "check",
      projectParamPresent: true,
      modeParamPresent: true,
    });

    expect(parseWorkspaceUrl("?project=project%2F1&mode=guided")).toEqual({
      projectParamPresent: true,
      modeParamPresent: true,
    });
    expect(parseWorkspaceUrl("?project=one&project=two&mode=studio&mode=build")).toEqual({
      projectParamPresent: true,
      modeParamPresent: true,
    });
  });

  it("uses authorized URL project before remembered and default projects", () => {
    const projects = [{ id: "first" }, { id: "remembered" }, { id: "linked" }];
    expect(selectWorkspaceProject(projects, "linked", "remembered")?.id).toBe("linked");
    expect(selectWorkspaceProject(projects, "forbidden", "remembered")?.id).toBe("remembered");
    expect(selectWorkspaceProject(projects, "forbidden", "missing")?.id).toBe("first");
    expect(selectWorkspaceProject([], "linked", "remembered")).toBeUndefined();
  });

  it("keeps a valid local mode when an explicit URL mode is malformed", () => {
    const malformed = parseWorkspaceUrl("?mode=verification");
    expect(malformed.modeParamPresent).toBe(true);
    expect(selectWorkspaceMode(malformed, "build")).toBe("build");
    expect(selectWorkspaceMode(parseWorkspaceUrl("?mode=studio"), "build")).toBe("studio");
  });

  it("canonicalizes owned fields while preserving unrelated params and hash", () => {
    expect(serializeWorkspaceUrl(
      "https://custombuild.test/workspace?invite=abc&mode=guided&project=old#model",
      { projectId: "project-2", mode: "studio" },
    )).toBe("/workspace?invite=abc&project=project-2&mode=studio#model");

    expect(serializeWorkspaceUrl(
      "https://custombuild.test/?project=server-project&mode=build&return=1",
      { mode: "explore" },
    )).toBe("/?return=1&mode=explore");
  });
});
