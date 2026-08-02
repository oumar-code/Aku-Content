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
import importlib.util
import json
import logging
import os
import shutil
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


def _load_genblaze_api_key_from_config() -> str:
    """Load GENBLAZE_API_KEY from env or the standard Genblaze config files."""
    value = os.environ.get("GENBLAZE_API_KEY", "").strip()
    if value:
        return value

    candidate_paths = [
        Path.home() / ".genblaze" / "config",
        Path.home() / ".genblaze" / "config.yaml",
        Path.home() / ".genblaze" / "config.yml",
        Path.home() / ".genblaze" / "config.json",
        Path.home() / ".config" / "genblaze" / "config",
    ]

    for path in candidate_paths:
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8")
            if not text.strip():
                continue

            data = yaml.safe_load(text) or {}
            if isinstance(data, dict):
                for key in ("api_key", "api-key", "token", "key"):
                    value = data.get(key)
                    if isinstance(value, str) and value.strip():
                        return value.strip()
            if isinstance(data, str) and data.strip():
                return data.strip()

            for line in text.splitlines():
                if "=" not in line:
                    continue
                left, right = line.split("=", 1)
                if left.strip().lower() in {"api_key", "api-key", "token", "key"}:
                    value = right.strip().strip('"').strip("'")
                    if value:
                        return value
        except Exception:
            continue

    return ""


def _ensure_genblaze_api_key() -> str:
    key = _load_genblaze_api_key_from_config()
    if key:
        os.environ["GENBLAZE_API_KEY"] = key
    return os.environ.get("GENBLAZE_API_KEY", "").strip()


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

class Pipeline:
    """Orchestrates all four generation stages for a subject/level pair."""

    def __init__(
        self,
        cfg: dict[str, Any],
        repo_root: Path | None = None,
        dry_run: bool = False,
    ) -> None:
        self.cfg = cfg
        self.repo_root = repo_root or Path(".")
        self.dry_run = dry_run
        self.b2 = B2Client(cfg, local_only=dry_run)
        self._text  = TextStage(cfg, self.b2, dry_run=dry_run)
        self._images = ImageStage(cfg, self.b2, dry_run=dry_run)
        self._audio  = AudioStage(cfg, self.b2, dry_run=dry_run)
        self._video  = VideoStage(cfg, self.b2, dry_run=dry_run)

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

        # Upload to B2 unless running locally in dry-run mode.
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

def _build_run_parser() -> argparse.ArgumentParser:
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
    p.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Generate local preview files only, skip B2 uploads and provider calls, "
            "and write prompt previews alongside chapter output"
        ),
    )
    return p


def _build_preflight_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m forge.pipeline preflight",
        description="Check Forge dependencies, credentials, voices, and B2 access",
    )
    p.add_argument(
        "--stages",
        nargs="+",
        choices=list(STAGE_NAMES),
        default=list(STAGE_NAMES),
        help="Stages to validate (default: all)",
    )
    p.add_argument(
        "--config",
        default="forge/config.yaml",
        help="Path to config.yaml (default: forge/config.yaml)",
    )
    p.add_argument(
        "--log-level",
        dest="log_level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (default: INFO)",
    )
    return p


def _build_cleanup_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m forge.pipeline cleanup",
        description="Remove local dry-run preview outputs",
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


def _required_env_vars(
    cfg: dict[str, Any],
    stages: tuple[str, ...],
) -> list[tuple[tuple[str, ...], str]]:
    env_vars: list[tuple[tuple[str, ...], str]] = [
        (("B2_APPLICATION_KEY_ID",), "Backblaze B2 application key ID"),
        (("B2_APPLICATION_KEY",), "Backblaze B2 application key"),
    ]

    provider_env_map = {
        "text": {
            "openai": (("OPENAI_API_KEY",), "OpenAI API key"),
            "google": (("GOOGLE_API_KEY",), "Google API key"),
            "anthropic": (("ANTHROPIC_API_KEY",), "Anthropic API key"),
        },
        "images": {
            "stability": (("STABILITY_API_KEY",), "Stability API key"),
            "openai": (("OPENAI_API_KEY",), "OpenAI API key"),
        },
        "audio": {
            "elevenlabs": (("ELEVENLABS_API_KEY",), "ElevenLabs API key"),
        },
        "video": {
            "runway": (
                ("RUNWAY_API_KEY", "RUNWAYML_API_SECRET"),
                "Runway API key",
            ),
            "luma": (("LUMA_API_KEY",), "Luma API key"),
        },
    }

    for stage in stages:
        provider = cfg["providers"][stage]["provider"]
        provider_var = provider_env_map.get(stage, {}).get(provider)
        if provider_var:
            env_vars.append(provider_var)

    return env_vars


