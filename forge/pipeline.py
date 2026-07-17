"""
forge/pipeline.py
-----------------
Aku-Content Forge — main Genblaze orchestration pipeline.

Usage (CLI):
    python -m forge.pipeline --subject mathematics --level ss1
    python -m forge.pipeline --subject biology --level ss1 --stages text images
    python -m forge.pipeline --subject mathematics --level ss1 --chapters-only "Number Bases,Indices"

Usage (Python):
    from forge.pipeline import Pipeline
    import yaml, pathlib

    cfg = yaml.safe_load(pathlib.Path("forge/config.yaml").read_text())
    pipe = Pipeline(cfg, repo_root=pathlib.Path("."))
    manifests = pipe.run("mathematics", "ss1")

The pipeline chains four stages in order:
  1. Text    — textbook JSON/MD + flashcards + quizzes (Genblaze → LLM)
  2. Images  — diagrams + thumbnails (Genblaze → Stability / DALL-E)
  3. Audio   — multilingual narration + flashcard audio (Genblaze → ElevenLabs)
  4. Video   — concept explainer clips (Genblaze → Runway / Luma)

All binary assets are stored in Backblaze B2.  The final manifest JSON is
written to ``content/textbooks/<subject>/<level>/manifest.json`` and
uploaded to B2 as the canonical index for Akudemy / Aku-EdgeHub.

Environment variables required:
  B2_APPLICATION_KEY_ID    — Backblaze B2 key ID
  B2_APPLICATION_KEY       — Backblaze B2 application key
  GENBLAZE_API_KEY         — Genblaze API key (set in env or ~/.genblaze/config)
  OPENAI_API_KEY           — if using OpenAI provider
  STABILITY_API_KEY        — if using Stability AI provider
  ELEVENLABS_API_KEY       — if using ElevenLabs provider
  RUNWAY_API_KEY           — if using Runway provider
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from forge.stages.audio import AudioStage
from forge.stages.images import ImageStage
from forge.stages.text import TextStage
from forge.stages.video import VideoStage
from forge.storage.b2 import B2Client

logger = logging.getLogger(__name__)

STAGE_NAMES = ("text", "images", "audio", "video")


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

class Pipeline:
    """Orchestrates all four generation stages for a subject/level pair."""

    def __init__(
        self,
        cfg: dict[str, Any],
        repo_root: Path | None = None,
    ) -> None:
        self.cfg = cfg
        self.repo_root = repo_root or Path(".")
        self.b2 = B2Client(cfg)
        self._text  = TextStage(cfg, self.b2)
        self._images = ImageStage(cfg, self.b2)
        self._audio  = AudioStage(cfg, self.b2)
        self._video  = VideoStage(cfg, self.b2)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        subject: str,
        level: str,
        stages: tuple[str, ...] = STAGE_NAMES,
        chapters_override: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Run the full (or partial) pipeline for *subject* / *level*.

        Args:
            subject:            e.g. ``"mathematics"``
            level:              e.g. ``"ss1"``
            stages:             Subset of ``('text', 'images', 'audio', 'video')``
                                to run. Defaults to all four.
            chapters_override:  Optional explicit list of topic names to
                                generate instead of the NERDC curriculum map.

        Returns:
            List of per-chapter manifest dicts with all B2 URLs populated.
        """
        logger.info(
            "Pipeline.run: subject=%s level=%s stages=%s",
            subject, level, stages
        )

        # Ensure B2 buckets exist before uploading anything
        self.b2.ensure_buckets()

        manifests: list[dict[str, Any]] = []

        if "text" in stages:
            manifests = self._text.run(
                subject, level, self.repo_root, chapters_override
            )
        else:
            # Load existing manifests from disk if skipping text stage
            manifests = self._load_existing_manifests(subject, level)

        if "images" in stages:
            self._images.run(manifests, self.repo_root)

        if "audio" in stages:
            self._audio.run(manifests, self.repo_root)

        if "video" in stages:
            self._video.run(manifests, self.repo_root)

        self._write_manifest(subject, level, manifests)
        return manifests

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _write_manifest(
        self,
        subject: str,
        level: str,
        manifests: list[dict[str, Any]],
    ) -> None:
        """Write the final manifest JSON locally and upload to B2."""
        tb_dir = (
            self.repo_root
            / self.cfg["output"]["textbooks"]
            / subject
            / level
        )
        tb_dir.mkdir(parents=True, exist_ok=True)

        final_manifest = {
            "subject": subject,
            "level": level,
            "chapter_count": len(manifests),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "generator": "aku-content-forge",
            "chapters": manifests,
        }

        manifest_path = tb_dir / "manifest.json"
        manifest_path.write_text(
            json.dumps(final_manifest, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        logger.info("Manifest written: %s", manifest_path)

        # Upload to B2
        b2_key = f"{subject}/{level}/manifest.json"
        url = self.b2.upload_text(
            json.dumps(final_manifest, ensure_ascii=False),
            "textbooks",
            b2_key,
            "application/json",
        )
        logger.info("Manifest uploaded: %s", url)

    def _load_existing_manifests(
        self, subject: str, level: str
    ) -> list[dict[str, Any]]:
        """Load chapter manifests from an existing manifest.json on disk."""
        manifest_path = (
            self.repo_root
            / self.cfg["output"]["textbooks"]
            / subject
            / level
            / "manifest.json"
        )
        if not manifest_path.exists():
            logger.warning(
                "No existing manifest found at %s — returning empty list",
                manifest_path,
            )
            return []
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        return data.get("chapters", [])


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m forge.pipeline",
        description="Aku-Content Forge — multimodal content generation pipeline",
    )
    p.add_argument(
        "--subject",
        required=True,
        help="Subject slug (e.g. mathematics, biology)",
    )
    p.add_argument(
        "--level",
        required=True,
        help="Class level (e.g. ss1, ss2, ss3)",
    )
    p.add_argument(
        "--stages",
        nargs="+",
        choices=list(STAGE_NAMES),
        default=list(STAGE_NAMES),
        help="Stages to run (default: all)",
    )
    p.add_argument(
        "--chapters-only",
        dest="chapters_only",
        default="",
        help="Comma-separated list of topic names to generate (overrides curriculum map)",
    )
    p.add_argument(
        "--config",
        default="forge/config.yaml",
        help="Path to config.yaml (default: forge/config.yaml)",
    )
    p.add_argument(
        "--repo-root",
        dest="repo_root",
        default=".",
        help="Repository root directory (default: .)",
    )
    p.add_argument(
        "--log-level",
        dest="log_level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (default: INFO)",
    )
    return p


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        stream=sys.stdout,
    )

    config_path = Path(args.config)
    if not config_path.exists():
        parser.error(f"Config file not found: {config_path}")

    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    repo_root = Path(args.repo_root).resolve()

    chapters_override = (
        [t.strip() for t in args.chapters_only.split(",") if t.strip()]
        if args.chapters_only
        else None
    )

    pipe = Pipeline(cfg, repo_root)
    manifests = pipe.run(
        subject=args.subject,
        level=args.level,
        stages=tuple(args.stages),
        chapters_override=chapters_override,
    )

    print(
        f"\n✅  Done — {len(manifests)} chapters generated "
        f"for {args.subject}/{args.level}"
    )


if __name__ == "__main__":
    main()
