"""
forge/stages/audio.py
---------------------
Stage 3 — Audio narration.

For each chapter manifest this stage:
  1. Extracts the chapter summary and key terms from the local JSON.
  2. Calls Genblaze → ElevenLabs to narrate the summary in English, Hausa,
     and Yoruba (languages configured in config.yaml).
  3. Generates short audio pronunciations for each flashcard front.
  4. Uploads all .mp3 files to the ``aku-audio`` B2 bucket.
  5. Patches the chapter manifest with ``b2.audio`` URLs.

The multilingual narration files are also written into the repo under
``content/news_corpus/<language>/`` to populate the multilingual corpus
used by Akudemy's offline audio player.

Supported provider: ElevenLabs (via Genblaze).
"""

from __future__ import annotations

import json
import importlib
import logging
from pathlib import Path
from typing import Any

from forge.storage.b2 import B2Client, build_provenance

logger = logging.getLogger(__name__)

# Language → locale label used in intro sentence
_LANG_LABELS = {
    "en": "English",
    "ha": "Hausa",
    "yo": "Yoruba",
}

# Intro sentence prepended before each narration so ElevenLabs knows the
# language context.  Adjust or remove if your voice model is language-aware.
_INTROS = {
    "en": "",
    "ha": "",   # ElevenLabs multilingual_v2 auto-detects language from text
    "yo": "",
}


def _narration_text(chapter_data: dict[str, Any], language: str) -> str:
    """Build the narration script from chapter data.

    For non-English languages we prepend the topic name and use the summary
    only (full chapter translation is out of scope for the hackathon demo;
    a production pipeline would add an LLM translation step here).
    """
    topic = chapter_data.get("topic", "")
    summary = chapter_data.get("summary", "")
    intro = chapter_data.get("introduction", "")
    lang_label = _LANG_LABELS.get(language, language.upper())

    if language == "en":
        return (
            f"Chapter topic: {topic}. "
            f"{intro} "
            f"Summary: {summary}"
        )
    # For ha/yo: narrate in the target language using summary only.
    # In a full pipeline, add an LLM translation step here.
    return (
        f"[{lang_label}] Topic: {topic}. {summary}"
    )


# ---------------------------------------------------------------------------
# AudioStage
# ---------------------------------------------------------------------------

