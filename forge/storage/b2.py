"""
forge/storage/b2.py
-------------------
Backblaze B2 Cloud Storage integration for Aku-Content Forge.

Responsibilities:
  - Upload generated assets (images, audio, video, JSON, MD) to B2 buckets.
  - Return public CDN URLs that are injected into textbook manifests.
  - Write provenance side-car records as B2 object metadata.
  - Provide a thin abstraction so the rest of the pipeline never touches the
    B2 SDK directly.

Environment variables required:
  B2_APPLICATION_KEY_ID   — your Backblaze B2 application key ID
  B2_APPLICATION_KEY      — your Backblaze B2 application key
"""

from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# B2 object metadata has a 2 KB total limit; leave 100 bytes of headroom for
# the metadata key name and any encoding overhead.
B2_METADATA_MAX_BYTES = 1900

import boto3
from botocore.config import Config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Bucket name helpers
# ---------------------------------------------------------------------------

BUCKET_MAP = {
    "textbooks": "aku-textbooks",
    "images":    "aku-images",
    "audio":     "aku-audio",
    "video":     "aku-video",
}


def _bucket_name(asset_type: str) -> str:
    return BUCKET_MAP.get(asset_type, f"aku-{asset_type}")


# ---------------------------------------------------------------------------
# B2Client
# ---------------------------------------------------------------------------

class B2Client:
    """Thin wrapper around the S3-compatible Backblaze B2 API."""

    def __init__(self, cfg: dict[str, Any]) -> None:
        b2_cfg = cfg.get("b2", {})
        self.region = b2_cfg.get("region", "us-west-004")
        self.cdn_base_url: str = b2_cfg.get("cdn_base_url", "")
        self.public_cdn: bool = b2_cfg.get("public_cdn", True)

        endpoint = f"https://s3.{self.region}.backblazeb2.com"
        self._s3 = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=os.environ["B2_APPLICATION_KEY_ID"],
            aws_secret_access_key=os.environ["B2_APPLICATION_KEY"],
            config=Config(signature_version="s3v4"),
            region_name=self.region,
        )
        logger.info("B2Client initialised — endpoint: %s", endpoint)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def upload_bytes(
        self,
        data: bytes,
        asset_type: str,
        object_key: str,
        content_type: str | None = None,
        provenance: dict[str, Any] | None = None,
    ) -> str:
        """Upload raw bytes and return the public URL.

        Args:
            data:         Raw bytes to upload.
            asset_type:   One of ``textbooks``, ``images``, ``audio``, ``video``.
            object_key:   S3 key within the bucket (e.g. ``mathematics/ss1/ch01.png``).
            content_type: MIME type; auto-detected from *object_key* if omitted.
            provenance:   Optional provenance dict stored as object metadata.

        Returns:
            Public URL string.
        """
        bucket = _bucket_name(asset_type)
        if content_type is None:
            content_type, _ = mimetypes.guess_type(object_key)
            content_type = content_type or "application/octet-stream"

        extra_args: dict[str, Any] = {"ContentType": content_type}
        if self.public_cdn:
            extra_args["ACL"] = "public-read"
        if provenance:
            # B2 metadata values must be strings and ≤ 2 KB total
            extra_args["Metadata"] = {
                "provenance": _truncate(json.dumps(provenance), B2_METADATA_MAX_BYTES),
            }

        self._s3.put_object(
            Bucket=bucket,
            Key=object_key,
            Body=data,
            **extra_args,
        )
        url = self._public_url(bucket, object_key)
        logger.info("Uploaded s3://%s/%s → %s", bucket, object_key, url)
        return url

    def upload_file(
        self,
        local_path: Path | str,
        asset_type: str,
        object_key: str,
        provenance: dict[str, Any] | None = None,
    ) -> str:
        """Read a local file and upload it. Returns the public URL."""
        local_path = Path(local_path)
        data = local_path.read_bytes()
        content_type, _ = mimetypes.guess_type(local_path.name)
        return self.upload_bytes(data, asset_type, object_key, content_type, provenance)

    def upload_text(
        self,
        text: str,
        asset_type: str,
        object_key: str,
        content_type: str = "text/plain; charset=utf-8",
        provenance: dict[str, Any] | None = None,
    ) -> str:
        """Convenience wrapper for plain text / JSON / Markdown uploads."""
        return self.upload_bytes(
            text.encode("utf-8"), asset_type, object_key, content_type, provenance
        )

    def ensure_buckets(self) -> None:
        """Create any missing B2 buckets (idempotent)."""
        existing = {
            b["Name"]
            for b in self._s3.list_buckets().get("Buckets", [])
        }
        for bucket in BUCKET_MAP.values():
            if bucket not in existing:
                self._s3.create_bucket(Bucket=bucket)
                logger.info("Created bucket: %s", bucket)
            else:
                logger.debug("Bucket already exists: %s", bucket)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _public_url(self, bucket: str, key: str) -> str:
        if self.cdn_base_url:
            return f"{self.cdn_base_url.rstrip('/')}/{key}"
        return (
            f"https://{bucket}.s3.{self.region}.backblazeb2.com/{key}"
        )


# ---------------------------------------------------------------------------
# Provenance helpers
# ---------------------------------------------------------------------------

def build_provenance(
    stage: str,
    provider: str,
    model: str,
    prompt: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a provenance record to attach to every generated asset."""
    record: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generator": "aku-content-forge",
        "stage": stage,
        "provider": provider,
        "model": model,
    }
    if prompt:
        record["prompt_sha256"] = hashlib.sha256(prompt.encode()).hexdigest()
        record["prompt"] = prompt
    if extra:
        record.update(extra)
    return record


# ---------------------------------------------------------------------------
# Internal utilities
# ---------------------------------------------------------------------------

def _truncate(s: str, max_bytes: int) -> str:
    encoded = s.encode("utf-8")
    if len(encoded) <= max_bytes:
        return s
    return encoded[:max_bytes].decode("utf-8", errors="ignore")
