#!/usr/bin/env python3
import csv
import json
import os
import re
import time
import unicodedata
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
SOURCE_DIR = Path(os.environ.get("SECOND_PASS_SOURCE", ROOT / "results" / "online"))
OUT_DIR = Path(os.environ.get("SECOND_PASS_OUTPUT", ROOT / "results" / "second_pass"))
CACHE_PATH = OUT_DIR / "cache.json"
REPORT_PATH = OUT_DIR / "flagged.json"
SUMMARY_PATH = OUT_DIR / "summary.json"
CSV_PATH = OUT_DIR / "flagged.csv"
DIRECT_CHECKS_PATH = OUT_DIR / "direct_url_checks.json"
OPENALEX_URL = "https://api.openalex.org/works"
CROSSREF_URL = "https://api.crossref.org/works"
REQUEST_DELAY = 0.35
TIMEOUT = (15, 45)

os.umask(0o077)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def read_json(path, default):
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def write_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + ".tmp")
    fields = [
        "file",
        "title",
        "valiref_type",
        "valiref_confidence",
        "classification",
        "match_score",
        "matched_title",
        "matched_authors",
        "matched_year",
        "matched_venue",
        "matched_source",
        "matched_url",
    ]
    with tmp.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def normalize_title(value):
    value = (value or "").lower()
    value = unicodedata.normalize("NFKD", value)
    value = "".join(char for char in value if not unicodedata.combining(char))
    value = value.replace("&", " and ")
    value = re.sub(r"\([^)]*\)", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def title_score(left, right):
    a = normalize_title(left)
    b = normalize_title(right)
    if not a or not b:
        return 0.0
    ratio = SequenceMatcher(None, a, b).ratio()
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    containment = len(shorter) / len(longer) if shorter in longer else 0.0
    prefix = 0.95 if longer.startswith(shorter) and len(shorter.split()) >= 4 else 0.0
    a_tokens = set(a.split())
    b_tokens = set(b.split())
    overlap = len(a_tokens & b_tokens) / len(a_tokens | b_tokens) if a_tokens | b_tokens else 0.0
    return max(ratio, containment * 0.98, overlap, prefix)


def surname_from_valiref(author):
    author = (author or "").strip()
    if not author:
        return ""
    if "," in author:
        return normalize_title(author.split(",", 1)[0])
    parts = normalize_title(author).split()
    return parts[-1] if parts else ""


def surname_from_display(author):
    parts = normalize_title(author).split()
    if not parts:
        return ""
    return parts[-1]


def author_match(record_authors, candidate_authors):
    left = {surname_from_valiref(a) for a in record_authors}
    right = {surname_from_display(a) for a in candidate_authors}
    left.discard("")
    right.discard("")
    if not left or not right:
        return False
    return bool(left & right)


def sleep():
    time.sleep(REQUEST_DELAY)


def request_json(session, url, params, attempts=4):
    last_error = None
    for attempt in range(attempts):
        try:
            response = session.get(url, params=params, timeout=TIMEOUT)
            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt == attempts - 1:
                    response.raise_for_status()
                retry_after = response.headers.get("Retry-After")
                delay = int(retry_after) if retry_after and retry_after.isdigit() else min(5 * (attempt + 1), 30)
                time.sleep(delay)
                continue
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt == attempts - 1:
                raise
            time.sleep(min(5 * (attempt + 1), 30))
    raise last_error


def openalex_candidates(session, title):
    query = " ".join(title.replace("?", " ").replace("#", " ").split())
    try:
        data = request_json(
            session,
            OPENALEX_URL,
            {
                "search": query,
                "per-page": 5,
            },
        )
    except requests.HTTPError as exc:
        if getattr(exc.response, "status_code", None) != 400 or len(query.split()) <= 8:
            raise
        data = request_json(
            session,
            OPENALEX_URL,
            {
                "search": " ".join(query.split()[:8]),
                "per-page": 5,
            },
        )
    output = []
    for item in data.get("results", []):
        authors = []
        for authorship in item.get("authorships", []):
            name = (authorship.get("author") or {}).get("display_name")
            if name:
                authors.append(name)
        venue = ""
        primary = item.get("primary_location") or {}
        source = primary.get("source") or {}
        venue = source.get("display_name") or ""
        output.append(
            {
                "source": "openalex",
                "title": item.get("title") or item.get("display_name") or "",
                "authors": authors,
                "year": item.get("publication_year"),
                "venue": venue,
                "type": item.get("type"),
                "url": item.get("doi") or item.get("id"),
                "raw_id": item.get("id"),
            }
        )
    return output


def crossref_candidates(session, title):
    data = request_json(
        session,
        CROSSREF_URL,
        {
            "query.bibliographic": title,
            "rows": 5,
            "select": "DOI,title,author,issued,container-title,type",
        },
    )
    output = []
    for item in (data.get("message") or {}).get("items", []):
        authors = []
        for author in item.get("author", []):
            family = author.get("family")
            if family:
                authors.append(family)
        titles = item.get("title") or []
        venue_titles = item.get("container-title") or []
        date_parts = ((item.get("issued") or {}).get("date-parts") or [[None]])[0]
        output.append(
            {
                "source": "crossref",
                "title": titles[0] if titles else "",
                "authors": authors,
                "year": date_parts[0] if date_parts else None,
                "venue": venue_titles[0] if venue_titles else "",
                "type": item.get("type"),
                "url": f"https://doi.org/{item['DOI']}" if item.get("DOI") else "",
                "raw_id": item.get("DOI"),
            }
        )
    return output


def best_match(record, candidates):
    best = None
    for candidate in candidates:
        score = title_score(record["title"], candidate.get("title"))
        matched_authors = author_match(record.get("authors", []), candidate.get("authors", []))
        candidate = dict(candidate)
        candidate["match_score"] = round(score, 4)
        candidate["author_match"] = matched_authors
        if best is None or candidate["match_score"] > best["match_score"] or (
            candidate["match_score"] == best["match_score"] and matched_authors and not best["author_match"]
        ):
            best = candidate
    return best


def classify(record, candidates):
    best = best_match(record, candidates)
    if not best:
        return "not_found_online", best
    score = best["match_score"]
    matched = best["author_match"]
    if score >= 0.92 and matched:
        return "confirmed_real", best
    if score >= 0.98:
        return "likely_real_title_only", best
    if score >= 0.85 or (score >= 0.75 and matched):
        return "possible_match_review", best
    return "not_found_online", best


def load_flagged():
    summary = read_json(SOURCE_DIR / "summary.json", [])
    records = []
    for item in summary:
        result_path = SOURCE_DIR / f"{item.get('sha256', '')}.json"
        if not result_path.exists():
            result_path = Path(item.get("result_file", ""))
        result = read_json(result_path, {})
        for reference in result.get("references", []):
            if reference.get("hallucination_type") == "Real":
                continue
            records.append(
                {
                    "file": item["file"],
                    "sha256": item["sha256"],
                    "task_id": item.get("task_id"),
                    "title": reference.get("title") or "",
                    "authors": reference.get("authors") or [],
                    "venue": reference.get("venue") or "",
                    "valiref_type": reference.get("hallucination_type"),
                    "valiref_confidence": reference.get("confidence"),
                    "valiref_reasoning": reference.get("reasoning"),
                }
            )
    return records


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(OUT_DIR, 0o700)
    records = load_flagged()
    cache = read_json(CACHE_PATH, {})
    direct_checks = read_json(DIRECT_CHECKS_PATH, {})
    session = requests.Session()
    session.headers.update(
        {
            "Accept": "application/json",
            "User-Agent": "ref-check-client/1.0",
        }
    )

    unique_titles = []
    seen = set()
    records_by_key = {}
    for record in records:
        key = normalize_title(record["title"])
        if key and key not in seen:
            seen.add(key)
            unique_titles.append((key, record["title"]))
            records_by_key[key] = record

    print(f"Loaded {len(records)} flagged references; querying {len(unique_titles)} unique titles", flush=True)
    for index, (key, title) in enumerate(unique_titles, start=1):
        if key in cache and "error" not in cache[key]:
            continue
        entry = {"title": title, "queried_at": utc_now(), "openalex": [], "crossref": []}
        try:
            print(f"[{index}/{len(unique_titles)}] {title}", flush=True)
            entry["openalex"] = openalex_candidates(session, title)
            sleep()
            entry["crossref"] = crossref_candidates(session, title)
            sleep()
        except Exception as exc:
            entry["error"] = str(exc)
            print(f"  query failed: {exc}", flush=True)
        cache[key] = entry
        write_json(CACHE_PATH, cache)

    for index, (key, title) in enumerate(unique_titles, start=1):
        entry = cache.get(key, {})
        candidates = list(entry.get("openalex", [])) + list(entry.get("crossref", []))
        classification, _ = classify(records_by_key[key], candidates)
        words = normalize_title(title).split()
        if classification != "not_found_online" or len(words) <= 8 or "fallback" in entry:
            continue
        query = " ".join(words[:8])
        print(f"[fallback {index}/{len(unique_titles)}] {query}", flush=True)
        fallback = {"query": query, "queried_at": utc_now(), "openalex": [], "crossref": []}
        try:
            fallback["openalex"] = openalex_candidates(session, query)
            sleep()
            fallback["crossref"] = crossref_candidates(session, query)
            sleep()
        except Exception as exc:
            fallback["error"] = str(exc)
            print(f"  fallback query failed: {exc}", flush=True)
        entry["fallback"] = fallback
        cache[key] = entry
        write_json(CACHE_PATH, cache)

    report = []
    for record in records:
        key = normalize_title(record["title"])
        entry = cache.get(key, {})
        fallback = entry.get("fallback") or {}
        candidates = (
            list(entry.get("openalex", []))
            + list(entry.get("crossref", []))
            + list(fallback.get("openalex", []))
            + list(fallback.get("crossref", []))
        )
        classification, best = classify(record, candidates)
        check = direct_checks.get(key)
        if check and check.get("verified"):
            classification = "confirmed_real_url"
            best = {
                "match_score": 1.0,
                "title": check.get("verified_title") or record["title"],
                "authors": check.get("authors") or record["authors"],
                "year": check.get("year"),
                "venue": check.get("source"),
                "source": "direct_url",
                "url": check.get("url"),
            }
        report.append(
            {
                **record,
                "classification": classification,
                "match_score": best.get("match_score") if best else None,
                "matched_title": best.get("title") if best else None,
                "matched_authors": best.get("authors") if best else None,
                "matched_year": best.get("year") if best else None,
                "matched_venue": best.get("venue") if best else None,
                "matched_source": best.get("source") if best else None,
                "matched_url": best.get("url") if best else None,
                "query_error": entry.get("error") or fallback.get("error"),
            }
        )

    report.sort(key=lambda row: (row["file"], row["classification"], row["title"]))
    write_json(REPORT_PATH, report)
    write_csv(CSV_PATH, report)

    counts = {}
    per_file = {}
    for row in report:
        classification = row["classification"]
        counts[classification] = counts.get(classification, 0) + 1
        per_file.setdefault(row["file"], {})
        per_file[row["file"]][classification] = per_file[row["file"]].get(classification, 0) + 1

    summary = {
        "generated_at": utc_now(),
        "total_flagged": len(report),
        "unique_titles_queried": len(unique_titles),
        "classification_counts": counts,
        "per_file": per_file,
        "report_file": str(REPORT_PATH),
        "csv_file": str(CSV_PATH),
        "direct_url_checks_file": str(DIRECT_CHECKS_PATH),
    }
    write_json(SUMMARY_PATH, summary)

    print("\nSecond-pass summary", flush=True)
    print("-------------------", flush=True)
    for key in ["confirmed_real", "confirmed_real_url", "likely_real_title_only", "possible_match_review", "not_found_online"]:
        print(f"{key}: {counts.get(key, 0)}", flush=True)
    print(f"\nReport: {REPORT_PATH}", flush=True)
    print(f"CSV: {CSV_PATH}", flush=True)


if __name__ == "__main__":
    main()
