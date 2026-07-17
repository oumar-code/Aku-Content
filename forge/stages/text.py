"""
forge/stages/text.py
--------------------
Stage 1 — Text generation.

Uses Genblaze to orchestrate calls to an LLM (OpenAI / Google / Anthropic)
and generates three artefacts per chapter:

  1. Textbook chapter  → ``content/textbooks/<subject>/<level>/<slug>.json``
                         ``content/textbooks/<subject>/<level>/<slug>.md``
  2. Flashcard deck    → ``content/flashcards/<subject>/<level>/<slug>.json``
  3. Quiz question bank→ ``content/quizzes/<subject>/<level>/<slug>.json``

Each artefact is also uploaded to the ``aku-textbooks`` B2 bucket and the
returned URL is embedded in the manifest so Akudemy / Aku-EdgeHub can fetch
it over CDN at runtime.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import genblaze  # type: ignore[import-untyped]

from forge.storage.b2 import B2Client, build_provenance

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# NERDC curriculum topic map (Mathematics SS1 seed — extend per subject)
# ---------------------------------------------------------------------------

CURRICULUM: dict[str, dict[str, list[str]]] = {
    "mathematics": {
        "ss1": [
            "Number Bases",
            "Modular Arithmetic",
            "Indices",
            "Logarithms",
            "Sequences and Series",
            "Sets",
            "Quadratic Equations",
            "Logical Reasoning",
            "Plane Geometry",
            "Mensuration",
        ],
        "ss2": [
            "Surds",
            "Matrices and Determinants",
            "Trigonometry",
            "Statistics",
            "Probability",
            "Vectors",
            "Transformation",
            "Coordinate Geometry",
            "Linear Programming",
            "Calculus — Differentiation",
        ],
        "ss3": [
            "Calculus — Integration",
            "Further Trigonometry",
            "Permutation and Combination",
            "Binomial Theorem",
            "Circle Theorems",
            "Further Statistics",
            "Financial Mathematics",
            "Further Vectors",
            "Proofs",
            "Revision — WAEC Focus",
        ],
    },
    "biology": {
        "ss1": [
            "Cell Biology",
            "Classification of Living Things",
            "Nutrition",
            "Transport in Living Things",
            "Excretion",
            "Reproduction",
            "Genetics and Evolution",
            "Ecology",
            "Adaptations",
            "Diseases and Immunity",
        ],
        "ss2": [
            "Further Cell Biology",
            "Plant Kingdom",
            "Animal Kingdom",
            "Human Physiology — Digestion",
            "Human Physiology — Circulation",
            "Human Physiology — Respiration",
            "Coordination and Control",
            "Genetics — Mendelian Inheritance",
            "Population Ecology",
            "Environmental Issues",
        ],
        "ss3": [
            "Evolution",
            "Biotechnology",
            "Applied Biology",
            "Further Genetics",
            "Immunology",
            "Microbiology",
            "Ecology and Conservation",
            "Human Reproduction",
            "Plant Physiology",
            "Revision — WAEC Focus",
        ],
    },
}

# Default fallback topics for subjects not yet in CURRICULUM
_DEFAULT_TOPICS = [f"Topic {i + 1}" for i in range(10)]


def _get_topics(subject: str, level: str, n: int) -> list[str]:
    topics = (
        CURRICULUM.get(subject, {}).get(level, _DEFAULT_TOPICS)
    )
    return topics[:n]


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

_CHAPTER_PROMPT = """\
You are an expert curriculum writer producing content aligned to the Nigerian \
NERDC / WAEC / BECE syllabus.

Write a detailed textbook chapter for:
  Subject : {subject_title}
  Level   : {level_upper} (Senior Secondary School Year {ss_year})
  Topic   : {topic}

The chapter MUST include:
1. Learning Objectives (3–5 bullet points)
2. Introduction / Background (2–3 paragraphs, accessible to a 15-year-old)
3. Core Content sections (3–5 sections, each with a heading, explanation,
   worked examples, and diagrams described as [DIAGRAM: brief description])
4. Key Terms / Glossary (5–10 terms with concise definitions)
5. Summary (one paragraph)
6. Practice Questions (5 questions: 3 objective + 2 theory)
7. Further Reading (2–3 suggested resources)

Tone: clear, encouraging, curriculum-faithful. Avoid jargon without explanation.
Return ONLY valid JSON matching this exact schema — no markdown fences:
{{
  "subject": "{subject}",
  "level": "{level}",
  "chapter_number": {chapter_number},
  "topic": "{topic}",
  "learning_objectives": ["..."],
  "introduction": "...",
  "sections": [
    {{
      "heading": "...",
      "body": "...",
      "worked_examples": ["..."],
      "diagram_hints": ["[DIAGRAM: ...]"]
    }}
  ],
  "key_terms": [{{"term": "...", "definition": "..."}}],
  "summary": "...",
  "practice_questions": [
    {{
      "type": "objective|theory",
      "question": "...",
      "options": ["A", "B", "C", "D"],
      "answer": "A",
      "explanation": "..."
    }}
  ],
  "further_reading": ["..."],
  "generated_at": "{generated_at}",
  "generator": "aku-content-forge/text-stage"
}}
"""

_FLASHCARD_PROMPT = """\
Create a flashcard deck for a Nigerian SS student studying {subject_title} \
({level_upper}), topic: {topic}.

