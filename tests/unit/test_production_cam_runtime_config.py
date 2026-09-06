from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest
from app import config_guards
from app.config import Settings
from app.schemas import GenerationRequest
from custombuild_worker.config import WorkerSettings
from pydantic import ValidationError


def test_api_and_worker_read_the_same_exact_server_owned_profile(tmp_path: Path) -> None:
    profile = tmp_path / "production-cam-profile.json"
    payload = b'{"profile":"exact-workshop-profile"}'
    profile.write_bytes(payload)
    profile_sha256 = hashlib.sha256(payload).hexdigest()

    api = Settings(
        _env_file=None,
        production_cam_profile_path=str(profile),
        production_cam_profile_sha256=profile_sha256,
    )
    worker = WorkerSettings(
        _env_file=None,
        production_cam_profile_path=str(profile),
        production_cam_profile_sha256=profile_sha256,
    )

    assert api.production_cam_profile_source == payload
    assert worker.production_cam_profile_source == payload
    assert api.production_cam_profile_path == worker.production_cam_profile_path
    assert api.production_cam_profile_sha256 == worker.production_cam_profile_sha256


@pytest.mark.parametrize("settings_type", (Settings, WorkerSettings))
def test_development_cam_profile_remains_backward_compatible_without_pin(
    settings_type: type[Settings] | type[WorkerSettings],
    tmp_path: Path,
) -> None:
    profile = tmp_path / "production-cam-profile.json"
    payload = b'{"profile":"development"}'
    profile.write_bytes(payload)

    assert (
        settings_type(
            _env_file=None,
            production_cam_profile_path=str(profile),
        ).production_cam_profile_source
        == payload
    )


@pytest.mark.parametrize("settings_type", (Settings, WorkerSettings))
def test_development_inline_cam_profile_honours_optional_exact_pin(
    settings_type: type[Settings] | type[WorkerSettings],
) -> None:
    payload = '{"profile":"inline-development"}'
    settings = settings_type(
        _env_file=None,
        production_cam_profile_json=payload,
        production_cam_profile_sha256=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
    )

    assert settings.production_cam_profile_source == payload


@pytest.mark.parametrize("settings_type", (Settings, WorkerSettings))
def test_production_cam_profile_path_requires_deployment_sha256_pin(
    settings_type: type[Settings] | type[WorkerSettings],
    tmp_path: Path,
) -> None:
    profile = tmp_path / "production-cam-profile.json"
    profile.write_text("{}", encoding="utf-8")

    with pytest.raises(
        ValidationError,
        match="production CAM profile path requires PRODUCTION_CAM_PROFILE_SHA256",
    ):
        settings_type(
            _env_file=None,
            app_env="production",
            production_cam_profile_path=str(profile),
        )


@pytest.mark.parametrize("settings_type", (Settings, WorkerSettings))
@pytest.mark.parametrize("profile_sha256", ("A" * 64, "a" * 63))
def test_cam_profile_deployment_sha256_pin_must_be_lowercase_exact_hex(
    settings_type: type[Settings] | type[WorkerSettings],
    profile_sha256: str,
) -> None:
    with pytest.raises(ValidationError, match="must be lowercase 64-character hex"):
        settings_type(
            _env_file=None,
            production_cam_profile_json="{}",
            production_cam_profile_sha256=profile_sha256,
        )


@pytest.mark.parametrize("settings_type", (Settings, WorkerSettings))
def test_cam_profile_deployment_sha256_pin_mismatch_fails_closed(
    settings_type: type[Settings] | type[WorkerSettings],
    tmp_path: Path,
) -> None:
    profile = tmp_path / "production-cam-profile.json"
    profile.write_text("{}", encoding="utf-8")

    with pytest.raises(ValidationError, match="SHA-256 does not match configured bytes"):
        settings_type(
            _env_file=None,
            production_cam_profile_path=str(profile),
            production_cam_profile_sha256="0" * 64,
        )


@pytest.mark.parametrize("settings_type", (Settings, WorkerSettings))
def test_cam_profile_deployment_pin_is_rechecked_on_every_property_read(
    settings_type: type[Settings] | type[WorkerSettings],
    tmp_path: Path,
) -> None:
    profile = tmp_path / "production-cam-profile.json"
    original = b'{"profile":"old"}'
    profile.write_bytes(original)
    settings = settings_type(
        _env_file=None,
        production_cam_profile_path=str(profile),
        production_cam_profile_sha256=hashlib.sha256(original).hexdigest(),
    )
    profile.write_bytes(b'{"profile":"new"}')

    with pytest.raises(ValueError, match="SHA-256 does not match configured bytes"):
        _ = settings.production_cam_profile_source