class AudioStage:
    """Narrate chapter summaries and flashcard terms in multiple languages."""

    def __init__(
        self,
        cfg: dict[str, Any],
        b2: B2Client,
        dry_run: bool = False,
    ) -> None:
        self.cfg = cfg
        self.b2 = b2
        self.dry_run = dry_run
        audio_cfg = cfg["providers"]["audio"]
        self.provider = audio_cfg["provider"]
        self.model = audio_cfg["model"]
        self.voices: dict[str, str] = audio_cfg.get("voices", {})
        self.languages: list[str] = cfg.get("narration_languages", ["en"])
        self._client: Any | None = None
        if not self.dry_run:
            self._client = importlib.import_module("genblaze").Client()

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(
        self,
        manifests: list[dict[str, Any]],
        repo_root: Path,
    ) -> list[dict[str, Any]]:
        """Enrich each manifest with audio URLs. Modifies in-place."""
        for manifest in manifests:
            self._process_chapter(manifest, repo_root)
        return manifests

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _process_chapter(
        self, manifest: dict[str, Any], repo_root: Path
    ) -> None:
        subject = manifest["subject"]
        level   = manifest["level"]
        chapter = manifest["chapter_number"]
        topic   = manifest["topic"]
        slug    = manifest["slug"]

        logger.info(
            "  AudioStage ch%02d: %s/%s — %s", chapter, subject, level, topic
        )

        # Load chapter JSON for narration text
        chapter_json_path = Path(manifest["local"]["chapter_json"])
        if not chapter_json_path.exists():
            logger.warning("    Chapter JSON not found — skipping audio")
            return
        chapter_data = json.loads(
            chapter_json_path.read_text(encoding="utf-8")
        )

        audio_urls: dict[str, str | list[str]] = {}
        preview_dir = _preview_dir(repo_root, self.cfg, subject, level)
        preview_dir.mkdir(parents=True, exist_ok=True)
        script_manifest: dict[str, Any] = {
            "narrations": {},
            "flashcards": [],
        }

        # ---- Chapter narrations per language ----------------------------
        for lang in self.languages:
            script = _narration_text(chapter_data, lang)
            voice_id = self.voices.get(lang, "")
            script_path = preview_dir / f"ch{chapter:02d}_{slug}_narration_{lang}.txt"
            script_path.write_text(script, encoding="utf-8")
            script_manifest["narrations"][lang] = {
                "script_path": str(script_path),
                "voice_id": voice_id,
            }

            if self.dry_run:
                continue

            if not voice_id:
                logger.debug(
                    "    No voice ID configured for '%s' — skipping", lang
                )
                continue

            audio_bytes = self._tts(script, voice_id)
            prov = build_provenance(
                "audio", self.provider, self.model, script[:200],
                {"language": lang, "voice_id": voice_id}
            )
            b2_key = (
                f"{subject}/{level}/ch{chapter:02d}_{slug}_{lang}.mp3"
            )
            url = self.b2.upload_bytes(
                audio_bytes, "audio", b2_key, "audio/mpeg", prov
            )
            audio_urls[f"narration_{lang}"] = url

            # Write to news_corpus for offline audio player
            corpus_dir = (
                repo_root
                / self.cfg["output"]["news_corpus"]
                / lang
                / subject
                / level
            )
            corpus_dir.mkdir(parents=True, exist_ok=True)
            audio_path = corpus_dir / f"ch{chapter:02d}_{slug}.mp3"
            audio_path.write_bytes(audio_bytes)
            logger.debug("    Wrote %s", audio_path)

        # ---- Flashcard audio (English only) ----------------------------
        fc_path = Path(manifest["local"]["flashcards"])
        en_voice = self.voices.get("en", "")
        if fc_path.exists():
            fc_data = json.loads(fc_path.read_text(encoding="utf-8"))
            fc_audio_urls: list[str] = []
            for idx, card in enumerate(fc_data.get("flashcards", [])[:10], 1):
                front_text = card.get("front", "")
                if not front_text:
                    continue
                script_manifest["flashcards"].append(
                    {
                        "card_index": idx,
                        "text": front_text,
                    }
                )
                if self.dry_run or not en_voice:
                    continue
                card_bytes = self._tts(front_text, en_voice)
                card_prov = build_provenance(
                    "audio", self.provider, self.model, front_text,
                    {"language": "en", "card_index": idx}
                )
                card_key = (
                    f"flashcards/{subject}/{level}/"
                    f"ch{chapter:02d}_{slug}_card{idx:03d}.mp3"
                )
                card_url = self.b2.upload_bytes(
                    card_bytes, "audio", card_key, "audio/mpeg", card_prov
                )
                fc_audio_urls.append(card_url)
            if fc_audio_urls:
                audio_urls["flashcard_audio"] = fc_audio_urls

        manifest.setdefault("b2", {})["audio"] = audio_urls
        if self.dry_run:
            prompt_path = preview_dir / f"ch{chapter:02d}_{slug}_audio_prompts.json"
            prompt_path.write_text(
                json.dumps(script_manifest, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            manifest.setdefault("dry_run", {})["audio_prompts"] = str(prompt_path)

    def _tts(self, text: str, voice_id: str) -> bytes:
        if self._client is None:
            raise RuntimeError("Genblaze client is unavailable in dry-run mode")
        response = self._client.text_to_speech(
            provider=self.provider,
            model=self.model,
            text=text,
            voice_id=voice_id,
        )
        return response.audio_bytes


def _preview_dir(
    repo_root: Path,
    cfg: dict[str, Any],
    subject: str,
    level: str,
) -> Path:
    return repo_root / cfg["output"]["textbooks"] / subject / level / "_dry_run"
