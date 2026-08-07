from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel

_CUSTOMBUILD_NAMESPACE = uuid.UUID("f8b44bb5-8010-55a8-a125-354329ce99ab")


def stable_id(entity: str, *semantic_path: object) -> str:
    """Return an ID stable across rebuilds and non-identity parameter edits."""

    name = "/".join([entity, *(str(item) for item in semantic_path)])
    return f"{entity[:3]}_{uuid.uuid5(_CUSTOMBUILD_NAMESPACE, name).hex}"


def _canonical(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _canonical(value.model_dump(mode="python", exclude_none=False))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, Mapping):
        return {str(key): _canonical(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, set | frozenset):
        return sorted(
            (_canonical(item) for item in value), key=lambda item: json.dumps(item, sort_keys=True)
        )
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_canonical(item) for item in value]
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(
        _canonical(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
