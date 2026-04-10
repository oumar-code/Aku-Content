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
| `content_templates/` | WAEC/NERDC lesson CSV templates (8 subjects) |

> **Status**: Directory structure initialised. Actual content pending full migration.
> See [migration tracker](https://github.com/oumar-code/Akulearn_docs/blob/main/docs/ecosystem-map.md).

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

## Source

Migrated from `Akulearn_docs` monorepo — see [migration tracker](https://github.com/oumar-code/Akulearn_docs/blob/main/docs/ecosystem-map.md).
