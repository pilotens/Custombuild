from __future__ import annotations

from typing import Any

import pytest
from app import storage


class RecordingS3Client:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def generate_presigned_url(self, operation: str, **kwargs: Any) -> str:
        self.calls.append({"operation": operation, **kwargs})
        return "https://artifacts.example.test/signed"


def test_presigned_download_sets_a_safe_content_disposition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = RecordingS3Client()
    monkeypatch.setattr(storage, "s3_client", lambda: client)

    result = storage.presigned_get(
        "org/job/bundle.zip",
        filename="custombuild-rev-7.zip",
    )

    assert result == "https://artifacts.example.test/signed"
    assert client.calls[0]["Params"]["ResponseContentDisposition"] == (
        'attachment; filename="custombuild-rev-7.zip"'
    )


@pytest.mark.parametrize(
    "filename",
    ("../bundle.zip", "bundle name.zip", "bundle\r\nX-Test: injected", "åäö.zip"),
)
def test_presigned_download_rejects_unsafe_filenames(
    monkeypatch: pytest.MonkeyPatch,
    filename: str,
) -> None:
    client = RecordingS3Client()
    monkeypatch.setattr(storage, "s3_client", lambda: client)

    with pytest.raises(ValueError, match="unsafe"):
        storage.presigned_get("org/job/bundle.zip", filename=filename)

    assert client.calls == []
