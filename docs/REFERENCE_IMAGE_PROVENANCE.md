# Reference image provenance

Reference images are inputs to a reviewed concept workflow. They are never treated as proof that
the inferred construction is safe or production-ready.

## Upload contract

An authenticated designer uploads a JPG, PNG or WebP file to
`POST /v1/projects/{project_id}/imports/inspect`. The API validates the file signature and name,
computes SHA-256 from the received bytes, and stores the bytes under a tenant- and project-scoped
content address whose final path component is a server-issued UUIDv4 import incarnation. The
client never supplies an object key, incarnation or trusted checksum.

The returned `import_id` is that incarnation and `image_sha256` identifies its bytes. Uploading or
pasting the same bytes again in the same project reuses the existing identity only while its full
committed storage-ledger identity and streamed bytes still verify. An identity from another project
or organization cannot be attached to a revision.

Every newly accepted physical import reserves both a unique object key ending in `import_id` and
the idempotency identity `imported:{import_id}` before writing bytes. Concurrent same-byte uploads
therefore have different UUID incarnations; the database winner remains the source record and a
losing reservation is recovered by the reaper. After confirmed provider deletion, finalization
permanently records the losing or deleted key and idempotency identity in the append-only tombstone
registry. Neither can ever be reused or rebound, even for identical bytes; a later accepted import
must receive a new UUID incarnation.

## Revision binding

Reference-derived revisions require all of the following:

- the server-issued `import_id` and `image_sha256`;
- all four explicit confirmations for dimensions, layout, material and construction assumptions;
- a model fingerprint equal to the current server-generated `design_hash`;
- a full streamed checksum verification of the stored source object.

Changing the model after confirmation causes a fingerprint conflict and requires a new review.
Replacing the source creates a new concept state and clears the previous model confirmation. A
missing or modified object returns a non-leaking 409 response; unavailable object storage returns
503. Both responses include a concrete recovery action and leave the revision unsaved.

## Offline evidence

The frozen provenance snapshot includes the import ID, image SHA-256, server model fingerprint,
media type, byte length and source-asset timestamp. It is emitted as
`validation/source-provenance.json`, hashed as a package artifact, and also included in the signed
manifest context. An offline reviewer can therefore compare the original image bytes with the
manifested SHA-256 without access to the private object key.

The import record, live object ledger, append-only retired-key history and S3 inventory are one
paired recovery domain. Coordinated backup v5 captures the exact tombstone count and history hash;
restore evidence v4 must reproduce that proof together with the exact object inventory. Restoring
only PostgreSQL or only S3, or trimming tombstones after retention expiry, would break provenance
and is forbidden.

This evidence does not change the release boundary: generated CAM remains validation-only and
`physical_cutting_authorized` remains `false`.
