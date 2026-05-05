# Contributing Content to Aku-Content

This guide explains how to copy textbooks, content templates, and exam papers from your local machine into this repository.

---

## Prerequisites

1. **Git LFS** must be installed on your machine:
   ```bash
   git lfs install
   ```
2. Clone the repo (if you haven't already):
   ```bash
   git clone https://github.com/oumar-code/Aku-Content
   cd Aku-Content
   git lfs install
   ```

---

## Directory Layout

```
Aku-Content/
├── content/
│   └── textbooks/
│       └── <subject>/          # e.g. mathematics, biology
│           └── <level>/        # ss1 | ss2 | ss3
│               ├── <chapter>.json
│               └── <chapter>.md
├── content_templates/
│   └── <subject>/              # e.g. mathematics
│       └── <template>.csv
└── data/
    └── exam_papers/
        ├── bece/
        │   └── <subject>/      # e.g. mathematics
        │       └── questions.json
        └── waec/
            └── <subject>/      # e.g. mathematics
                └── questions.json  (or <year>.pdf)
```

**Naming conventions**: lowercase, underscore-separated (e.g. `english_language`, not `English Language`).

---

## Step-by-step Migration

Work in **three separate batches** so that history stays clean and individual batches can be rolled back if needed.

### Batch 1 — Content Templates

```bash
# Copy your CSVs into the correct subject folder
cp /path/to/local/templates/mathematics/*.csv content_templates/mathematics/
cp /path/to/local/templates/biology/*.csv      content_templates/biology/
# … repeat for all subjects

git add content_templates/
git commit -m "content(templates): add WAEC/NERDC lesson CSV templates"
```

### Batch 2 — Textbooks

Commit one subject at a time to keep history granular:

```bash
cp -r /path/to/local/textbooks/mathematics/ss1/* content/textbooks/mathematics/ss1/
git add content/textbooks/mathematics/
git commit -m "content(textbooks): add mathematics SS1 textbook (JSON + MD)"

# Repeat for each subject / level
```

### Batch 3 — Exam Papers

```bash
# BECE papers (JSON question banks already exist — add new ones or extend)
cp /path/to/local/exam_papers/bece/mathematics/*.json data/exam_papers/bece/mathematics/
git add data/exam_papers/bece/
git commit -m "content(exam-papers): add BECE past questions"

# WAEC papers (PDFs are tracked via Git LFS automatically)
cp /path/to/local/exam_papers/waec/mathematics/*.pdf data/exam_papers/waec/mathematics/
git add data/exam_papers/waec/
git commit -m "content(exam-papers): add WAEC past papers (PDF)"
```

### Push

```bash
git push origin main
# Git LFS uploads binary objects automatically during push
```

---

## Verify the Upload

```bash
# List all LFS-tracked files
git lfs ls-files

# Do a quick sanity-check from a fresh clone
git clone https://github.com/oumar-code/Aku-Content /tmp/verify-clone
cd /tmp/verify-clone
git lfs install && git lfs pull
```

---

## Adding New Binary Formats

If you have formats not yet listed (e.g. `.pptx`, `.epub`, `.docx`), they are already configured in `.gitattributes`. For any other format, add a line **before** copying the files:

```
*.xyz filter=lfs diff=lfs merge=lfs -text
```

Then commit `.gitattributes` before adding the binary files.

---

## File Size & LFS Quota

GitHub LFS free tier: **1 GB storage / 1 GB bandwidth per month**.

- Split large uploads across multiple pushes if needed.
- Consider the GitHub LFS Data Pack (extra 50 GB storage + bandwidth) for large PDF collections.
- If exam papers are copyrighted / embargoed, keep this repo **private** or use a separate private repo for those assets.

---

## Commit Message Convention

```
content(<type>): <short description>

# Types: textbooks | templates | exam-papers | assets
# Examples:
#   content(textbooks): add chemistry SS2 textbook
#   content(templates): add government lesson CSV template
#   content(exam-papers): add WAEC biology 2023 paper
```
