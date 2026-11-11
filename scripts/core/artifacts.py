"""Content-addressed artifact envelopes used by review and approval flows."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .clock import utc_now

ARTIFACT_SCHEMA_VERSION = 1


class ArtifactError(ValueError):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def canonical_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_json(data: Any) -> str:
    return sha256_text(canonical_json(data))


def file_sha256(path: Path) -> str:
    try:
        return sha256_bytes(path.read_bytes())
    except OSError as exc:
        raise ArtifactError("ARTIFACT_SOURCE_MISSING", f"cannot read artifact source: {path}") from exc


def envelope(
    artifact_type: str,
    payload: Any,
    *,
    project_id: str | None = None,
    chapter_id: str | None = None,
    source_sha256: str | None = None,
    producer: str = "govel",
) -> dict[str, Any]:
    body = {
        "artifactType": artifact_type,
        "schemaVersion": ARTIFACT_SCHEMA_VERSION,
        "projectId": project_id,
        "chapterId": chapter_id,
        "sourceSha256": source_sha256,
        "createdAt": utc_now(),
        "producer": producer,
        "payload": payload,
    }
    body["artifactSha256"] = sha256_json(body)
    return body


def validate_envelope(data: dict[str, Any], *, artifact_type: str | None = None) -> dict[str, Any]:
    if not isinstance(data, dict) or data.get("schemaVersion") != ARTIFACT_SCHEMA_VERSION:
        raise ArtifactError("ARTIFACT_INVALID", "invalid artifact envelope schema")
    required = ("artifactType", "createdAt", "producer", "payload", "artifactSha256")
    missing = [key for key in required if key not in data]
    if missing:
        raise ArtifactError("ARTIFACT_INVALID", "artifact envelope missing fields", {"missing": missing})
    if artifact_type and data.get("artifactType") != artifact_type:
        raise ArtifactError("ARTIFACT_TYPE_MISMATCH", f"expected {artifact_type}, got {data.get('artifactType')}")
    expected = sha256_json({key: value for key, value in data.items() if key != "artifactSha256"})
    if data.get("artifactSha256") != expected:
        raise ArtifactError("ARTIFACT_HASH_MISMATCH", "artifact envelope hash does not match payload")
    return data
