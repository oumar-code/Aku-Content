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
import importlib
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

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Bucket name helpers
# ---------------------------------------------------------------------------

BUCKET_SUFFIXES = {
    "textbooks": "textbooks",
    "images":    "images",
    "audio":     "audio",
    "video":     "video",
}


def _build_bucket_map(bucket_prefix: str) -> dict[str, str]:
    return {
        asset_type: f"{bucket_prefix}-{suffix}"
        for asset_type, suffix in BUCKET_SUFFIXES.items()
    }


# ---------------------------------------------------------------------------
# B2Client
# ---------------------------------------------------------------------------

class B2Client:
    """Thin wrapper around the S3-compatible Backblaze B2 API."""

    def __init__(self, cfg: dict[str, Any], local_only: bool = False) -> None:
        b2_cfg = cfg.get("b2", {})
        self.local_only = local_only
        self.bucket_prefix = b2_cfg.get("bucket_prefix", "aku")
        self.bucket_map = _build_bucket_map(self.bucket_prefix)
        self.region = b2_cfg.get("region", "us-west-004")
        self.cdn_base_url: str = b2_cfg.get("cdn_base_url", "")
        self.public_cdn: bool = b2_cfg.get("public_cdn", True)
        self._s3: Any | None = None

        if self.local_only:
            logger.info(
                "B2Client initialised in local-only mode — uploads will be skipped"
            )
            return

        endpoint = f"https://s3.{self.region}.backblazeb2.com"
        boto3 = importlib.import_module("boto3")
        config_cls = importlib.import_module("botocore.config").Config
        self._s3 = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=os.environ["B2_APPLICATION_KEY_ID"],
            aws_secret_access_key=os.environ["B2_APPLICATION_KEY"],
            config=config_cls(signature_version="s3v4"),
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
        bucket = self.bucket_name(asset_type)
        if content_type is None:
            content_type, _ = mimetypes.guess_type(object_key)
            content_type = content_type or "application/octet-stream"

        if self.local_only:
            url = self._dry_run_url(bucket, object_key)
            logger.info("Local-only upload skipped: %s", url)
            return url

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
        if self.local_only:
            logger.info("Local-only mode — bucket creation skipped")
            return

        existing = {
            b["Name"]
            for b in self._s3.list_buckets().get("Buckets", [])
        }
        for bucket in self.bucket_map.values():
            if bucket not in existing:
                self._s3.create_bucket(Bucket=bucket)
                logger.info("Created bucket: %s", bucket)
            else:
                logger.debug("Bucket already exists: %s", bucket)

    def check_access(self) -> dict[str, Any]:
        """Report whether configured buckets are visible to the current credentials."""
        if self.local_only:
            return {
                "ok": True,
                "mode": "local-only",
                "buckets": self.bucket_names(),
            }

        existing = {
            bucket["Name"]
            for bucket in self._s3.list_buckets().get("Buckets", [])
        }
        status = {
            asset_type: {
                "bucket": bucket,
                "exists": bucket in existing,
            }
            for asset_type, bucket in self.bucket_map.items()
        }
        return {
            "ok": all(item["exists"] for item in status.values()),
            "mode": "live",
            "buckets": status,
        }

    def bucket_name(self, asset_type: str) -> str:
        return self.bucket_map.get(asset_type, f"{self.bucket_prefix}-{asset_type}")

    def bucket_names(self) -> dict[str, str]:
        return dict(self.bucket_map)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _public_url(self, bucket: str, key: str) -> str:
        if self.cdn_base_url:
            return f"{self.cdn_base_url.rstrip('/')}/{key}"
        return (
            f"https://{bucket}.s3.{self.region}.backblazeb2.com/{key}"
        )

    def _dry_run_url(self, bucket: str, key: str) -> str:
        return f"dry-run://{bucket}/{key}"


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