Return ONLY valid JSON — no markdown fences:
{{
  "subject": "{subject}",
  "level": "{level}",
  "topic": "{topic}",
  "flashcards": [
    {{"front": "...", "back": "...", "difficulty": "easy|medium|hard"}}
  ],
  "generated_at": "{generated_at}",
  "generator": "aku-content-forge/text-stage"
}}

Generate 15 flashcards covering key terms, formulas, and exam-style prompts.
"""

_QUIZ_PROMPT = """\
Create a 10-question multiple-choice quiz for a Nigerian SS student studying \
{subject_title} ({level_upper}), topic: {topic}.

Return ONLY valid JSON — no markdown fences:
{{
  "subject": "{subject}",
  "level": "{level}",
  "topic": "{topic}",
  "questions": [
    {{
      "id": "Q001",
      "body": "...",
      "options": ["A. ...", "B. ...", "C. ...", "D. ..."],
      "correct_option": 0,
      "answer": "A. ...",
      "explanation": "...",
      "difficulty": "easy|medium|hard",
      "marks": 1
    }}
  ],
  "generated_at": "{generated_at}",
  "generator": "aku-content-forge/text-stage"
}}
"""


# ---------------------------------------------------------------------------
# Text stage implementation
# ---------------------------------------------------------------------------

class TextStage:
    """Generate textbook, flashcard, and quiz content for one chapter."""

    def __init__(self, cfg: dict[str, Any], b2: B2Client) -> None:
        self.cfg = cfg
        self.b2 = b2
        text_cfg = cfg["providers"]["text"]
        self.provider = text_cfg["provider"]
        self.model = text_cfg["model"]
        self.temperature = text_cfg.get("temperature", 0.7)
        self.max_tokens = text_cfg.get("max_tokens", 4096)
        self._client = genblaze.Client()

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(
        self,
        subject: str,
        level: str,
        repo_root: Path,
        chapters_override: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Generate all chapters for *subject* / *level*.

        Returns a list of chapter manifest dicts (each containing b2 URLs).
        """
        n = self.cfg.get("chapters_per_level", 10)
        topics = chapters_override or _get_topics(subject, level, n)
        logger.info(
            "TextStage: %s/%s — %d chapters", subject, level, len(topics)
        )
        manifests = []
        for idx, topic in enumerate(topics, start=1):
            manifest = self._generate_chapter(
                subject, level, idx, topic, repo_root
            )
            manifests.append(manifest)
        return manifests

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _generate_chapter(
        self,
        subject: str,
        level: str,
        chapter_number: int,
        topic: str,
        repo_root: Path,
    ) -> dict[str, Any]:
        subject_title = subject.replace("_", " ").title()
        level_upper = level.upper()
        ss_year = level[-1] if level.startswith("ss") else "1"
        now = datetime.now(timezone.utc).isoformat()
        slug = _slugify(topic)

        logger.info("  Generating chapter %02d: %s", chapter_number, topic)

        # ---- 1. Textbook chapter ----------------------------------------
        chapter_json = self._llm_call(
            _CHAPTER_PROMPT.format(
                subject=subject,
                subject_title=subject_title,
                level=level,
                level_upper=level_upper,
                ss_year=ss_year,
                topic=topic,
                chapter_number=chapter_number,
                generated_at=now,
            )
        )
        chapter_data = _parse_json(chapter_json)

        # Write local files
        tb_dir = repo_root / self.cfg["output"]["textbooks"] / subject / level
        tb_dir.mkdir(parents=True, exist_ok=True)
        json_path = tb_dir / f"ch{chapter_number:02d}_{slug}.json"
        md_path   = tb_dir / f"ch{chapter_number:02d}_{slug}.md"
        json_path.write_text(json.dumps(chapter_data, indent=2, ensure_ascii=False), encoding="utf-8")
        md_path.write_text(_chapter_to_markdown(chapter_data), encoding="utf-8")

        # Upload to B2
        prov = build_provenance("text", self.provider, self.model, _CHAPTER_PROMPT[:200])
        b2_json_key = f"{subject}/{level}/ch{chapter_number:02d}_{slug}.json"
        b2_md_key   = f"{subject}/{level}/ch{chapter_number:02d}_{slug}.md"
        json_url = self.b2.upload_text(
            json.dumps(chapter_data, ensure_ascii=False),
            "textbooks", b2_json_key, "application/json", prov
        )
        md_url = self.b2.upload_text(
            _chapter_to_markdown(chapter_data),
            "textbooks", b2_md_key, "text/markdown", prov
        )

        # ---- 2. Flashcards ----------------------------------------------
        fc_json = self._llm_call(
            _FLASHCARD_PROMPT.format(
                subject=subject,
                subject_title=subject_title,
                level=level,
                level_upper=level_upper,
                topic=topic,
                generated_at=now,
            )
        )
        fc_data = _parse_json(fc_json)
        fc_dir = repo_root / self.cfg["output"]["flashcards"] / subject / level
        fc_dir.mkdir(parents=True, exist_ok=True)
        fc_path = fc_dir / f"ch{chapter_number:02d}_{slug}.json"
        fc_path.write_text(json.dumps(fc_data, indent=2, ensure_ascii=False), encoding="utf-8")
        fc_url = self.b2.upload_text(
            json.dumps(fc_data, ensure_ascii=False),
            "textbooks", f"flashcards/{subject}/{level}/ch{chapter_number:02d}_{slug}.json",
            "application/json", prov
        )

        # ---- 3. Quizzes -------------------------------------------------
        quiz_json = self._llm_call(
            _QUIZ_PROMPT.format(
                subject=subject,
                subject_title=subject_title,
                level=level,
                level_upper=level_upper,
                topic=topic,
                generated_at=now,
            )
        )
        quiz_data = _parse_json(quiz_json)
        quiz_dir = repo_root / self.cfg["output"]["quizzes"] / subject / level
        quiz_dir.mkdir(parents=True, exist_ok=True)
        quiz_path = quiz_dir / f"ch{chapter_number:02d}_{slug}.json"
        quiz_path.write_text(json.dumps(quiz_data, indent=2, ensure_ascii=False), encoding="utf-8")
        quiz_url = self.b2.upload_text(
            json.dumps(quiz_data, ensure_ascii=False),
            "textbooks", f"quizzes/{subject}/{level}/ch{chapter_number:02d}_{slug}.json",
            "application/json", prov
        )

        manifest = {
            "subject": subject,
            "level": level,
            "chapter_number": chapter_number,
            "topic": topic,
            "slug": slug,
            "b2": {
                "chapter_json": json_url,
                "chapter_md": md_url,
                "flashcards": fc_url,
                "quiz": quiz_url,
            },
            "local": {
                "chapter_json": str(json_path),
                "chapter_md": str(md_path),
                "flashcards": str(fc_path),
                "quiz": str(quiz_path),
            },
        }
        return manifest

    def _llm_call(self, prompt: str) -> str:
        response = self._client.generate(
            provider=self.provider,
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            temperature=self.temperature,
            max_tokens=self.max_tokens,
        )
        return response.text


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _parse_json(raw: str) -> dict[str, Any]:
    """Parse LLM output, stripping any accidental markdown fences."""
    raw = raw.strip()
    raw = re.sub(r"^```[a-z]*\n?", "", raw)
    raw = re.sub(r"\n?```$", "", raw)
    return json.loads(raw)


