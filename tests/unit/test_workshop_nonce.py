from __future__ import annotations

import base64
from dataclasses import dataclass, fields, replace
from typing import Any, cast

import pytest
from app.workshop_nonce import (
    WorkshopNonceError,
    WorkshopNonceSet,
    derive_workshop_nonce_set,
    new_workshop_nonce_context,
    workshop_nonce_matches_digest,
)
from app.workshop_trust import STAGE_ORDER

SECRET = b"workshop-nonce-test-secret-material-0001"
RUN_SHA256 = "a" * 64
ALTERNATE_CONTEXT = base64.urlsafe_b64encode(b"\x01" * 32).decode().rstrip("=")


@dataclass(frozen=True, slots=True)
class _DerivationArgs:
    secret: bytes = SECRET
    key_version: str = "nonce-key-2026-09"
    organization_id: str = "11111111-1111-4111-8111-111111111111"
    run_sha256: str = RUN_SHA256
    nonce_set_id: str = "33333333-3333-4333-8333-333333333333"
    generation: int = 1
    derivation_context_base64: str = "A" * 43


DEFAULT_ARGS = _DerivationArgs()


def _derive(args: _DerivationArgs = DEFAULT_ARGS) -> WorkshopNonceSet:
    return derive_workshop_nonce_set(
        secret=args.secret,
        key_version=args.key_version,
        organization_id=args.organization_id,
        run_sha256=args.run_sha256,
        nonce_set_id=args.nonce_set_id,
        generation=args.generation,
        derivation_context_base64=args.derivation_context_base64,
    )


def test_nonce_set_is_exactly_repeatable_but_stage_separated() -> None:
    first = _derive()
    second = _derive()

    assert first == second
    assert tuple(item.stage for item in first.challenges) == STAGE_ORDER
    assert len({item.nonce for item in first.challenges}) == 3
    assert len({item.nonce_sha256 for item in first.challenges}) == 3
    assert first.nonce_by_stage() == {item.stage: item.nonce for item in first.challenges}
    assert all(
        workshop_nonce_matches_digest(item.nonce, item.nonce_sha256)
        for item in first.challenges
    )


@pytest.mark.parametrize(
    "changed_args",
    (
        replace(
            DEFAULT_ARGS,
            organization_id="22222222-2222-4222-8222-222222222222",
        ),
        replace(DEFAULT_ARGS, run_sha256="b" * 64),
        replace(
            DEFAULT_ARGS,
            nonce_set_id="44444444-4444-4444-8444-444444444444",
        ),
        replace(DEFAULT_ARGS, generation=2),
        replace(DEFAULT_ARGS, key_version="nonce-key-2026-10"),
        replace(DEFAULT_ARGS, derivation_context_base64=ALTERNATE_CONTEXT),
    ),
)
def test_every_persisted_identity_changes_all_challenges(
    changed_args: _DerivationArgs,
) -> None:
    baseline = _derive()
    changed = _derive(changed_args)

    assert baseline.digest_by_stage() != changed.digest_by_stage()


def test_raw_nonce_is_not_exposed_by_repr_or_digest_fields() -> None:
    nonce_set = _derive()

    for challenge in nonce_set.challenges:
        assert challenge.nonce not in repr(challenge)
        assert challenge.nonce not in repr(nonce_set)
        assert {item.name for item in fields(challenge)} == {
            "stage",
            "nonce",
            "nonce_sha256",
        }
        assert challenge.nonce != challenge.nonce_sha256


def test_nonce_digest_comparison_fails_closed() -> None:
    challenge = _derive().challenges[0]

    assert workshop_nonce_matches_digest(challenge.nonce, challenge.nonce_sha256)
    assert not workshop_nonce_matches_digest(challenge.nonce + "x", challenge.nonce_sha256)
    assert not workshop_nonce_matches_digest(challenge.nonce, "A" * 64)
    assert not workshop_nonce_matches_digest("raw-secret", challenge.nonce_sha256)


def test_nonce_digest_comparison_uses_constant_time_primitive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    challenge = _derive().challenges[0]
    compared: list[tuple[str, str]] = []

    def compare_digest(actual: str, expected: str) -> bool:
        compared.append((actual, expected))
        return True

    monkeypatch.setattr("app.workshop_nonce.hmac.compare_digest", compare_digest)

    assert workshop_nonce_matches_digest(challenge.nonce, challenge.nonce_sha256)
    assert compared == [(challenge.nonce_sha256, challenge.nonce_sha256)]


@pytest.mark.parametrize(
    ("nonce", "digest"),
    (
        (None, "a" * 64),
        (b"wn1_bytes", "a" * 64),
        ("wn1_valid-token", None),
        ("wn1_valid-token", b"a" * 64),
    ),
)
def test_nonce_digest_comparison_rejects_wrong_runtime_types_without_raising(
    nonce: object,
    digest: object,
) -> None:
    assert not workshop_nonce_matches_digest(nonce, digest)


@pytest.mark.parametrize(
    "overrides",
    (
        {"secret": b"short"},
        {"secret": bytearray(SECRET)},
        {"generation": 0},
        {"generation": True},
        {"run_sha256": "A" * 64},
        {"run_sha256": None},
        {"organization_id": "organization-not-a-uuid"},
        {"organization_id": "11111111-1111-4111-8111-11111111111A"},
        {"nonce_set_id": "nonce-set-not-a-uuid"},
        {"derivation_context_base64": "not-canonical"},
        {"derivation_context_base64": "!" * 43},
        {"derivation_context_base64": "B" * 43},
        {"derivation_context_base64": b"A" * 43},
        {"key_version": " space"},
    ),
)
def test_invalid_derivation_inputs_are_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises(WorkshopNonceError):
        # Runtime validation is the subject of this test, so deliberately cross
        # the static dataclass boundary with malformed values.
        _derive(replace(DEFAULT_ARGS, **cast(Any, overrides)))


def test_new_context_is_canonical_and_nonrepeating() -> None:
    first = new_workshop_nonce_context()
    second = new_workshop_nonce_context()

    assert len(first) == len(second) == 43
    assert first != second
    assert _derive(replace(DEFAULT_ARGS, derivation_context_base64=first)) != _derive(
        replace(DEFAULT_ARGS, derivation_context_base64=second)
    )
