"""
forge/stages/video.py
---------------------
Stage 4 — Video generation.

For each chapter manifest this stage:
  1. Builds a short script (30–60 s) from the chapter introduction and key terms.
  2. Calls Genblaze → Runway (or Luma) to generate a concept explainer clip.
  3. Uploads the .mp4 to the ``aku-video`` B2 bucket.
  4. Patches the chapter manifest with ``b2.video`` URLs.

Supported providers (set in config.yaml):
  runway  — Runway Gen-3 Alpha Turbo
  luma    — Luma Dream Machine

Note: Video generation is billed per second. The pipeline defaults to a
45-second clip per chapter; adjust ``duration_seconds`` in config.yaml.
For the hackathon demo a single chapter video is sufficient.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import genblaze  # type: ignore[import-untyped]

from forge.storage.b2 import B2Client, build_provenance

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Script builder
# ---------------------------------------------------------------------------

_SCRIPT_TEMPLATE = """\
Create an engaging 45-second educational explainer video script for a Nigerian \
high-school student.

Subject: {subject}
Level: {level}
Topic: {topic}

Opening (5 s): hook question or surprising fact.
Explanation (25 s): clear 3-point breakdown of the core concept.
Worked example (10 s): one concrete real-world example from West Africa.
Closing (5 s): key takeaway + call to action ("Try the practice questions!").

Visual style: bright, animated infographic. No talking head. On-screen text \
for key terms. Upbeat background music.

Return the script as a single paragraph of visual/narration directions that \
can be passed directly to a text-to-video API.
"""

# ---------------------------------------------------------------------------
# VideoStage
# ---------------------------------------------------------------------------

class VideoStage:
    """Generate concept explainer video clips for each chapter."""

    def __init__(self, cfg: dict[str, Any], b2: B2Client) -> None:
        self.cfg = cfg
        self.b2 = b2
        vid_cfg = cfg["providers"]["video"]
        self.provider = vid_cfg["provider"]
        self.model = vid_cfg["model"]
        self.duration = vid_cfg.get("duration_seconds", 45)
        self.resolution = vid_cfg.get("resolution", "1280x720")
        self._client = genblaze.Client()

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(
        self,
        manifests: list[dict[str, Any]],
        repo_root: Path,  # noqa: ARG002 — reserved for local caching
    ) -> list[dict[str, Any]]:
        """Enrich each manifest with video URLs. Modifies in-place."""
        for manifest in manifests:
            self._process_chapter(manifest)
        return manifests

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _process_chapter(self, manifest: dict[str, Any]) -> None:
        subject = manifest["subject"]
        level   = manifest["level"]
        chapter = manifest["chapter_number"]
        topic   = manifest["topic"]
        slug    = manifest["slug"]

        logger.info(
            "  VideoStage ch%02d: %s/%s — %s", chapter, subject, level, topic
        )

        # Build the video prompt / script
        chapter_json_path = Path(manifest["local"]["chapter_json"])
        if chapter_json_path.exists():
            chapter_data = json.loads(
                chapter_json_path.read_text(encoding="utf-8")
            )
            extra_context = chapter_data.get("introduction", "")[:500]
        else:
            extra_context = ""

        script_prompt = _SCRIPT_TEMPLATE.format(
            subject=subject.replace("_", " ").title(),
            level=level.upper(),
            topic=topic,
        )
        if extra_context:
            script_prompt += f"\n\nContext from textbook: {extra_context}"

        # Optional: thumbnail image as the first frame seed
        thumbnail_url = (
            manifest.get("b2", {})
            .get("images", {})
            .get("thumbnail", "")
        )

        video_bytes = self._generate_video(script_prompt, thumbnail_url)

        prov = build_provenance(
            "video", self.provider, self.model, script_prompt[:300],
            {"duration_seconds": self.duration, "resolution": self.resolution}
        )
        b2_key = f"{subject}/{level}/ch{chapter:02d}_{slug}.mp4"
        url = self.b2.upload_bytes(
            video_bytes, "video", b2_key, "video/mp4", prov
        )
        manifest.setdefault("b2", {})["video"] = {"explainer": url}
        logger.info("    Video uploaded: %s", url)

    def _generate_video(self, prompt: str, image_url: str = "") -> bytes:
        kwargs: dict[str, Any] = {
            "provider": self.provider,
            "model": self.model,
            "prompt": prompt,
            "duration": self.duration,
        }
        if image_url:
            kwargs["image_url"] = image_url
        response = self._client.generate_video(**kwargs)
        return response.video_bytes
