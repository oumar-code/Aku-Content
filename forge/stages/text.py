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
import importlib
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
    "chemistry": {
        "ss1": [
            "Introduction to Chemistry",
            "Particulate Nature of Matter",
            "Atomic Structure",
            "Chemical Symbols, Formulae and Equations",
            "Kinetic Theory of Matter",
            "Gas Laws",
            "Acids, Bases and Salts",
            "Water and Solutions",
            "Separation Techniques",
            "Revision - WAEC Focus",
        ],
        "ss2": [
            "Periodic Table and Periodicity",
            "Chemical Bonding",
            "Redox Reactions",
            "Electrolysis",
            "Energy Changes in Reactions",
            "Rates of Chemical Reactions",
            "Chemical Equilibrium",
            "Carbon and Its Compounds",
            "Metals and Their Compounds",
            "Revision - WAEC Focus",
        ],
        "ss3": [
            "Hydrocarbons",
            "Alkanols and Ethers",
            "Alkanals and Alkanones",
            "Alkanoic Acids and Esters",
            "Amines and Amides",
            "Polymers",
            "Applied Chemistry",
            "Nuclear Chemistry",
            "Qualitative Analysis",
            "Revision - WAEC Focus",
        ],
    },
    "physics": {
        "ss1": [
            "Measurement and Units",
            "Scalars and Vectors",
            "Motion",
            "Forces",
            "Equilibrium of Forces",
            "Work, Energy and Power",
            "Simple Machines",
            "Heat Energy",
            "Properties of Matter",
            "Revision - WAEC Focus",
        ],
        "ss2": [
            "Waves",
            "Sound",
            "Light",
            "Electrostatics",
            "Current Electricity",
            "Magnetism",
            "Electromagnetism",
            "Electric Cells and Circuits",
            "Heat Transfer",
            "Revision - WAEC Focus",
        ],
        "ss3": [
            "Mechanics",
            "Projectile Motion",
            "Gravitational Field",
            "Fluid Mechanics",
            "Atomic Physics",
            "Radioactivity",
            "Electronics",
            "Alternating Current",
            "Introductory Modern Physics",
            "Revision - WAEC Focus",
        ],
    },
    "english_language": {
        "ss1": [
            "Parts of Speech",
            "Sentence Structure",
            "Comprehension Skills",
            "Vocabulary Development",
            "Tenses and Concord",
            "Continuous Writing",
            "Summary Writing",
            "Speech Work",
            "Lexis and Structure",
            "Revision - WAEC Focus",
        ],
        "ss2": [
            "Advanced Grammar",
            "Clauses and Phrases",
            "Reading for Meaning",
            "Formal Letter Writing",
            "Informal Letter Writing",
            "Report and Article Writing",
            "Argumentative Essay Writing",
            "Summary Techniques",
            "Oral English",
            "Revision - WAEC Focus",
        ],
        "ss3": [
            "Essay Writing Strategies",
            "Comprehension and Critical Reading",
            "Summary and Note-Making",
            "Lexis and Structure Mastery",
            "Register and Idiomatic Expression",
            "Oral English and Phonetics",
            "Revision of Grammar Rules",
            "Examination Practice in Writing",
            "Past Question Techniques",
            "Revision - WAEC Focus",
        ],
    },
    "economics": {
        "ss1": [
            "Introduction to Economics",
            "Basic Economic Problems",
            "Factors of Production",
            "Demand and Supply",
            "Price Determination",
            "Elasticity of Demand and Supply",
            "Theory of Consumer Behaviour",
            "Types of Economic Systems",
            "Population and Labour Market",
            "Revision - WAEC Focus",
        ],
        "ss2": [
            "National Income",
            "Money and Inflation",
            "Financial Institutions",
            "Public Finance",
            "Economic Growth and Development",
            "Agriculture in West Africa",
            "Industrialisation",
            "International Trade",
            "Economic Integration",
            "Revision - WAEC Focus",
        ],
        "ss3": [
            "Market Structures",
            "Business Organisations",
            "Theory of Costs and Revenue",
            "Location of Industry",
            "Taxation",
            "Banking and Capital Market",
            "Balance of Payments",
            "Exchange Rates",
            "Economic Planning and Development",
            "Revision - WAEC Focus",
        ],
    },
    "geography": {
        "ss1": [
            "Introduction to Geography",
            "The Earth and the Solar System",
            "Map Reading",
            "Scale and Distance",
            "Relief Features",
            "Rocks and Weathering",
            "Landforms",
            "Drainage Systems",
            "Climate and Weather",
            "Revision - WAEC Focus",
        ],
        "ss2": [
            "Vegetation and Soils",
            "Environmental Resources",
            "Population Geography",
            "Settlement",
            "Transportation",
            "Trade and Communication",
            "Agricultural Geography",
            "Mineral Resources",
            "Manufacturing Industries",
            "Revision - WAEC Focus",
        ],
        "ss3": [
            "Surveying and Fieldwork",
            "Geographical Information Interpretation",
            "Regional Geography of West Africa",
            "Regional Geography of Africa",
            "Environmental Hazards",
            "Urbanisation",
            "Tourism Geography",
            "World Trade Patterns",
            "Sustainable Development",
            "Revision - WAEC Focus",
        ],
    },
    "government": {
        "ss1": [
            "Meaning and Scope of Government",
            "Basic Concepts in Government",
            "The Constitution",
            "Forms of Government",
            "Arms of Government",
            "Citizenship",
            "Political Ideologies",
            "Democracy and Rule of Law",
            "Electoral Systems",
            "Revision - WAEC Focus",
        ],
        "ss2": [
            "Political Parties and Pressure Groups",
            "Public Opinion",
            "Public Administration",
            "Civil Service",
            "Local Government",
            "Military Rule",
            "Nigerian Constitutional Development",
            "Federalism in Nigeria",
            "Legislature and Law-Making",
            "Revision - WAEC Focus",
        ],
        "ss3": [
            "Foreign Policy",
            "International Organisations",
            "ECOWAS and African Union",
            "The United Nations",
            "Defence and National Security",
            "Human Rights",
            "Development Challenges in Africa",
            "Political Crises and Conflict Resolution",
            "Comparative Government",
            "Revision - WAEC Focus",
        ],
    },
    "literature_in_english": {
        "ss1": [
            "Introduction to Literature",
            "Genres of Literature",
            "Elements of Prose",
            "Elements of Poetry",
            "Elements of Drama",
            "Figures of Speech",
            "Themes and Settings",
            "Characterisation",
            "Literary Appreciation",
            "Revision - WAEC Focus",
        ],
        "ss2": [
            "Analysis of African Prose",
            "Analysis of Non-African Prose",
            "Poetry Appreciation",
            "Dramatic Techniques",
            "Narrative Voice and Point of View",
            "Imagery and Symbolism",
            "Literary Devices in Context",
            "Comparative Text Study",
            "Essay Writing in Literature",
            "Revision - WAEC Focus",
        ],
        "ss3": [
            "Advanced Prose Analysis",
            "Advanced Poetry Analysis",
            "Advanced Drama Analysis",
            "Themes in African Literature",
            "Style and Language",
            "Literary Criticism",
            "Contextual Interpretation",
            "Examination Essay Techniques",
            "Past Question Practice",
            "Revision - WAEC Focus",
        ],
    },
}

