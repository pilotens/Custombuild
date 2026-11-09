import { readFile, writeFile } from "node:fs/promises";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { expect, test, type Page, type TestInfo } from "@playwright/test";
import { newFurnitureWorkspace, type FurnitureFamily } from "../lib/furniture-workspace";
import type { FurnitureModulePlan } from "../lib/furniture-module-plan";
import { provisionLiveProject, selectProjectBeforeNavigation } from "./live-helpers";

async function attachView(page: Page, info: TestInfo, name: string) {
  const path = info.outputPath(`${name}.png`);
  await page.screenshot({ path, fullPage: true });
  await info.attach(name, { path, contentType: "image/png" });
}

test.describe("möbelfamiljer med verklig API, databas, kö och CAD-worker", () => {
  test.skip(process.env.PLAYWRIGHT_REAL_API !== "1", "Requires the complete Compose stack.");
  test.afterEach(async ({ page }, info) => {
    if (info.status !== info.expectedStatus && await page.locator("main").count()) {
      console.log("Furniture workspace at failure:", await page.locator("main").innerText());
    }
  });
  test("stolsbenets delblad, monteringsförband och reversibla borttagning", async ({page, request}, info) => {
    const project = await provisionLiveProject(request, info, "inspect-chair-leg");
    await selectProjectBeforeNavigation(page, project);
    await page.goto("/furniture?family=chair");
    await page.getByRole("combobox", {name:"Granska en del"}).selectOption({label:"leg-front-left"});
    const sheet = page.getByRole("region",{name:"Delritning och förband"});
    await expect(sheet.getByRole("heading",{name:"leg-front-left"})).toBeVisible();
    await sheet.getByRole("button",{name:"Visa utan delen"}).click();
    await expect(sheet.getByRole("status")).toContainText("4 modellerade förband bryts");
    await expect(sheet.getByRole("status")).toContainText("3 ramben");
    await expect(page.getByRole("button",{name:"Spara revision"})).toBeDisabled();
    await sheet.getByRole("button",{name:"Isolera delen"}).click();
    const download = page.waitForEvent("download");
    await sheet.getByRole("button",{name:"Hämta utskrivbart delblad med alla sidor"}).click();
    expect((await download).suggestedFilename()).toMatch(/leg-front-left.*granskning\.html$/);
    await sheet.getByRole("button",{name:"Hela möbeln"}).click();
    await expect(page.getByRole("button",{name:"Spara revision"})).toBeEnabled();
  });

  test("sex underskåpsluckor: ändrad fackindelning och återlästa beslag i två revisioner", async ({page, request}, info) => {
    test.setTimeout(900_000);
    const project = await provisionLiveProject(request, info, "cabinet-doors");
    await selectProjectBeforeNavigation(page, project);
    await page.goto("/furniture?family=shelving");
    await page.getByRole("button", {name: "Öppna valnötsbokhylla med sex gångjärnsluckor"}).click();
    await expect(page.getByText(/Arbetsfilen har kontrollerats/)).toBeVisible();
    await page.getByLabel("Projektnamn", {exact: true}).fill(project.project.name + "-luckor");
    const canvas = page.getByTestId("furniture-viewer").locator("canvas");
    await expect(canvas).toHaveAttribute("data-custombuild-model-root", /.+/);
    const firstView = await canvas.screenshot();
    for (const [revision, bays] of [[1, 6], [2, 7]] as const) {
      if (revision === 2) {
        await page.getByLabel("Avdelare", {exact:true}).fill("6");
        await expect(page.getByLabel("Granska en del").locator("option").filter({hasText:"cabinet-front-6"})).toHaveCount(1);
        await expect.poll(async () => !(await canvas.screenshot()).equals(firstView)).toBe(true);
        await expect(page.getByRole("link", {name:/Hämta STEP, GLB, DXF/})).toHaveCount(0);
      }
      await page.getByRole("button", {name:"Spara revision"}).click();
      await expect(page.getByText(`Revision ${revision} är sparad.`)).toBeVisible();
      await page.getByRole("button", {name:"Skapa granskningspaket"}).click();
      const link = page.getByRole("link", {name:/Hämta STEP, GLB, DXF/});
      await expect(link).toBeVisible({timeout:360_000});
      const event = page.waitForEvent("download");
      await link.click();
      const download = await event;
      const path = info.outputPath(`Custombuild-Valnot-Gangjarn-Rev${revision}.zip`);
      await download.saveAs(path);
      const report = JSON.parse(execFileSync("python3", ["-c", [
        "import json,sys,zipfile",
        "with zipfile.ZipFile(sys.argv[1]) as z:",
        " m=json.loads(z.read('manifest.json'))",
        " r=json.loads(z.read('inspection/door-hardware.json'))",
        " assert r['design_hash']==m['design_hash'] and not m['production_qualified']",
        " print(json.dumps(r))",
      ].join("\n"), path], {encoding:"utf8"}));
      expect(report).toMatchObject({revision, doors:bays, hinges:bays*2, plates:bays*2,
        screws:bays*12, door_and_plate_bores:bays*15});
    }
    await attachView(page, info, "underskap-valnot-sju-luckor");
  });

  test("kundmått 4340 × 2540 × 280: arbetsfil, återöppning och spårbart CAD-underlag", async ({ page, request }, info) => {
    test.setTimeout(900_000);
    const provisioned = await provisionLiveProject(request, info, "customer-bookcase");
    await selectProjectBeforeNavigation(page, provisioned);
    await page.goto("/");
    await page.getByRole("link", { name: "Bokhylla med verkstadsunderlag" }).click();
    await expect(page.getByRole("button", { name: "Spara revision" })).toBeEnabled();
    await page.getByLabel("Läs arbetsfil (JSON)").setInputFiles(fileURLToPath(new URL(
      "../../../examples/furniture/bookcase-4340x2540x280.json", import.meta.url)));
    await expect(page.getByText(/Arbetsfilen har kontrollerats/)).toBeVisible();
    await expect(page.getByLabel("Bredd (mm)", { exact: true })).toHaveValue("4300");
    await expect(page.getByLabel("Höjd (mm)", { exact: true })).toHaveValue("2540");
    await expect(page.getByLabel("Djup (mm)", { exact: true })).toHaveValue("260");
    await page.getByText("Fack, hyllhöjder och rygg", { exact: true }).click();
    await page.getByRole("checkbox", { name: /Prova mekanisk låsning/ }).check();
    const viewer = page.getByTestId("furniture-viewer");
    await expect(viewer).toHaveAttribute("data-renderer", "webgl");
    const canvas = viewer.locator("canvas");
    await expect(canvas).toHaveAttribute("data-custombuild-model-root", /.+/);
    const originalView = await canvas.screenshot();
    await page.getByLabel("Avdelare", {exact: true}).fill("5");
    await page.getByLabel("Hyllrader", {exact: true}).fill("7");
    await page.getByLabel("Hur anges hyllasten?").selectOption("per_metre");
    await page.getByLabel("Last per meter hyllrad (kg/m)").fill("30");
    await page.getByLabel("Last per meter hyllrad (kg/m)").blur();
    await expect(page.getByLabel("Granska en del").locator("option")).toHaveCount(58);
    await expect.poll(async () => !(await canvas.screenshot()).equals(originalView)).toBe(true);
    await page.getByLabel("Projektnamn", { exact: true }).fill(`${provisioned.project.name}-import`);
    const created = page.waitForResponse(r => r.request().method() === "POST" && new URL(r.url()).pathname === "/v1/furniture/projects");
    await page.getByRole("button", { name: "Spara revision" }).click();
    const response = await created;
    expect(response.status()).toBe(201);
    const { project } = await response.json();
    await expect(page.getByText("Revision 1 är sparad.")).toBeVisible();
    await page.reload();
    const projects = page.getByRole("combobox", { name: "Öppna möbelprojekt" });
    await expect(projects.locator(`option[value="${project.id}"]`)).toHaveCount(1);
    await projects.selectOption(project.id);
    await page.getByText("Kundens yttermått och listutrymme", { exact: true }).click();
    await expect(page.getByLabel("Kundlängd inklusive reserverat utrymme (mm)")).toHaveValue("4340");
    await expect(page.getByLabel("Längden inkluderar list", { exact: true })).toBeChecked();
    await expect(page.getByLabel("Listhöjd (mm)", { exact: true })).toHaveValue("90");
    await expect(page.getByLabel("Listbredd/utstick (mm)", { exact: true })).toHaveValue("20");
    await expect(page.getByLabel("Listens funktion", { exact: true })).toHaveValue("existing_room_trim");
    await expect(page.getByLabel("Vänster · reserverat (mm)")).toHaveValue("20");
    await expect(page.getByRole("button", { name: "Förbered tillverkning" })).toBeDisabled();
    await page.getByText("Råformat att stämma av med verkstaden", { exact: true }).click();
    await expect(page.getByRole("table", {name: "Reserverade och uppmätta väggavstånd"})).toContainText("Ej uppmätt");
    await expect(page.getByRole("table")).toHaveCount(3);
    await page.getByRole("button", { name: "Skapa granskningspaket" }).click();
    const link = page.getByRole("link", { name: /Hämta STEP, GLB, DXF/ });
    await expect(link).toBeVisible({ timeout: 360_000 });
    const downloadEvent = page.waitForEvent("download");
    await link.click();
    const download = await downloadEvent;
    expect(await download.failure()).toBeNull();
    await download.saveAs(info.outputPath("Custombuild-Bokhylla-Valnot-Konstruktionsprov.zip"));
    const exported = JSON.parse(execFileSync("python3", ["-c", [
      "import csv,hashlib,io,json,sys,zipfile",
      "with zipfile.ZipFile(sys.argv[1]) as z:",
      " manifest=json.loads(z.read('manifest.json'))",
      " for entry in manifest['files']:",
      "  assert hashlib.sha256(z.read(entry['path'])).hexdigest()==entry['sha256']",
      " assert z.read('design/model.step').startswith(b'ISO-10303-21;')",
      " assert manifest['physical_cutting_authorized'] is False",
      " model=json.loads(z.read('design/resolved.json'))",
      " ops=json.loads(z.read('manufacturing/manufacturing-intent.json'))",
      " graph=json.loads(z.read('assembly/graph.json'))",
      " assert manifest['design_hash']==model['design_hash']==graph['design_hash']==ops['document_identity']['design_hash']",
      " assert json.loads(z.read('design/workspace.json'))['manufacturing'] is None",
      " assert all(p['material_id']=='solid-walnut' for p in model['parts'] if p['role']!='back')",
      " readback=json.loads(z.read('inspection/geometry-readback.json'))",
      " assert readback['design_hash']==manifest['design_hash'] and readback['revision']==1",
      " assert [readback[k] for k in ('timber_solids','pilot_holes','dxf_faces','grooves','stopped_grooves','assembly_steps')]==[57,196,114,122,94,56]",
      " assert len(graph['graph']['steps'])==56",
      " trial=json.loads(z.read('inspection/kvalificeringsprov.json'))",
      " assert trial['design_hash']==manifest['design_hash'] and trial['type_test_results'] is None",
      " assert trial['order_requirements']['joint_shear_n']==10784 and trial['order_requirements']['assembly_steps']==list(range(1,57))",
      " pairs=list(csv.DictReader(io.StringIO(z.read('inspection/fogprov.csv').decode('utf-8-sig'))))",
      " assert len(pairs)==196 and len({r['joint_id'] for r in pairs})==98",
      " assert all(r['design_hash']==manifest['design_hash'] and r['drawing'] in z.namelist() and not r['inspection_result'] for r in pairs)",
      " fittings=json.loads(z.read('assembly/trial-angles.json'))",
      " assert len(fittings['angles'])==98 and fittings['revision']==manifest['revision']",
      " assert len(fittings['straight_access_um_by_pilot'])==196",
      " assert z.read('design/with-mounted-angles.step').startswith(b'ISO-10303-21;')",
      " rows=list(csv.DictReader(io.StringIO(z.read('inspection/first-article-checks.csv').decode('utf-8-sig'))))",
      " assert rows and all(not r['measured'] and not r['result'] for r in rows)",
      " print(z.read('manufacturing/workshop-handoff.json').decode())",
    ].join("\n"), (await download.path())!], { encoding: "utf8" }));
    expect(exported.dimensions.installation.width_um).toBe(4_340_000);
    expect(exported.dimensions.installation.height_um).toBe(2_540_000);
    expect(exported.dimensions.installation.depth_um).toBe(280_000);
    expect(exported.dimensions.installation.trim_profile).toEqual({
      height_um: 90_000, width_um: 20_000, use: "existing_room_trim", walls: ["left", "right", "rear"],
    });
    expect(exported.dimensions.state).toBe("requires_resolution");
    expect(exported.dimensions.standing_assembly.state).toBe("blocked");
    expect(exported.dimensions.standing_assembly.minimum_height_from_base_um).toBe(2_558_000);
    expect(exported.dimensions.standing_assembly.walls.map((wall: {margin_um: number}) => wall.margin_um))
      .toEqual([-6_000, -6_000, 0]);
    const originalDenied = page.waitForResponse(r => r.request().method() === "POST" && r.url().endsWith("/exports"));
    await page.getByRole("button", {name: "Beställ verkstadsritningar"}).click();
    const blocked = await originalDenied;
    expect(blocked.status()).toBe(409);
    expect((await blocked.json()).detail).toMatch(/26 mm/);
    await attachView(page, info, "customer-bookcase-dimensions");
    const width = page.getByLabel("Bredd (mm)", { exact: true });
    await width.fill("6000.001");
    await width.blur();
    await expect(link).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Spara revision" })).toBeDisabled();
    await width.fill("4300");
    await width.blur();
    await page.getByRole("button", {name: "Använd platsalternativ"}).click();
    await expect(width).toHaveValue("4280");
    await expect(page.getByLabel("Höjd (mm)", {exact: true})).toHaveValue("2500");
    const leftMeasurement = page.getByLabel("Vänster vägg · uppmätt avstånd (mm)");
    await leftMeasurement.fill("25");
    await expect(page.getByText(/Uppmätt avstånd från stommens slutläge till vänster vägg är 25 mm/).first()).toBeVisible();
    await expect(width).toHaveValue("4280");
    // Remove the negative probe. The customer supplied no wall survey for this order.
    await leftMeasurement.fill("");
    await expect(page.getByLabel("Hyllrader", { exact: true })).toHaveValue("7");
    await page.getByRole("button", { name: "Spara revision" }).click();
    await expect(page.getByText("Revision 2 är sparad.")).toBeVisible();
    await page.getByRole("button", { name: "Skapa granskningspaket" }).click();
    const second = page.getByRole("link", { name: /Hämta STEP.*revision 2/ });
    await expect(second).toBeVisible({ timeout: 360_000 });
    const secondEvent = page.waitForEvent("download");
    await second.click();
    const regenerated = await secondEvent;
    expect(await regenerated.failure()).toBeNull();
    await regenerated.saveAs(info.outputPath("Custombuild-Bokhylla-Valnot-Platsalternativ.zip"));
    execFileSync("python3", ["-c", [
      "import json,sys,zipfile",
      "with zipfile.ZipFile(sys.argv[1]) as before, zipfile.ZipFile(sys.argv[2]) as after:",
      " old,new=[json.loads(z.read('design/resolved.json')) for z in (before,after)]",
      " site=json.loads(after.read('assembly/required-space.json'))['standing_site']",
      " assert site['state']=='requires_measurement' and site['minimum_ceiling_height_um']==2518000",
      " assert site['measured_ceiling_height_um'] is None and site['measured_front_space_um'] is None",
      " assert [w['margin_um'] for w in site['walls']]==[4000,4000,0]",
      " assert [w['measured_distance_um'] for w in site['walls']]==[None,None,None]",
      " assert [w['measured_margin_um'] for w in site['walls']]==[None,None,None]",
      " assert new['spec']['revision']==2 and new['design_hash']!=old['design_hash']",
      " readback=json.loads(after.read('inspection/geometry-readback.json'))",
      " assert readback['design_hash']==new['design_hash'] and readback['revision']==2",
      " assert len(new['parts'])==readback['timber_solids']==57 and readback['pilot_holes']==196",
      " assert old['spec']['intent']['width_um']==4300000 and new['spec']['intent']['width_um']==4280000",
    ].join("\n"), (await download.path())!, (await regenerated.path())!]);
    const orderUrl = `${process.env.PLAYWRIGHT_API_URL}/v1/furniture/projects/${project.id}`;
    const headers = {Authorization: `Bearer ${process.env.PLAYWRIGHT_DEMO_TOKEN || "demo-nordic-owner"}`};
    const secondDraft = await (await request.get(orderUrl+"/draft", {headers})).json();
    expect(secondDraft.revision).toBe(2);
    const secondIdentity = {expected_revision: 2, expected_design_hash: secondDraft.preview.design.design_hash};
    const denied = await request.post(orderUrl+"/exports", {
      headers, data: {...secondIdentity, purpose: "workshop_drawings"},
    });
    expect(denied.status()).toBe(409);
    expect((await denied.json()).detail).toMatch(/takhöjd|framför/i);
    // Removing a trim profile preserves the three walls where it was measured.
    await page.getByRole("checkbox", {name: "Ange listprofil"}).uncheck();
    await page.getByRole("checkbox", {name: "Längden inkluderar list"}).uncheck();
    await page.getByLabel("Vänster · reserverat (mm)").fill("0");
    await page.getByRole("button", {name: "Räkna om stommen från kundmåtten"}).click();
    await expect(page.getByLabel("Vänster · vägg utan list")).toBeChecked();
    await expect(page.getByText(/vid vänster vägg behöver 6 mm nominellt/).first()).toBeVisible();
    await page.getByLabel("Övriga sidor är kontrollerade på plats").uncheck();
    await expect(page.getByText(/Kontrollera väggläget vid vänster vägg/).first()).toBeVisible();
    await page.getByLabel("Ange separata kundmått").uncheck();
    await expect(page.getByText(/Monteringsplatsen är inte angiven/).first()).toBeVisible();
    await expect(second).toHaveCount(0);
    await page.setViewportSize({ width: 390, height: 844 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await attachView(page, info, "customer-bookcase-mobile");

    const exportOrder = async (revision: number, hash: string) => {
      const response = await request.post(orderUrl+"/exports", {
        headers, data: {expected_revision:revision, expected_design_hash:hash},
      });
      expect(response.status()).toBe(202);
      return (await response.json()).job_id as string;
    };
    const saveRows = async (revision: number, count: number) => {
      const source = structuredClone(secondDraft.workspace);
      source.design.intent.shelf_count = count;
      const response = await request.put(orderUrl+"/draft", {headers,
        data:{expected_revision:revision, workspace:source}});
      expect(response.status()).toBe(200);
      return (await response.json()).preview.design.design_hash as string;
    };
    const repeat = await exportOrder(2, secondIdentity.expected_design_hash);
    const repeated = await request.get(`${orderUrl}/exports/${repeat}/download`, {headers});
    expect(repeated.status()).toBe(200);
    expect((await repeated.body()).equals(await readFile((await regenerated.path())!))).toBe(true);
    const staleHash = await saveRows(2, 5);
    const interrupted = await exportOrder(3, staleHash);
    const fourthHash = await saveRows(3, 2);
    const stopped = await request.get(`${orderUrl}/exports/${interrupted}`, {headers});
    expect((await stopped.json()).state).toBe("cancelled");
    expect((await request.get(`${orderUrl}/exports/${interrupted}/download`,{headers})).status()).toBe(404);
    const oldOrder = await request.post(orderUrl+"/exports", {headers,
      data:{expected_revision:3, expected_design_hash:staleHash}});
    expect(oldOrder.status()).toBe(409);
    const fourthJob = await exportOrder(4, fourthHash);
    expect(await exportOrder(4, fourthHash)).toBe(fourthJob);
    const fourthUrl = `${orderUrl}/exports/${fourthJob}`;
    await expect.poll(async () => (await (await request.get(fourthUrl,{headers})).json()).state,
      {timeout: 300_000, intervals:[2_000]}).toBe("succeeded");
    const final = await request.get(fourthUrl+"/download",{headers});
    expect(final.status()).toBe(200);
    const finalPath = info.outputPath("revision-after-cancellation.zip");
    await writeFile(finalPath, await final.body());
    execFileSync("python3", ["-c", [
      "import json,sys,zipfile",
      "with zipfile.ZipFile(sys.argv[1]) as z:",
      " m=json.loads(z.read('manifest.json')); r=json.loads(z.read('inspection/geometry-readback.json'))",
      " assert m['revision']==r['revision']==4 and r['design_hash']==sys.argv[2]",
      " assert r['timber_solids']==27 and r['dxf_faces']==54",
    ].join("\n"), finalPath, fourthHash]);

  });
  test("stommoduler: granska kundplan, hämta exakta arbetsfiler och bevara källrevision", async ({ page, request }, info) => {
    test.setTimeout(120_000);
    const provisioned = await provisionLiveProject(request, info, "customer-modules");
    const base = process.env.PLAYWRIGHT_API_URL!.replace(/\/$/, "");
    const headers = { Authorization: `Bearer ${process.env.PLAYWRIGHT_DEMO_TOKEN || "demo-nordic-owner"}` };
    const path = `${base}/v1/furniture/projects/${provisioned.project.id}`;
    const workspace = JSON.parse(await readFile(new URL(
      "../../../tests/fixtures/furniture/bookcase-unresolved.json", import.meta.url), "utf8"));
    const saved = await request.put(`${path}/draft`, { headers, data: { expected_revision: 0, workspace } });
    expect(saved.status(), await saved.text()).toBe(200);
    const source = await saved.json();
    await page.goto("/furniture");
    await page.getByRole("combobox", { name: "Öppna möbelprojekt" }).selectOption(provisioned.project.id);
    await expect(page.getByLabel("Bredd (mm)", { exact: true })).toHaveValue("4340");
    const panel = page.getByRole("region", { name: "Planera stommoduler" });
    await panel.getByLabel("Kolumner, från vänster").fill("5");
    await panel.getByLabel("Rader, nerifrån och upp").fill("2");
    await panel.getByLabel("Hyllplan per modul, rad 1 (nederst)").fill("2");
    await panel.getByLabel("Hyllplan per modul, rad 2").fill("2");
    await expect(panel.getByRole("button", { name: "Beräkna modulplan", exact: true })).toBeDisabled();
    await panel.getByLabel("Mellanrum mellan moduler (mm)").fill("0");
    const responseEvent = page.waitForResponse(response =>
      response.request().method() === "POST" && new URL(response.url()).pathname === "/v1/furniture/module-plan");
    await panel.getByRole("button", { name: "Beräkna modulplan", exact: true }).click();
    const response = await responseEvent;
    expect(response.status()).toBe(200);
    const plan = await response.json() as FurnitureModulePlan;
    expect(plan.modules).toHaveLength(10);
    expect(plan.source.workspace).toEqual(source.workspace);
    expect(plan.physical_cutting_authorized).toBe(false);
    await expect(panel.getByRole("button", { name: "Hämta hela modulplanen" })).toBeEnabled();
    await expect(panel.getByRole("button", { name: /Hämta arbetsfil för rad/ })).toHaveCount(10);
    await expect(panel.getByRole("img", { name: /Modulindelning sedd framifrån/ })).toBeVisible();
    const planEvent = page.waitForEvent("download");
    await panel.getByRole("button", { name: "Hämta hela modulplanen" }).click();
    const planDownload = await planEvent;
    expect(await planDownload.failure()).toBeNull();
    expect(JSON.parse(await readFile((await planDownload.path())!, "utf8"))).toEqual(plan);
    const moduleEvent = page.waitForEvent("download");
    await panel.getByRole("button", { name: "Hämta arbetsfil för rad 1, kolumn 1", exact: true }).click();
    const moduleDownload = await moduleEvent;
    expect(await moduleDownload.failure()).toBeNull();
    const child = JSON.parse(await readFile((await moduleDownload.path())!, "utf8"));
    expect(child).toEqual(plan.modules[0]!.workspace);
    expect(child.design.installation).toEqual(source.workspace.design.installation);
    expect(child.design.material).toEqual(source.workspace.design.material);
    expect(child.design.intent.width_um).toBe(868_000);
    expect(child.design.intent.height_um).toBe(1_270_000);
    expect(await (await request.get(`${path}/draft`, { headers })).json()).toEqual(source);
    expect((await (await request.get(`${path}/history`, { headers })).json()).items).toHaveLength(1);
    for (const [name, width, height] of [["module-plan-desktop", 1440, 960], ["module-plan-mobile", 390, 844]] as const) {
      await page.setViewportSize({ width, height });
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
      const screenshot = info.outputPath(`${name}.png`);
      await panel.screenshot({ path: screenshot });
      await info.attach(name, { path: screenshot, contentType: "image/png" });
    }
    await panel.getByLabel("Mellanrum mellan moduler (mm)").fill("1");
    await expect(panel.getByRole("button", { name: "Hämta hela modulplanen" })).toHaveCount(0);
    await page.getByLabel("Läs arbetsfil (JSON)").setInputFiles((await moduleDownload.path())!);
    await expect(page.getByText(/Arbetsfilen har kontrollerats/)).toBeVisible();
    await expect(page.getByLabel("Bredd (mm)", { exact: true })).toHaveValue("868");
    await expect(page.getByLabel("Höjd (mm)", { exact: true })).toHaveValue("1270");
    await expect(page.getByRole("button", { name: "Förbered tillverkning" })).toBeDisabled();
  });
  for (const family of ["table", "chest_of_drawers", "shelving"] as FurnitureFamily[]) {
    test(`${family}: spara, byta profil, öppna igen och hämta CAD`, async ({ page, request }, info) => {
      test.setTimeout(family === "shelving" ? 480_000 : 240_000);
      const provisioned = await provisionLiveProject(request, info, `furniture-${family}`);
      const base = process.env.PLAYWRIGHT_API_URL!.replace(/\/$/, "");
      const headers = { Authorization: `Bearer ${process.env.PLAYWRIGHT_DEMO_TOKEN || "demo-nordic-owner"}` };
      const path = `${base}/v1/furniture/projects/${provisioned.project.id}`;
      const initial = newFurnitureWorkspace(family);
      if (family === "shelving") {
        initial.design.intent = { ...initial.design.intent, width_um: 700_001,
          height_um: 1_000_003, shelf_count: 2, divider_count: 0 };
        initial.manufacturing = { machine_profile_id: "custombuild-router-1325-linuxcnc",
          machine_profile_version: "1.0.0-validation", stock_width_um: 2_440_000,
          stock_height_um: 1_220_000, stock_grain_axis: "x" };
      }
      const saved = await request.put(`${path}/draft`, { headers,
        data: { expected_revision: 0, workspace: initial } });
      expect(saved.status(), await saved.text()).toBe(200);
      const apiFailures: string[] = [];
      page.on("response", response => {
        if (new URL(response.url()).pathname.startsWith("/v1/") && response.status() >= 400) {
          apiFailures.push(`${response.request().method()} ${new URL(response.url()).pathname}: ${response.status()}`);
        }
      });
      page.on("requestfailed", request => {
        if (new URL(request.url()).pathname.startsWith("/v1/")
          && !request.failure()?.errorText.includes("ERR_ABORTED")) {
          apiFailures.push(`${request.method()} ${new URL(request.url()).pathname}: ${request.failure()?.errorText}`);
        }
      });
      await selectProjectBeforeNavigation(page, provisioned);
      await page.goto("/furniture");
      const projectSelect = page.getByRole("combobox", { name: "Öppna möbelprojekt" });
      await expect(projectSelect.locator(`option[value="${provisioned.project.id}"]`)).toHaveCount(1);
      await projectSelect.selectOption(provisioned.project.id);
      await expect(page.getByLabel("Möbeltyp", { exact: true })).toHaveValue(family);
      await expect(page.getByRole("button", { name: "Skapa granskningspaket" })).toBeEnabled();

      await page.getByLabel("Uppmätt skivtjocklek (mm)", { exact: true }).fill("17.801");
      await page.getByRole("button", { name: "Kontrollera profilbyte" }).click();
      await expect(page.getByText("Konsekvenser av profilbytet")).toBeVisible();
      await page.getByRole("button", { name: "Använd profilbytet i designen" }).click();
      await expect(page.getByRole("button", { name: "Skapa granskningspaket" })).toBeDisabled();
      await expect(page.getByRole("button", { name: "Spara revision" })).toBeEnabled();
      await page.getByRole("button", { name: "Spara revision" }).click();
      await expect(page.getByText("Revision 2 är sparad.")).toBeVisible();
      await page.reload();
      await expect(projectSelect.locator(`option[value="${provisioned.project.id}"]`)).toHaveCount(1);
      await projectSelect.selectOption(provisioned.project.id);
      await expect(page.getByLabel("Uppmätt skivtjocklek (mm)", { exact: true })).toHaveValue("17.801");
      await expect(page.getByRole("button", { name: /^Revision 1 ·/ })).toBeVisible();
      await expect(page.locator("canvas")).toHaveAttribute("data-custombuild-render-commit", /^[1-9]\d*$/, { timeout: 30_000 });
      await attachView(page, info, `${family}-assembled`);
      if (family === "chest_of_drawers") {
        await page.getByRole("button", { name: "Öppna lådorna" }).click();
        await expect(page.getByRole("button", { name: "Stäng lådorna" })).toHaveAttribute("aria-pressed", "true");
      }
      await page.getByRole("button", { name: "Skapa granskningspaket" }).click();
      const link = page.getByRole("link", { name: /Hämta STEP, GLB, DXF/ });
      await expect(link).toBeVisible({ timeout: 200_000 });
      const downloadEvent = page.waitForEvent("download");
      await link.click();
      const download = await downloadEvent;
      expect(await download.failure()).toBeNull();
      expect(download.suggestedFilename()).toBe("custombuild-design-review-2.zip");
      const bytes = await readFile((await download.path())!);
      expect(bytes.subarray(0, 4)).toEqual(Buffer.from([0x50, 0x4b, 0x03, 0x04]));
      expect(bytes.length).toBeGreaterThan(1_000);
      await attachView(page, info, `${family}-studio`);
      await page.setViewportSize({ width: 390, height: 844 });
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
      await attachView(page, info, `${family}-mobile`);
      if (family === "shelving") {
        await page.setViewportSize({ width: 1440, height: 1000 });
        await page.getByRole("button", { name: "Förbered tillverkning" }).click();
        await expect(page.getByRole("heading", { name: "Förbered tillverkningen" })).toBeVisible();
        const savedVersion = page.waitForResponse(r => r.request().method() === "POST"
          && new URL(r.url()).pathname === `/v1/projects/${provisioned.project.id}/versions`);
        await page.getByRole("button", { name: "Spara och kontrollera" }).click();
        const versionResponse = await savedVersion;
        expect(versionResponse.status(), await versionResponse.text()).toBe(201);
        const version = await versionResponse.json();
        expect(version.spec_json.parameters.actual_thickness_um).toBe(17_801);
        expect(version.spec_json.parameters.width_um).toBe(700_001);
        expect(version.result_json.source_furniture.workspace.design.revision).toBe(2);
        await page.getByRole("checkbox", { name: "Jag har läst och kontrollerat varningarna ovan." }).check();
        await page.getByRole("button", { name: "Godkänn designkontroll", exact: true }).click();
        await expect(page.getByRole("button", { name: "Skapa underlag", exact: true })).toBeEnabled();
        await page.getByRole("button", { name: "Skapa underlag", exact: true }).click();
        const downloadButton = page.getByRole("button", { name: "Ladda ned granskningspaket (.zip)", exact: true });
        await expect(downloadButton).toBeVisible({ timeout: 200_000 });
        const fullDownloadEvent = page.waitForEvent("download");
        await downloadButton.click();
        const fullDownload = await fullDownloadEvent;
        expect(await fullDownload.failure()).toBeNull();
        const productionBytes = await readFile((await fullDownload.path())!);
        expect(productionBytes.length).toBeGreaterThan(1_000);
        const exportedSource = JSON.parse(execFileSync("python3", ["-c", [
          "import hashlib,json,sys,zipfile",
          "with zipfile.ZipFile(sys.argv[1]) as z:",
          " data=z.read('design/furniture-source.json')",
          " manifest=json.loads(z.read('manifest.json'))",
          " entry=next(e for e in manifest['artifacts'] if e['path']=='design/furniture-source.json')",
          " assert hashlib.sha256(data).hexdigest()==entry['sha256']",
          " assert manifest['physical_cutting_authorized'] is False",
          " print(data.decode())",
        ].join("\n"), (await fullDownload.path())!], { encoding: "utf8" }));
        expect(exportedSource).toEqual(version.result_json.source_furniture);
        await attachView(page, info, "shelving-production");
        await page.reload();
        await projectSelect.selectOption(provisioned.project.id);
        await page.getByRole("button", { name: "Förbered tillverkning" }).click();
        await expect(downloadButton).toBeVisible({ timeout: 30_000 });
        const restored = await request.get(`${base}/v1/projects/${provisioned.project.id}/production-state`, { headers });
        expect(restored.ok()).toBe(true);
        expect((await restored.json()).version.id).toBe(version.id);
        await page.setViewportSize({ width: 390, height: 844 });
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
        await attachView(page, info, "shelving-production-mobile");
      }
      expect(apiFailures).toEqual([]);
    });
  }
});