@pytest.mark.parametrize("settings_type", (Settings, WorkerSettings))
def test_cam_profile_path_and_inline_json_are_mutually_exclusive(
    settings_type: type[Settings] | type[WorkerSettings],
    tmp_path: Path,
) -> None:
    profile = tmp_path / "production-cam-profile.json"
    profile.write_text("{}", encoding="utf-8")

    with pytest.raises(ValidationError, match="mutually exclusive"):
        settings_type(
            _env_file=None,
            production_cam_profile_path=str(profile),
            production_cam_profile_json="{}",
        )


@pytest.mark.parametrize("settings_type", (Settings, WorkerSettings))
def test_production_rejects_inline_cam_profile_environment_payload(
    settings_type: type[Settings] | type[WorkerSettings],
) -> None:
    # Run the shared CAM guard before unrelated production guards so this
    # remains a focused regression test without duplicating every credential.
    with pytest.raises(ValidationError, match="must use PRODUCTION_CAM_PROFILE_PATH"):
        settings_type(
            _env_file=None,
            app_env="production",
            production_cam_profile_json="{}",
        )


@pytest.mark.parametrize("settings_type", (Settings, WorkerSettings))
def test_production_rejects_group_writable_cam_profile(
    settings_type: type[Settings] | type[WorkerSettings],
    tmp_path: Path,
) -> None:
    profile = tmp_path / "production-cam-profile.json"
    profile.write_text("{}", encoding="utf-8")
    profile.chmod(0o664)
    profile_sha256 = hashlib.sha256(profile.read_bytes()).hexdigest()

    with pytest.raises(ValidationError, match="group- or world-writable"):
        settings_type(
            _env_file=None,
            app_env="production",
            production_cam_profile_path=str(profile),
            production_cam_profile_sha256=profile_sha256,
        )


@pytest.mark.parametrize("settings_type", (Settings, WorkerSettings))
def test_cam_profile_path_must_be_absolute_regular_and_nonempty(
    settings_type: type[Settings] | type[WorkerSettings],
    tmp_path: Path,
) -> None:
    with pytest.raises(ValidationError, match="canonical absolute path"):
        settings_type(_env_file=None, production_cam_profile_path="relative/profile.json")

    empty = tmp_path / "empty.json"
    empty.write_bytes(b"")
    with pytest.raises(ValidationError, match="file size is invalid"):
        settings_type(_env_file=None, production_cam_profile_path=str(empty))

    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "profile-link.json"
    link.symlink_to(target)
    with pytest.raises(ValidationError, match="file is unavailable"):
        settings_type(_env_file=None, production_cam_profile_path=str(link))


def test_cam_profile_reader_rejects_same_size_in_place_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = tmp_path / "production-cam-profile.json"
    profile.write_bytes(b'{"profile":"old"}')
    replacement = b'{"profile":"new"}'
    initial_status = profile.stat()
    assert len(replacement) == initial_status.st_size
    original_read = os.read
    changed = False

    def read_then_change(descriptor: int, count: int) -> bytes:
        nonlocal changed
        payload = original_read(descriptor, count)
        if payload and not changed:
            changed = True
            profile.write_bytes(replacement)
            os.utime(
                profile,
                ns=(initial_status.st_atime_ns, initial_status.st_mtime_ns + 1_000_000_000),
            )
        return payload

    monkeypatch.setattr(os, "read", read_then_change)

    with pytest.raises(ValueError, match="changed while it was read"):
        config_guards.read_production_cam_profile_source(
            profile_path=str(profile),
            profile_json="",
            profile_sha256="",
            production=False,
        )
    assert changed is True


def test_unconfigured_cam_profile_remains_an_explicit_fail_closed_empty_source() -> None:
    assert Settings(_env_file=None).production_cam_profile_source == ""
    assert WorkerSettings(_env_file=None).production_cam_profile_source == ""


def test_cutting_candidate_request_is_opt_in_and_requires_validation_program() -> None:
    assert GenerationRequest().include_cutting_candidate is False
    assert GenerationRequest(include_cutting_candidate=True).include_validation_program is True
    with pytest.raises(ValidationError, match="requires include_validation_program"):
        GenerationRequest(
            include_cutting_candidate=True,
            include_validation_program=False,
        )
