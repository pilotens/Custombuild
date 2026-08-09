# Semantic design, snapping and CAD integration

This document defines the product boundary introduced for the simplified
Custombuild editor. The user edits furniture concepts; deterministic software
creates geometry and manufacturing data.

## Product principle

The browser must not expose a general-purpose CAD interface. A user should drag
recognisable furniture components—shelf rows, dividers, backs and plinths—into a
furniture model. The editor resolves a compatible semantic snap target and emits
an intent such as:

```json
{
  "action": "add",
  "component_kind": "shelf_row",
  "target_id": "bookcase:shelf:bay:2",
  "source": "user_drag"
}
```

The intent contains no drill, pocket, toolpath or machine coordinates. It is
compiled to the canonical, versioned `DesignSpec`; the domain engine then rebuilds
parts, paired joints, features and the assembly graph. Construction screening and
DFM remain mandatory before any production context can be frozen.

## Layered flow

```text
User interaction / natural-language request
                 |
                 v
SemanticFurnitureIntent
  - component kind
  - compatible target
  - user/AI provenance
  - visual pointer intent
                 |
                 v
Semantic compiler (deterministic, fail-closed)
                 |
                 v
Canonical DesignSpec / material versions
                 |
        +--------+---------+
        |                  |
        v                  v
Rule engine         Furniture domain engine
                           |
                           v
                parts + joints + features
                           |
                           v
             Manufacturing Operations Model
                           |
              +------------+-------------+
              |                          |
              v                          v
     CadQuery/OpenCascade         nesting/CAM/postprocessor
              |
              v
 authoritative STEP/GLB
              |
              v
 optional headless FreeCAD import
 derivative FCStd project only
```

The editable source of truth is never a Three.js mesh, STEP file, FCStd file or
AI response.

## Semantic snapping

Ordinary CAD snapping matches points, edges and planes. Custombuild snapping
matches furniture relationships:

| Component | Relation | Current bookcase target |
| --- | --- | --- |
| Shelf row | `shelf_in_bay` | One of the current carcass bays |
| Divider | `divider_in_carcass` | The usable carcass opening |
| Back panel | `back_behind_carcass` | Rear carcass plane |
| Plinth | `plinth_under_carcass` | Carcass base |

The frontend shows a visual snap preview, but the pointer coordinate is not
promoted to a manufacturing coordinate. `DesignSpec 1.0` still distributes shelf
rows and divider-created bays equally. This limitation is displayed to users and
free movement fails closed until a versioned custom-position schema exists.

## AI boundary

AI can propose a `SemanticFurnitureIntent`, interpret natural language or suggest
an engineering change. It cannot directly create or edit:

- part dimensions;
- hole, groove or pocket coordinates;
- manufacturing features;
- CAM operations or toolpaths;
- machine code;
- approved material or hardware properties.

An intent with `source = ai_proposal` requires explicit user confirmation. After
confirmation it passes through the same deterministic compiler and every normal
validation gate. AI provenance and rationale remain attached to the intent.

## FreeCAD strategy

Custombuild already uses CadQuery/OpenCascade for authoritative CAD. The new
FreeCAD bridge is intentionally optional and downstream:

1. Custombuild generates and validates the authoritative STEP assembly.
2. A headless `FreeCADCmd` process imports that STEP.
3. The bridge stores the design hash, STEP checksum and contract versions inside
   the FCStd project.
4. The project is marked `CB_AuthoritativeGeometry = False` and warns that edits
   must not be used as CNC source.

This permits future use of FreeCAD TechDraw, Assembly or CAM capabilities without
coupling the user interface or the canonical design model to FreeCAD. A FreeCAD
failure cannot partially replace authoritative geometry.

## Current delivered scope

- Guided mode is the default editor mode.
- A draggable furniture-component palette is available in the 3D workspace.
- Dragging provides a semantic snap preview and compiles to the existing
  deterministic `DesignSpec`.
- Click-to-insert is provided as a keyboard/touch fallback.
- The Python domain package exposes the equivalent semantic document, targets,
  intents and compiler, including explicit AI confirmation.
- An optional headless FreeCAD STEP-to-FCStd bridge is available as a derivative
  interchange adapter.

## Deliberately deferred

The following require new versioned domain contracts rather than UI-only work:

- arbitrary per-shelf heights and unequal bay widths;
- moving existing components with persistent constraints;
- doors, drawers and hardware drag/drop;
- supplier-specific connector catalogues;
- generating TechDraw pages or FreeCAD CAM jobs in the production bundle;
- machine-specific postprocessors beyond verified profiles.

Each deferred capability must first be representable in the canonical domain
model, then covered by rules, CAD, DFM, manufacturing operations, assembly and
golden fixtures. Visual behaviour alone is not considered implementation.
