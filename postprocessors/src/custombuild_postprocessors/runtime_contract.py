"""Closed workshop observations for the supported LinuxCNC runtime.

The CI oracle qualifies standalone rs274 only.  These observations and the
retained inventory are accepted at the workshop trust boundary; they are never
inferred from an oracle PASS or filled in by the production authoring tool.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from custombuild_manufacturing.model import canonical_json_bytes, sha256_hex

LINUXCNC_ORACLE_CONTROLLER_VERSION = "2.9.4"
LINUXCNC_ORACLE_PACKAGE_VERSION = "1:2.9.4-2+deb13u1"
LINUXCNC_ORACLE_CONTAINER_IMAGE = (
    "debian:trixie-slim@sha256:abc9cb88a5587630d7f915f47b23b0668fe250fbfc6457aa4d52b534c1bbf73f"
)
LINUXCNC_ORACLE_DEBIAN_SNAPSHOT = "20260824T000000Z"
LINUXCNC_ORACLE_BUILD = MappingProxyType(
    {
        "container_image": LINUXCNC_ORACLE_CONTAINER_IMAGE,
        "debian_snapshot": LINUXCNC_ORACLE_DEBIAN_SNAPSHOT,
        "linuxcnc_package": "linuxcnc-uspace",
        "linuxcnc_package_version": LINUXCNC_ORACLE_PACKAGE_VERSION,
        "schema_version": "custombuild.linuxcnc-oracle-build.v1",
    }
)
LINUXCNC_ORACLE_BUILD_SHA256 = sha256_hex(canonical_json_bytes(dict(LINUXCNC_ORACLE_BUILD)))
RUNTIME_CONTRACT_SCHEMA_VERSION = "custombuild.linuxcnc-runtime-contract.v1"

# Keys describe effective loaded values, after includes and startup overrides.
# False means absent/disabled, not merely omitted from the principal INI file.
REQUIRED_RUNTIME_SETTINGS: Mapping[str, str | int | bool] = MappingProxyType(
    {
        "TRAJ.SPINDLES": 1,
        "MOTION.num_spindles": 1,
        "COMMAND.spindle_index": 0,
        "SPINDLE_0.commands_feedback_interlocks_bound": True,
        "SPINDLE_0.interlocks_compare_exact_programmed_S": True,
        "RS274NGC.REMAP_T": False,
        "RS274NGC.REMAP_S": False,
        "RS274NGC.REMAP_F": False,
        "RS274NGC.ON_ABORT_COMMAND": False,
        "FILTER.ngc": False,
        "INPUT.exact_verified_bytes_to_interpreter": True,
        "TASK.TASK": "milltask",
        "TRAJ.TPMOD": "tpmod",
        "EMCMOT.EMCMOT": "motmod",
        "EMCMOT.HOMEMOD": "homemod",
        "EMCIO.EMCIO": "io",
        "STARTUP.alternate_components_or_search_path_overrides": False,
        "M6.axis_motion": False,
        "EMCIO.TOOL_CHANGE_POSITION": False,
        "EMCIO.TOOL_CHANGE_QUILL_UP": False,
        "EMCIO.TOOL_CHANGE_AT_G30": False,
        "EMCIO.DB_PROGRAM": False,
        "EMCIO.file_tool_table_backend": True,
        **{f"JOINT_{i}.TYPE": "LINEAR" for i in range(3)},
        **{f"JOINT_{i}.effective_UNITS": "mm" for i in range(3)},
    }
)
RUNTIME_COMPONENTS = frozenset({"rs274", "milltask", "tpmod", "motmod", "homemod", "io"})
RUNTIME_INPUTS = frozenset({"expanded_ini", "expanded_hal", "hal_topology", "tool_table"})
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class RuntimeSetting:
    name: str
    value: str | int | bool


@dataclass(frozen=True, slots=True)
class RuntimeInventoryEntry:
    name: str
    sha256: str


@dataclass(frozen=True, slots=True)
class LinuxCNCRuntimeContract:
    schema_version: str
    oracle_build_sha256: str
    installed_package_version: str
    effective_settings: tuple[RuntimeSetting, ...]
    components: tuple[RuntimeInventoryEntry, ...]
    configuration_inputs: tuple[RuntimeInventoryEntry, ...]
    min_forward_velocity_rpm: int
    max_forward_velocity_rpm: int
    evidence_id: str
    evidence_version: str
    evidence_sha256: str
    workshop_runtime_verified: bool

    def __post_init__(self) -> None:
        for typed_entries, entry_type in (
            (self.effective_settings, RuntimeSetting),
            (self.components, RuntimeInventoryEntry),
            (self.configuration_inputs, RuntimeInventoryEntry),
        ):
            if type(typed_entries) is not tuple or any(
                type(value) is not entry_type for value in typed_entries
            ):
                raise ValueError("runtime observations must be immutable typed inventories")
        if self.schema_version != RUNTIME_CONTRACT_SCHEMA_VERSION:
            raise ValueError("unsupported LinuxCNC runtime contract schema")
        if self.oracle_build_sha256 != LINUXCNC_ORACLE_BUILD_SHA256:
            raise ValueError("LinuxCNC oracle build digest mismatch")
        if self.installed_package_version != LINUXCNC_ORACLE_PACKAGE_VERSION:
            raise ValueError("LinuxCNC package is outside the oracle-qualified build")
        if tuple(item.name for item in self.effective_settings) != tuple(
            sorted(REQUIRED_RUNTIME_SETTINGS)
        ):
            raise ValueError("runtime settings must exactly cover the supported configuration")
        for item in self.effective_settings:
            expected = REQUIRED_RUNTIME_SETTINGS[item.name]
            if type(item.value) is not type(expected) or item.value != expected:
                raise ValueError(f"unsupported effective LinuxCNC setting: {item.name}")
        for label, entries, required in (
            ("components", self.components, RUNTIME_COMPONENTS),
            ("configuration_inputs", self.configuration_inputs, RUNTIME_INPUTS),
        ):
            if tuple(item.name for item in entries) != tuple(sorted(required)):
                raise ValueError(f"runtime {label} inventory is incomplete or non-canonical")
            for entry in entries:
                if not _SHA256.fullmatch(entry.sha256):
                    raise ValueError(f"runtime {label} requires SHA-256 byte digests")
        if (
            type(self.min_forward_velocity_rpm) is not int
            or type(self.max_forward_velocity_rpm) is not int
            or not 0 < self.min_forward_velocity_rpm < self.max_forward_velocity_rpm
        ):
            raise ValueError("runtime spindle limits must be positive increasing integer RPM")
        for value in (self.evidence_id, self.evidence_version):
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", value):
                raise ValueError("runtime evidence requires canonical identities")
        if not _SHA256.fullmatch(self.evidence_sha256):
            raise ValueError("runtime evidence requires a SHA-256 byte digest")
        if self.workshop_runtime_verified is not True:
            raise ValueError("the workshop must explicitly verify the effective runtime")

    @property
    def sha256(self) -> str:
        return sha256_hex(canonical_json_bytes(self))

    def as_dict(self) -> dict[str, Any]:
        return dict(json.loads(canonical_json_bytes(self)))

    @classmethod
    def from_mapping(cls, value: Any) -> LinuxCNCRuntimeContract:
        expected = {
            "schema_version",
            "oracle_build_sha256",
            "installed_package_version",
            "effective_settings",
            "components",
            "configuration_inputs",
            "min_forward_velocity_rpm",
            "max_forward_velocity_rpm",
            "evidence_id",
            "evidence_version",
            "evidence_sha256",
            "workshop_runtime_verified",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError("runtime contract has missing or unknown fields")
        for key in expected - {
            "effective_settings",
            "components",
            "configuration_inputs",
            "min_forward_velocity_rpm",
            "max_forward_velocity_rpm",
            "workshop_runtime_verified",
        }:
            if type(value[key]) is not str:
                raise ValueError(f"runtime {key} must be a string")
        collections: dict[str, list[dict[str, Any]]] = {}
        for key, fields in (
            ("effective_settings", {"name", "value"}),
            ("components", {"name", "sha256"}),
            ("configuration_inputs", {"name", "sha256"}),
        ):
            entries = value[key]
            if not isinstance(entries, list) or any(
                not isinstance(item, dict)
                or set(item) != fields
                or type(item["name"]) is not str
                or ("sha256" in item and type(item["sha256"]) is not str)
                for item in entries
            ):
                raise ValueError(f"runtime {key} must be a closed inventory array")
            collections[key] = entries
        return cls(
            **{key: value[key] for key in expected - collections.keys()},
            effective_settings=tuple(
                RuntimeSetting(**item) for item in collections["effective_settings"]
            ),
            components=tuple(RuntimeInventoryEntry(**item) for item in collections["components"]),
            configuration_inputs=tuple(
                RuntimeInventoryEntry(**item) for item in collections["configuration_inputs"]
            ),
        )