def _chapter_to_markdown(data: dict[str, Any]) -> str:
    """Convert the structured chapter JSON to a human-readable Markdown file."""
    lines = [
        f"# Chapter {data.get('chapter_number', '?')}: {data.get('topic', '')}",
        "",
        f"**Subject:** {data.get('subject', '').replace('_', ' ').title()}  ",
        f"**Level:** {data.get('level', '').upper()}",
        "",
        "## Learning Objectives",
        "",
    ]
    for obj in data.get("learning_objectives", []):
        lines.append(f"- {obj}")
    lines += ["", "## Introduction", "", data.get("introduction", ""), ""]

    for sec in data.get("sections", []):
        lines += [f"## {sec.get('heading', '')}", "", sec.get("body", ""), ""]
        examples = sec.get("worked_examples", [])
        if examples:
            lines += ["**Worked Examples**", ""]
            for ex in examples:
                lines.append(f"> {ex}")
            lines.append("")
        for hint in sec.get("diagram_hints", []):
            lines.append(f"_{hint}_")
        lines.append("")

    lines += ["## Key Terms", ""]
    for kt in data.get("key_terms", []):
        lines.append(f"**{kt.get('term', '')}**: {kt.get('definition', '')}")
    lines += ["", "## Summary", "", data.get("summary", ""), "", "## Practice Questions", ""]
    for i, q in enumerate(data.get("practice_questions", []), 1):
        lines.append(f"{i}. {q.get('question', '')}")
        for opt in q.get("options", []):
            lines.append(f"   - {opt}")
        lines.append(f"   *Answer: {q.get('answer', '')}*")
        lines.append("")

    lines += ["## Further Reading", ""]
    for ref in data.get("further_reading", []):
        lines.append(f"- {ref}")

    return "\n".join(lines)