# Placeholder topic names used when a subject/level is not yet in CURRICULUM.
_PLACEHOLDER_TOPICS = [f"Topic {i + 1}" for i in range(10)]


def _get_topics(subject: str, level: str, n: int) -> list[str]:
    topics = (
        CURRICULUM.get(subject, {}).get(level, _PLACEHOLDER_TOPICS)
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

    def __init__(
        self,
        cfg: dict[str, Any],
        b2: B2Client,
        dry_run: bool = False,
    ) -> None:
        self.cfg = cfg
        self.b2 = b2
        self.dry_run = dry_run
        text_cfg = cfg["providers"]["text"]
        self.provider = text_cfg["provider"]
        self.model = text_cfg["model"]
        self.temperature = text_cfg.get("temperature", 0.7)
        self.max_tokens = text_cfg.get("max_tokens", 4096)
        self._client: Any | None = None
        if not self.dry_run:
            self._client = importlib.import_module("genblaze").Client()

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
        prompt_dir = _preview_dir(repo_root, self.cfg, subject, level)
        prompt_dir.mkdir(parents=True, exist_ok=True)

        logger.info("  Generating chapter %02d: %s", chapter_number, topic)

        # ---- 1. Textbook chapter ----------------------------------------
        chapter_prompt = _CHAPTER_PROMPT.format(
            subject=subject,
            subject_title=subject_title,
            level=level,
            level_upper=level_upper,
            ss_year=ss_year,
            topic=topic,
            chapter_number=chapter_number,
            generated_at=now,
        )
        chapter_prompt_path = prompt_dir / (
            f"ch{chapter_number:02d}_{slug}_chapter_prompt.txt"
        )
        chapter_prompt_path.write_text(chapter_prompt, encoding="utf-8")
        if self.dry_run:
            chapter_data = _build_preview_chapter(
                subject, level, chapter_number, topic, now
            )
        else:
            chapter_json = self._llm_call(chapter_prompt)
            chapter_data = _parse_json(chapter_json)

        # Write local files
        tb_dir = repo_root / self.cfg["output"]["textbooks"] / subject / level
        tb_dir.mkdir(parents=True, exist_ok=True)
        json_path = tb_dir / f"ch{chapter_number:02d}_{slug}.json"
        md_path   = tb_dir / f"ch{chapter_number:02d}_{slug}.md"
        json_path.write_text(json.dumps(chapter_data, indent=2, ensure_ascii=False), encoding="utf-8")
        md_path.write_text(_chapter_to_markdown(chapter_data), encoding="utf-8")

        # Upload to B2
        prov = build_provenance("text", self.provider, self.model, chapter_prompt[:200])
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
        flashcard_prompt = _FLASHCARD_PROMPT.format(
            subject=subject,
            subject_title=subject_title,
            level=level,
            level_upper=level_upper,
            topic=topic,
            generated_at=now,
        )
        flashcard_prompt_path = prompt_dir / (
            f"ch{chapter_number:02d}_{slug}_flashcards_prompt.txt"
        )
        flashcard_prompt_path.write_text(flashcard_prompt, encoding="utf-8")
        if self.dry_run:
            fc_data = _build_preview_flashcards(subject, level, topic, now)
        else:
            fc_json = self._llm_call(flashcard_prompt)
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
        quiz_prompt = _QUIZ_PROMPT.format(
            subject=subject,
            subject_title=subject_title,
            level=level,
            level_upper=level_upper,
            topic=topic,
            generated_at=now,
        )
        quiz_prompt_path = prompt_dir / (
            f"ch{chapter_number:02d}_{slug}_quiz_prompt.txt"
        )
        quiz_prompt_path.write_text(quiz_prompt, encoding="utf-8")
        if self.dry_run:
            quiz_data = _build_preview_quiz(subject, level, topic, now)
        else:
            quiz_json = self._llm_call(quiz_prompt)
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
        if self.dry_run:
            manifest["dry_run"] = {
                "chapter_prompt": str(chapter_prompt_path),
                "flashcards_prompt": str(flashcard_prompt_path),
                "quiz_prompt": str(quiz_prompt_path),
            }
        return manifest

    def _llm_call(self, prompt: str) -> str:
        if self._client is None:
            raise RuntimeError("Genblaze client is unavailable in dry-run mode")
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


def _preview_dir(
    repo_root: Path,
    cfg: dict[str, Any],
    subject: str,
    level: str,
) -> Path:
    return repo_root / cfg["output"]["textbooks"] / subject / level / "_dry_run"


def _build_preview_chapter(
    subject: str,
    level: str,
    chapter_number: int,
    topic: str,
    generated_at: str,
) -> dict[str, Any]:
    return {
        "subject": subject,
        "level": level,
        "chapter_number": chapter_number,
        "topic": topic,
        "learning_objectives": [
            f"Define the core ideas in {topic}",
            f"Solve basic exam-style questions on {topic}",
            f"Relate {topic} to real classroom examples",
        ],
        "introduction": (
            f"This dry-run chapter preview shows the file structure for {topic}. "
            "It does not call an external model, but it preserves the expected output shape."
        ),
        "sections": [
            {
                "heading": f"Foundations of {topic}",
                "body": (
                    f"Preview content for {topic}. Replace this with generated curriculum text "
                    "during a live run."
                ),
                "worked_examples": [
                    f"Worked example placeholder for {topic}.",
                ],
                "diagram_hints": [
                    f"[DIAGRAM: labelled concept diagram for {topic}]",
                ],
            }
        ],
        "key_terms": [
            {"term": topic, "definition": f"Preview definition for {topic}."},
            {"term": "Exam skill", "definition": "Ability to answer curriculum-aligned questions."},
        ],
        "summary": f"Dry-run summary placeholder for {topic}.",
        "practice_questions": [
            {
                "type": "objective",
                "question": f"Which statement best describes {topic}?",
                "options": ["A", "B", "C", "D"],
                "answer": "A",
                "explanation": "Preview explanation.",
            },
            {
                "type": "theory",
                "question": f"Explain one application of {topic}.",
                "options": [],
                "answer": "Any correct curriculum-aligned explanation.",
                "explanation": "Preview explanation.",
            },
        ],
        "further_reading": [
            "NERDC scheme of work",
            "Class notes and teacher-approved revision guides",
        ],
        "generated_at": generated_at,
        "generator": "aku-content-forge/text-stage-dry-run",
    }


def _build_preview_flashcards(
    subject: str,
    level: str,
    topic: str,
    generated_at: str,
) -> dict[str, Any]:
    flashcards = []
    for idx in range(1, 16):
        flashcards.append(
            {
                "front": f"{topic} concept {idx}",
                "back": f"Preview flashcard answer {idx} for {topic}.",
                "difficulty": "easy" if idx <= 5 else "medium" if idx <= 10 else "hard",
            }
        )
    return {
        "subject": subject,
        "level": level,
        "topic": topic,
        "flashcards": flashcards,
        "generated_at": generated_at,
        "generator": "aku-content-forge/text-stage-dry-run",
    }


def _build_preview_quiz(
    subject: str,
    level: str,
    topic: str,
    generated_at: str,
) -> dict[str, Any]:
    questions = []
    for idx in range(1, 11):
        questions.append(
            {
                "id": f"Q{idx:03d}",
                "body": f"Preview quiz question {idx} on {topic}.",
                "options": [
                    "A. Option one",
                    "B. Option two",
                    "C. Option three",
                    "D. Option four",
                ],
                "correct_option": 0,
                "answer": "A. Option one",
                "explanation": f"Preview explanation for question {idx}.",
                "difficulty": "easy" if idx <= 3 else "medium" if idx <= 7 else "hard",
                "marks": 1,
            }
        )
    return {
        "subject": subject,
        "level": level,
        "topic": topic,
        "questions": questions,
        "generated_at": generated_at,
        "generator": "aku-content-forge/text-stage-dry-run",
    }


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
