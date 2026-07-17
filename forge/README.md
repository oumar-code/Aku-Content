# Aku-Content Forge

> **Backblaze Generative AI Media Hackathon submission**

Aku-Content Forge is a [Genblaze](https://github.com/Backblaze/genblaze)-powered
pipeline that generates **offline-ready multimedia educational content** for
West African students. Given a subject and grade level, it produces complete
textbook chapters, illustrated diagrams, multilingual audio narration
(English / Hausa / Yoruba), and short concept-explainer videos — storing and
serving everything through **Backblaze B2 Cloud Storage**.

This directly populates the
[Aku-Content](https://github.com/oumar-code/Aku-Content) library that powers
**Akudemy** and **Aku-EdgeHub**, bringing AI-generated WAEC/BECE curriculum to
students in low-connectivity regions of West Africa at near-zero marginal cost.

---

## The Problem We Solve

Over **10 million** West African students sit WAEC and BECE exams every year.
Most study from outdated, scarce physical textbooks. Akudemy delivers these
students an offline-first learning platform — but hand-authoring thousands of
chapters, flashcards, quizzes, diagrams, and audio clips is impossibly slow.

**Aku-Content Forge makes content generation as fast as inference.**

---

## Architecture

```
                       ┌─────────────────────────────────────────┐
                       │           Aku-Content Forge              │
                       │                                          │
  User CLI/API         │  Stage 1 — Text                          │
  ─────────────►       │    Genblaze → OpenAI / Google            │
  subject=mathematics  │    • Textbook chapter (JSON + MD)        │
  level=ss1            │    • Flashcard deck (JSON)               │
                       │    • Quiz question bank (JSON)           │
                       │                                          │
                       │  Stage 2 — Images                        │
                       │    Genblaze → Stability AI / DALL-E 3    │
                       │    • Chapter diagrams (.png)             │
                       │    • Topic thumbnails (.png)             │
                       │                                          │
                       │  Stage 3 — Audio                         │
                       │    Genblaze → ElevenLabs                 │
                       │    • Chapter narration (EN/HA/YO .mp3)  │
                       │    • Flashcard audio pronunciations      │
                       │                                          │
                       │  Stage 4 — Video                         │
                       │    Genblaze → Runway / Luma              │
                       │    • 45 s concept explainer (.mp4)      │
                       └────────────────────┬────────────────────┘
                                            │  all assets + provenance
                                            ▼
                       ┌─────────────────────────────────────────┐
                       │         Backblaze B2 Buckets             │
                       │  aku-textbooks  aku-images               │
                       │  aku-audio      aku-video                │
                       └────────────────────┬────────────────────┘
                                            │  CDN URLs injected
                                            ▼     into manifest.json
                       ┌─────────────────────────────────────────┐
                       │   Aku-Content repo  (this repo)          │
                       │   content/textbooks/<subject>/<level>/   │
                       │   content/flashcards/                    │
                       │   content/quizzes/                       │
                       │   content/news_corpus/<lang>/            │
                       └────────────────────┬────────────────────┘
                                            │
                              ┌─────────────┴─────────────┐
                              ▼                           ▼
                          Akudemy                   Aku-EdgeHub
                     (online platform)        (offline Raspberry Pi hub)
```

---

## Why Backblaze B2?

| Problem | B2 Solution |
|---------|-------------|
| Git LFS 1 GB/month bandwidth cap can't serve audio/video at scale | B2 has no egress fees to Cloudflare CDN — unlimited bandwidth for media |
| Binary assets bloat the git history | All images, audio, video live in B2; only JSON/MD goes in git |
| Provenance tracking for AI-generated content | Provenance records stored as B2 object metadata alongside every asset |
| Offline-first deployment (Raspberry Pi hubs) | EdgeHub syncs selectively from B2 over low-bandwidth connections |

The `aku-textbooks`, `aku-images`, `aku-audio`, and `aku-video` B2 buckets
replace the `git lfs` approach described in the repo's CONTRIBUTING.md,
eliminating the 1 GB bandwidth bottleneck entirely.

---

## Why Genblaze?

The pipeline orchestrates **four different AI providers** in a single run:
OpenAI (text), Stability AI (images), ElevenLabs (audio), and Runway (video).
Genblaze provides a unified Python SDK so we can swap providers in one line of
`config.yaml` without touching pipeline code — critical for a hackathon demo
and for long-term cost optimisation.

---

## Quick Start

### 1. Prerequisites

```bash
python -m pip install -r forge/requirements.txt
```

### 2. Environment variables

```bash
export B2_APPLICATION_KEY_ID="your-b2-key-id"
export B2_APPLICATION_KEY="your-b2-application-key"
export GENBLAZE_API_KEY="your-genblaze-api-key"
export OPENAI_API_KEY="your-openai-key"          # for text stage
export STABILITY_API_KEY="your-stability-key"    # for images stage
export ELEVENLABS_API_KEY="your-elevenlabs-key"  # for audio stage
export RUNWAY_API_KEY="your-runway-key"           # for video stage
```

### 3. Configure voice IDs

Edit `forge/config.yaml` and set your ElevenLabs voice IDs:

```yaml
providers:
  audio:
    voices:
      en: "EXAVITQu4vr4xnSDxMaL"   # English narrator
      ha: "<your-hausa-voice-id>"
      yo: "<your-yoruba-voice-id>"
```

### 4. Run the pipeline

```bash
# Full pipeline — Mathematics SS1 (all 4 stages)
python -m forge.pipeline --subject mathematics --level ss1

# Text + images only (faster for testing)
python -m forge.pipeline --subject biology --level ss1 --stages text images

# Single chapter (fastest demo)
python -m forge.pipeline \
  --subject mathematics --level ss1 \
  --stages text images audio video \
  --chapters-only "Number Bases"

# Full run — all 9 WAEC subjects, SS1
for subject in mathematics biology chemistry physics english_language \
               economics geography government literature_in_english; do
  python -m forge.pipeline --subject $subject --level ss1
done
```

### 5. Python API

```python
import yaml
from pathlib import Path
from forge.pipeline import Pipeline

cfg = yaml.safe_load(Path("forge/config.yaml").read_text())
pipe = Pipeline(cfg, repo_root=Path("."))

# Generate one chapter for a quick demo
manifests = pipe.run(
    "mathematics", "ss1",
    stages=("text", "images", "audio", "video"),
    chapters_override=["Number Bases"],
)

print(manifests[0]["b2"])
# {
#   "chapter_json": "https://aku-textbooks.s3.us-west-004.backblazeb2.com/...",
#   "chapter_md":   "https://aku-textbooks.s3.us-west-004.backblazeb2.com/...",
#   "flashcards":   "https://aku-textbooks.s3.us-west-004.backblazeb2.com/...",
#   "quiz":         "https://aku-textbooks.s3.us-west-004.backblazeb2.com/...",
#   "images":       {"thumbnail": "...", "diagrams": [...]},
#   "audio":        {"narration_en": "...", "narration_ha": "...", "narration_yo": "..."},
#   "video":        {"explainer": "..."}
# }
```

---

## Output Structure

After a full run the following files are created locally and mirrored to B2:

```
content/
├── textbooks/
│   └── mathematics/ss1/
│       ├── ch01_number_bases.json      ← structured chapter data
│       ├── ch01_number_bases.md        ← human-readable markdown
│       ├── manifest.json               ← index with all B2 URLs
│       └── ...
├── flashcards/
│   └── mathematics/ss1/
│       └── ch01_number_bases.json      ← 15 flashcards
├── quizzes/
│   └── mathematics/ss1/
│       └── ch01_number_bases.json      ← 10 MCQ questions
└── news_corpus/
    ├── en/mathematics/ss1/
    │   └── ch01_number_bases.mp3       ← English narration
    ├── ha/mathematics/ss1/
    │   └── ch01_number_bases.mp3       ← Hausa narration
    └── yo/mathematics/ss1/
        └── ch01_number_bases.mp3       ← Yoruba narration
```

B2 buckets mirror this structure plus images and video:

```
aku-textbooks/mathematics/ss1/ch01_number_bases.json
aku-images/mathematics/ss1/ch01_number_bases_thumb.png
aku-images/mathematics/ss1/ch01_number_bases_sec1_diag1.png
aku-audio/mathematics/ss1/ch01_number_bases_en.mp3
aku-audio/mathematics/ss1/ch01_number_bases_ha.mp3
aku-audio/mathematics/ss1/ch01_number_bases_yo.mp3
aku-video/mathematics/ss1/ch01_number_bases.mp4
```

---

## Provenance

Every B2 object carries a `provenance` metadata key containing:

```json
{
  "generated_at": "2026-07-17T09:00:00Z",
  "generator": "aku-content-forge",
  "stage": "text",
  "provider": "openai",
  "model": "gpt-4o",
  "prompt_sha256": "abc123..."
}
```

This satisfies AI content disclosure requirements and enables audit trails
for curriculum quality review.

---

## Supported Subjects

| Subject | WAEC Code | Levels |
|---------|-----------|--------|
| Mathematics | MAT | SS1–SS3 |
| Biology | BIO | SS1–SS3 |
| Chemistry | CHE | SS1–SS3 |
| Physics | PHY | SS1–SS3 |
| English Language | ENG | SS1–SS3 |
| Economics | ECO | SS1–SS3 |
| Geography | GEO | SS1–SS3 |
| Government | GOV | SS1–SS3 |
| Literature in English | LIT | SS1–SS3 |

---

## Repository Integration

This `forge/` directory lives inside
[Aku-Content](https://github.com/oumar-code/Aku-Content) so every pipeline run
can commit the generated JSON/MD files directly to the content library.
Binary assets (images, audio, video) are **not** committed — they stay in B2
and are referenced by CDN URL in `manifest.json`, solving the Git LFS bandwidth
cap described in [CONTRIBUTING.md](../CONTRIBUTING.md).

---

## Hackathon Checklist

- [x] Uses **Backblaze B2** as the primary asset store and CDN backbone
- [x] Uses **Genblaze SDK** as the orchestration layer across all providers
- [x] Generates **video** (Runway / Luma concept explainers)
- [x] Generates **images** (Stability AI diagrams and thumbnails)
- [x] Generates **audio** (ElevenLabs multilingual narration)
- [x] Generates **multimodal text** (OpenAI / Google structured curriculum)
- [x] Real-world use case with social impact (West African edtech)
- [x] Provenance records on every B2 asset
- [x] Open source, documented, and runnable in one command

---

## License

MIT — see [LICENSE](../LICENSE) if present, otherwise consider content
generated by this pipeline as CC BY 4.0 for educational use.
