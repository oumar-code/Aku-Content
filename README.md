# Aku-Content

Offline content library for the Aku Platform — consumed by **Akudemy** and **Aku-EdgeHub**.

## Contents

| Directory | Description |
|-----------|-------------|
| `content/textbooks/` | AI-generated WAEC textbooks — 9 subjects × SS1 (JSON + MD) |
| `content/ar/` | Augmented Reality assets (.glb) |
| `content/vr/` | Virtual Reality scene packages (.unitypackage) |
| `content/simulations/` | Interactive science simulations |
| `content/flashcards/` | Subject flashcard decks |
| `content/quizzes/` | Formative assessment question banks |
| `content/games/` | Gamified learning modules |
| `content/encyclopedia/` | Offline Wikipedia (.zim) |
| `content/tools/` | Interactive learning tools |
| `content/news_corpus/` | Multilingual news corpus (EN/HA/YO) + translation corpora |
| `content_templates/` | WAEC/NERDC lesson CSV templates (9 subjects) |
| `data/exam_papers/bece/` | BECE / Common Entrance past questions (JSON) |
| `data/exam_papers/waec/` | WAEC / SSCE past papers (JSON + PDF) |

> **Status**: Directory structure initialised. Actual content pending full migration.
> See [migration tracker](https://github.com/oumar-code/Akulearn_docs/blob/main/docs/ecosystem-map.md).

## Contributing / Migrating Content

See [CONTRIBUTING.md](CONTRIBUTING.md) for step-by-step instructions on copying textbooks, content templates, and exam papers from your local machine into this repo.

## Git LFS

Binary assets (`.glb`, `.unitypackage`, `.pdf`, `.mp4`, `.zip`, `.zim`) are tracked via **Git LFS**.
Run `git lfs install` and `git lfs pull` after cloning.

## Usage in Other Repos

Clone or submodule this repo to make content available offline:

```bash
# As a submodule
git submodule add https://github.com/oumar-code/Aku-Content content
git lfs install && git lfs pull
```

## Aku-Content Forge (Generative Pipeline)

The [`forge/`](forge/) directory contains the **Aku-Content Forge** pipeline — a
[Genblaze](https://github.com/Backblaze/genblaze)-powered tool that generates all
content in this library automatically:

| What it generates | Provider |
|-------------------|----------|
| Textbook chapters, flashcards, quizzes (JSON + MD) | OpenAI / Google via Genblaze |
| Diagrams and thumbnails (.png) | Stability AI / DALL-E 3 via Genblaze |
| Multilingual narration EN/HA/YO (.mp3) | ElevenLabs via Genblaze |
| Concept explainer videos (.mp4) | Runway / Luma via Genblaze |

All binary assets are stored in **Backblaze B2** (replacing Git LFS for binary files)
and referenced by CDN URL in each chapter's `manifest.json`.

```bash
# Generate Mathematics SS1 — all stages
python -m forge.pipeline --subject mathematics --level ss1
```

See [forge/README.md](forge/README.md) for full setup and usage instructions.

---

## Source

Migrated from `Akulearn_docs` monorepo — see [migration tracker](https://github.com/oumar-code/Akulearn_docs/blob/main/docs/ecosystem-map.md).
