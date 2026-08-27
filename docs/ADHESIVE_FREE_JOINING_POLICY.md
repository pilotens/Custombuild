# Adhesive-free joining policy

Date: 2026-08-17<br>
Status: authoritative product policy

This policy applies to every design, rule, generated document, bill of materials,
review package and future production adapter in Custombuild.

## Non-negotiable rules

- Adhesives, glue, epoxy, hot-melt, construction adhesive, sealant and other
  chemically bonded retention methods are prohibited.
- The preferred construction is dry, self-locking and interlocking: parts should
  slot into and retain one another in a Kigumi-like assembly where the geometry
  and material have been verified for that use.
- When a verified self-locking solution is not available, the design must use
  explicit removable mechanical retention such as screws, bolts, dowels, rails,
  hinges, brackets or locking hardware.
- Edge protection must be mechanically retained. An unresolved attachment method
  blocks physical release.
- A plain dado or slot may prove local bearing capacity, but it is not evidence of
  permanent retention, resistance to pull-out or a self-locking assembly.
- Software must never invent a hardware SKU, fastener layout, tolerance, wall
  anchor, fixture or workshop approval. Missing evidence remains a visible block.

## Release consequence

Design-review artifacts may describe unresolved decisions, but they must state
them explicitly. CAM approval and physical release remain blocked until the dry
self-locking or mechanical retention system is versioned, represented in the
model and supported by the required verification evidence.

`physical_cutting_authorized=false` remains mandatory for the current product.

## Supersession

This policy supersedes every earlier roadmap or readiness item that proposed
adhesive bonding as an implementation option. Historical documents may retain
their original dates, but they must be interpreted through this policy.
