# Reference image provenance

Reference images are inputs to a reviewed concept workflow. They are never treated as proof that
the inferred construction is safe or production-ready.

## Upload contract

An authenticated designer uploads a JPG, PNG or WebP file to
`POST /v1/projects/{project_id}/imports/inspect`. The API validates the file signature and name,
computes SHA-256 from the received bytes, and stores the bytes under a tenant- and project-scoped
content address. The client never supplies an object key or trusted checksum.

The returned `import_id` and `image_sha256` identify the immutable source. Uploading or pasting the
same bytes again in the same project reuses that identity. An identity from another project or
organization cannot be attached to a revision.

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

This evidence does not change the release boundary: generated CAM remains validation-only and
`physical_cutting_authorized` remains `false`.
