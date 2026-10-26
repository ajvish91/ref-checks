# Reference Validation Workflow

A small, review-oriented toolkit for checking whether references in a PDF exist. It uploads a references-only PDF to ValiRef, then re-checks ValiRef's flagged references against OpenAlex and Crossref by title. Non-indexed sources such as blog posts and datasets can be verified with an optional direct-URL manifest.

This repository contains only code. Manuscripts, uploaded PDFs, and run outputs are intentionally ignored.

## What it does

1. `valiref_upload.py`
   - Finds PDFs in the repository root.
   - Hashes each file and uploads it as `refs-<sha256-prefix>.pdf`.
   - Polls the ValiRef task and saves the raw result, state, mapping, and summary under `results/`.
   - Reuses completed tasks when rerun.

2. `second_pass.py`
   - Reads the flagged references from `results/online/summary.json`.
   - Searches OpenAlex and Crossref using reference titles only.
   - Classifies each result as confirmed, likely, needing review, or not found.
   - Optionally merges direct-URL checks for sources that scholarly indexes do not cover.

## Privacy and scope

- Use PDFs that contain only the reference section. ValiRef receives the uploaded PDF.
- OpenAlex and Crossref receive reference titles only. No PDF is sent to them.
- Uploaded filenames are anonymized, but PDF metadata can still identify a manuscript. Strip metadata before uploading if that matters.
- Run outputs contain reference titles, authors, DOIs, task IDs, and original filenames. Treat `results/` as sensitive.
- Never commit PDFs, `results/`, `expected.json`, or local logs. `.gitignore` covers these paths.

## Requirements

- Python 3.10 or newer
- `requests`

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Quick start

Place one or more references-only PDFs in the repository root, then run:

```bash
VALIREF_SEARCH_MODE=local \
VALIREF_RESULTS_DIR=results/online \
python3 valiref_upload.py
```

Then run the second pass:

```bash
python3 second_pass.py
```

`second_pass.py` reads `results/online/summary.json` by default and writes to `results/second_pass/`.

## Configuration

### `valiref_upload.py`

| Variable | Default | Purpose |
| --- | --- | --- |
| `VALIREF_BASE_URL` | `https://www.valiref.com/api` | ValiRef API base URL |
| `VALIREF_SEARCH_MODE` | `local` | Value sent as `search_mode` |
| `VALIREF_RESULTS_DIR` | `results` | Output directory for state and results |
| `VALIREF_EXPECTED_FILE` | `expected.json` | Optional expected-reference counts |

`expected.json` is optional and locally supplied. It maps PDF filenames to expected reference counts:

```json
{
  "example.pdf": 42
}
```

Do not commit this file; it can reveal manuscript filenames and counts.

### `second_pass.py`

| Variable | Default | Purpose |
| --- | --- | --- |
| `SECOND_PASS_SOURCE` | `results/online` | Directory containing ValiRef `summary.json` |
| `SECOND_PASS_OUTPUT` | `results/second_pass` | Directory for second-pass reports |

## Outputs

`valiref_upload.py` writes:

- `results/<run>/state.json` - task IDs and upload state
- `results/<run>/mapping.json` - original filename to anonymized upload mapping
- `results/<run>/summary.json` - per-file ValiRef counts
- `results/<run>/<sha256>.json` - raw ValiRef result
- `results/<run>/<stem>-compressed.pdf` - optional compressed upload, if present

`second_pass.py` writes to `results/second_pass/`:

- `cache.json` - incremental OpenAlex/Crossref responses
- `flagged.json` - full classification report
- `flagged.csv` - spreadsheet-friendly report
- `summary.json` - classification counts
- `direct_url_checks.json` - optional manual URL verification manifest, read if present

## Classifications

- `confirmed_real` - exact or near-exact title match with a matching author.
- `confirmed_real_url` - direct source URL or DOI verified outside OpenAlex/Crossref.
- `likely_real_title_only` - exact title match, but author matching was inconclusive.
- `possible_match_review` - close match that needs a human decision.
- `not_found_online` - no reliable match in the checked sources.

## Direct-URL checks

Some references are blog posts, incident reports, or datasets. To verify them, add `results/second_pass/direct_url_checks.json` using normalized title keys. For example:

```json
{
  "normalized reference title": {
    "verified": true,
    "url": "https://example.org/source",
    "verified_title": "Title shown at the source",
    "authors": ["Author Name"],
    "year": 2026,
    "source": "Publisher page",
    "http_status": 200
  }
}
```

Keys are produced by the same normalization used by `second_pass.py`: lowercase, strip accents and punctuation, replace `&` with `and`, and collapse whitespace. A verified entry overrides the automated classification.

## Clean sharing check

Before sharing or committing, confirm that only source files are visible:

```bash
git status --ignored
git ls-files
```

The tracked tree should contain only `.gitignore`, `README.md`, `requirements.txt`, `valiref_upload.py`, and `second_pass.py`.

## Known limitations

- The hosted ValiRef worker used during development ignored the per-task `search_mode` and queried its local index. Treat ValiRef flags as candidates, not final verdicts.
- OpenAlex and Crossref do not index every blog post, dataset, or very recent non-scholarly source.
- Title matching is deliberately conservative. Ambiguous matches are surfaced for human review rather than silently accepted.
- Direct-URL verification is manual and is only as reliable as the source page.
