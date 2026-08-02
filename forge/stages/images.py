"""
forge/stages/images.py
----------------------
Stage 2 — Image generation.

For each chapter in the text-stage manifest, this stage:
  1. Extracts ``[DIAGRAM: ...]`` hints from the chapter JSON.
  2. Calls Genblaze → Stability AI (or DALL-E 3) to render each diagram.
  3. Generates a topic thumbnail image.
  4. Uploads all images to the ``aku-images`` B2 bucket.
  5. Patches the chapter manifest with ``b2.images`` URLs so downstream
     stages (audio, video) and the final manifest reference them.

Supported providers (set in config.yaml):
  stability  — Stability AI (stable-diffusion-3-medium)
  openai     — OpenAI DALL-E 3
"""

from __future__ import annotations

import json
import importlib
import logging
from pathlib import Path
from typing import Any

from forge.storage.b2 import B2Client, build_provenance

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prompt helpers
# ---------------------------------------------------------------------------

_DIAGRAM_SYSTEM = (
    "You are a scientific illustrator creating clear, labelled diagrams for "
    "a West African high-school textbook. Style: clean vector-style on a white "
    "background, accurate labels, educational, no cartoons."
)

_THUMBNAIL_TEMPLATE = (
    "A clean, colourful educational poster thumbnail for a Nigerian SS textbook chapter. "
    "Topic: {topic}. Subject: {subject}. "
    "Bold title text, simple icons, bright colours, academic style, white background."
)


def _diagram_prompt(hint: str, subject: str, topic: str) -> str:
    description = hint.replace("[DIAGRAM:", "").rstrip("]").strip()
    return (
        f"Educational diagram for a Nigerian high-school {subject} textbook. "
        f"Topic: {topic}. Diagram: {description}. "
        "Clean white background, accurate scientific labels, vector illustration style."
    )


# ---------------------------------------------------------------------------
# ImageStage
# ---------------------------------------------------------------------------

class ImageStage:
    """Render diagrams and thumbnails for each chapter manifest."""

    def __init__(
        self,
        cfg: dict[str, Any],
        b2: B2Client,
        dry_run: bool = False,
    ) -> None:
        self.cfg = cfg
        self.b2 = b2
        self.dry_run = dry_run
        img_cfg = cfg["providers"]["images"]
        self.provider = img_cfg["provider"]
        self.model = img_cfg["model"]
        self.width = img_cfg.get("width", 1024)
        self.height = img_cfg.get("height", 1024)
        self.steps = img_cfg.get("steps", 30)
        self._client: Any | None = None
        if not self.dry_run:
            self._client = importlib.import_module("genblaze").Client()

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(
        self,
        manifests: list[dict[str, Any]],
        repo_root: Path,  # noqa: ARG002 — reserved for future local save
    ) -> list[dict[str, Any]]:
        """Enrich each chapter manifest with image URLs.

        Modifies manifests in-place and returns them.
        """
        for manifest in manifests:
            self._process_chapter(manifest, repo_root)
        return manifests

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _process_chapter(self, manifest: dict[str, Any], repo_root: Path) -> None:
        subject = manifest["subject"]
        level   = manifest["level"]
        chapter = manifest["chapter_number"]
        topic   = manifest["topic"]
        slug    = manifest["slug"]

        logger.info(
            "  ImageStage ch%02d: %s/%s — %s", chapter, subject, level, topic
        )

        images: dict[str, str | list[str]] = {}

        # ---- Thumbnail --------------------------------------------------
        thumb_prompt = _THUMBNAIL_TEMPLATE.format(
            topic=topic, subject=subject.replace("_", " ").title()
        )
        thumb_key = f"{subject}/{level}/ch{chapter:02d}_{slug}_thumb.png"
        preview_dir = _preview_dir(repo_root, self.cfg, subject, level)
        preview_dir.mkdir(parents=True, exist_ok=True)

        if self.dry_run:
            image_prompts: dict[str, Any] = {
                "thumbnail_prompt": thumb_prompt,
                "diagram_prompts": [],
            }
            images["thumbnail"] = self.b2.upload_bytes(
                b"", "images", thumb_key, "image/png"
            )
        else:
            thumb_bytes = self._generate_image(thumb_prompt)
            prov = build_provenance("images", self.provider, self.model, thumb_prompt)
            images["thumbnail"] = self.b2.upload_bytes(
                thumb_bytes, "images", thumb_key, "image/png", prov
            )

        # ---- Diagrams from chapter JSON ---------------------------------
        chapter_json_path = Path(manifest["local"]["chapter_json"])
        if chapter_json_path.exists():
            chapter_data = json.loads(chapter_json_path.read_text(encoding="utf-8"))
            diagram_urls: list[str] = []
            for sec_idx, section in enumerate(chapter_data.get("sections", []), start=1):
                for diag_idx, hint in enumerate(
                    section.get("diagram_hints", []), start=1
                ):
                    if not hint.startswith("[DIAGRAM:"):
                        continue
                    diag_prompt = _diagram_prompt(hint, subject, topic)
                    diag_key = (
                        f"{subject}/{level}/ch{chapter:02d}_{slug}"
                        f"_sec{sec_idx}_diag{diag_idx}.png"
                    )
                    if self.dry_run:
                        image_prompts["diagram_prompts"].append(
                            {
                                "section": sec_idx,
                                "diagram_index": diag_idx,
                                "prompt": diag_prompt,
                                "object_key": diag_key,
                            }
                        )
                        url = self.b2.upload_bytes(b"", "images", diag_key, "image/png")
                    else:
                        diag_bytes = self._generate_image(diag_prompt)
                        diag_prov = build_provenance(
                            "images", self.provider, self.model, diag_prompt,
                            {"section": sec_idx, "diagram_index": diag_idx}
                        )
                        url = self.b2.upload_bytes(
                            diag_bytes, "images", diag_key, "image/png", diag_prov
                        )
                    diagram_urls.append(url)
                    logger.debug("    Diagram uploaded: %s", url)
            images["diagrams"] = diagram_urls

        manifest.setdefault("b2", {})["images"] = images
        if self.dry_run:
            prompt_path = preview_dir / f"ch{chapter:02d}_{slug}_images_prompts.json"
            prompt_path.write_text(
                json.dumps(image_prompts, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            manifest.setdefault("dry_run", {})["images_prompts"] = str(prompt_path)

    def _generate_image(self, prompt: str) -> bytes:
        if self._client is None:
            raise RuntimeError("Genblaze client is unavailable in dry-run mode")
        response = self._client.generate_image(
            provider=self.provider,
            model=self.model,
            prompt=prompt,
            width=self.width,
            height=self.height,
            steps=self.steps,
        )
        return response.image_bytes


def _preview_dir(
    repo_root: Path,
    cfg: dict[str, Any],
    subject: str,
    level: str,
) -> Path:
    return repo_root / cfg["output"]["textbooks"] / subject / level / "_dry_run"
