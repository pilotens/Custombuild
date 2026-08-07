from __future__ import annotations

from custombuild_manufacturing import MANIFEST_CONTEXT_HASH_FIELDS

from scripts.live_acceptance import CONTEXT_HASH_FIELDS


def test_live_acceptance_hashes_the_exact_manifest_context_contract() -> None:
    assert CONTEXT_HASH_FIELDS == MANIFEST_CONTEXT_HASH_FIELDS