def _check_dependencies(
    cfg: dict[str, Any],
    stages: tuple[str, ...],
) -> list[tuple[str, bool, str]]:
    modules = {
        "yaml": "pyyaml",
        "genblaze": "genblaze",
        "boto3": "boto3",
        "botocore": "botocore",
    }
    results = [
        (module, importlib.util.find_spec(module) is not None, package)
        for module, package in modules.items()
    ]

    provider_modules = {
        ("text", "openai"): ("genblaze_openai", "genblaze-openai"),
        ("images", "openai"): ("genblaze_openai", "genblaze-openai"),
        ("audio", "elevenlabs"): (
            "genblaze_elevenlabs",
            "genblaze-elevenlabs",
        ),
        ("video", "runway"): ("genblaze_runway", "genblaze-runway"),
    }

    for stage in stages:
        provider = cfg["providers"][stage]["provider"]
        adapter = provider_modules.get((stage, provider))
        if adapter is None:
            results.append(
                (
                    f"{stage}:{provider}",
                    False,
                    "no provider adapter mapping configured in preflight",
                )
            )
            continue
        module, package = adapter
        results.append(
            (module, importlib.util.find_spec(module) is not None, package)
        )

    return results


def _check_voice_ids(cfg: dict[str, Any]) -> list[tuple[str, bool, bool, str]]:
    audio_cfg = cfg.get("providers", {}).get("audio", {})
    voices = audio_cfg.get("voices", {})
    results: list[tuple[str, bool, bool, str]] = []
    for lang in cfg.get("narration_languages", ["en"]):
        value = voices.get(lang, "")
        required = lang == "en"
        ok = bool(value.strip()) or not required
        results.append((lang, required, ok, value))
    return results


def run_preflight(cfg: dict[str, Any], stages: tuple[str, ...]) -> int:
    _ensure_genblaze_api_key()
    print("\nForge preflight\n")

    dep_results = _check_dependencies(cfg, stages)
    dep_failed = False
    print("Dependencies:")
    for module, ok, package in dep_results:
        status = "OK" if ok else "MISSING"
        print(f"  [{status}] {module} (pip install {package})")
        dep_failed = dep_failed or not ok

    print("\nEnvironment:")
    env_failed = False
    for env_names, label in _required_env_vars(cfg, stages):
        ok = any(bool(os.environ.get(env_var, "")) for env_var in env_names)
        status = "OK" if ok else "MISSING"
        print(f"  [{status}] {' or '.join(env_names)} - {label}")
        env_failed = env_failed or not ok

    voice_failed = False
    if "audio" in stages:
        print("\nVoice IDs:")
        for lang, required, ok, value in _check_voice_ids(cfg):
            status = "OK" if ok else "MISSING"
            suffix = value if value else ("not configured" if required else "optional; skipped")
            print(f"  [{status}] {lang} - {suffix}")
            voice_failed = voice_failed or (required and not ok)

    bucket_failed = False
    print("\nBucket access:")
    if dep_failed or env_failed:
        print("  [SKIPPED] Resolve missing dependencies and environment variables first")
        bucket_failed = True
    else:
        try:
            b2 = B2Client(cfg)
            access = b2.check_access()
            for asset_type, status in access["buckets"].items():
                state = "OK" if status["exists"] else "MISSING"
                print(f"  [{state}] {asset_type}: {status['bucket']}")
            bucket_failed = not access["ok"]
        except Exception as exc:  # pragma: no cover - runtime credential errors
            print(f"  [ERROR] Unable to verify buckets: {exc}")
            bucket_failed = True

    failed = dep_failed or env_failed or voice_failed or bucket_failed
    print("\nResult:")
    print("  FAIL" if failed else "  PASS")
    return 1 if failed else 0


def run_cleanup(cfg: dict[str, Any], repo_root: Path) -> int:
    textbooks_root = repo_root / cfg["output"]["textbooks"]
    dry_run_dirs = sorted(
        path for path in textbooks_root.rglob("_dry_run") if path.is_dir()
    )

    print("\nForge cleanup\n")
    if not dry_run_dirs:
        print("No _dry_run directories found.")
        return 0

    for path in dry_run_dirs:
        shutil.rmtree(path)
        print(f"Removed {path}")

    print(f"\nRemoved {len(dry_run_dirs)} dry-run directories.")
    return 0


def main(argv: list[str] | None = None) -> None:
    argv = list(argv or sys.argv[1:])

    if argv and argv[0] == "preflight":
        parser = _build_preflight_parser()
        args = parser.parse_args(argv[1:])

        logging.basicConfig(
            level=getattr(logging, args.log_level),
            format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
            stream=sys.stdout,
        )

        config_path = Path(args.config)
        if not config_path.exists():
            parser.error(f"Config file not found: {config_path}")

        _ensure_genblaze_api_key()
        cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        raise SystemExit(run_preflight(cfg, tuple(args.stages)))

    if argv and argv[0] == "cleanup":
        parser = _build_cleanup_parser()
        args = parser.parse_args(argv[1:])

        logging.basicConfig(
            level=getattr(logging, args.log_level),
            format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
            stream=sys.stdout,
        )

        config_path = Path(args.config)
        if not config_path.exists():
            parser.error(f"Config file not found: {config_path}")

        _ensure_genblaze_api_key()
        cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        repo_root = Path(args.repo_root).resolve()
        raise SystemExit(run_cleanup(cfg, repo_root))

    parser = _build_run_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        stream=sys.stdout,
    )

    config_path = Path(args.config)
    if not config_path.exists():
        parser.error(f"Config file not found: {config_path}")

    _ensure_genblaze_api_key()
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    repo_root = Path(args.repo_root).resolve()

    chapters_override = (
        [t.strip() for t in args.chapters_only.split(",") if t.strip()]
        if args.chapters_only
        else None
    )

    pipe = Pipeline(cfg, repo_root, dry_run=args.dry_run)
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
