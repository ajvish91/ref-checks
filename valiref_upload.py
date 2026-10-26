#!/usr/bin/env python3
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE_URL = os.environ.get("VALIREF_BASE_URL", "https://www.valiref.com/api").rstrip("/")
SEARCH_MODE = os.environ.get("VALIREF_SEARCH_MODE", "local")
ROOT = Path(__file__).resolve().parent
RESULTS_DIR = Path(os.environ.get("VALIREF_RESULTS_DIR", "results"))
if not RESULTS_DIR.is_absolute():
    RESULTS_DIR = ROOT / RESULTS_DIR
STATE_PATH = RESULTS_DIR / "state.json"
MAPPING_PATH = RESULTS_DIR / "mapping.json"
SUMMARY_PATH = RESULTS_DIR / "summary.json"
POLL_INTERVAL = 5
POLL_TIMEOUT = 1800
EXPECTED_PATH = Path(os.environ.get("VALIREF_EXPECTED_FILE", ROOT / "expected.json"))
EXPECTED = {}
TERMINAL = {"completed", "failed", "failed_permanently"}

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


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def log(message):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


def make_session():
    session = requests.Session()
    session.headers.update(
        {
            "Accept": "application/json",
            "User-Agent": "ref-check-client/1.0",
        }
    )
    return session


def retry_delay(response, attempt):
    if response is not None:
        value = response.headers.get("Retry-After")
        if value and value.isdigit():
            return int(value)
    return min(5 * (attempt + 1), 30)


def get_json(session, path, attempts=4):
    last_error = None
    for attempt in range(attempts):
        try:
            response = session.get(f"{BASE_URL}{path}", timeout=(15, 60))
            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt == attempts - 1:
                    response.raise_for_status()
                delay = retry_delay(response, attempt)
                log(f"GET {path} returned {response.status_code}; retrying in {delay}s")
                time.sleep(delay)
                continue
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt == attempts - 1:
                raise
            delay = retry_delay(None, attempt)
            log(f"GET {path} failed; retrying in {delay}s: {exc}")
            time.sleep(delay)
    raise last_error


def submit_pdf(session, pdf_path, external_name, attempts=4):
    last_error = None
    for attempt in range(attempts):
        try:
            with pdf_path.open("rb") as fh:
                response = session.post(
                    f"{BASE_URL}/validation/submit",
                    files={"file": (external_name, fh, "application/pdf")},
                    data={"search_mode": SEARCH_MODE},
                    timeout=(30, 600),
                )
            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt == attempts - 1:
                    response.raise_for_status()
                delay = retry_delay(response, attempt)
                log(
                    f"POST submit returned {response.status_code}; retrying in {delay}s"
                )
                time.sleep(delay)
                continue
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt == attempts - 1:
                raise
            delay = retry_delay(None, attempt)
            log(f"POST submit failed; retrying in {delay}s: {exc}")
            time.sleep(delay)
    raise last_error


def poll_task(session, task_id):
    start = time.time()
    last_key = None
    last_log = 0.0
    while time.time() - start < POLL_TIMEOUT:
        data = get_json(session, f"/validation/status/{task_id}")
        status = data.get("status", "unknown")
        stage = data.get("stage") or "-"
        progress = data.get("progress") or {}
        processed = progress.get("processed")
        total = progress.get("total")
        key = (status, stage, processed, total)
        now = time.time()
        if key != last_key or now - last_log >= 60:
            detail = ""
            if processed is not None and total is not None:
                detail = f" ({processed}/{total})"
            log(f"{task_id}: status={status} stage={stage}{detail}")
            last_key = key
            last_log = now
        if status in TERMINAL:
            return data
        time.sleep(POLL_INTERVAL)
    raise TimeoutError(f"Timed out waiting for task {task_id}")


def result_task_id(status_data):
    return status_data.get("task_id")


def safe_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def summarize_result(pdf_name, sha, task_id, result):
    references = result.get("references") or []
    type_counts = {}
    for reference in references:
        key = reference.get("hallucination_type") or "Unknown"
        type_counts[key] = type_counts.get(key, 0) + 1
    return {
        "file": pdf_name,
        "sha256": sha,
        "task_id": task_id,
        "status": result.get("status"),
        "search_mode": SEARCH_MODE,
        "expected_references": EXPECTED.get(pdf_name),
        "total_references": safe_int(result.get("total_references")),
        "validated_count": safe_int(result.get("validated_count")),
        "real_count": safe_int(result.get("real_count")),
        "hallucination_count": safe_int(result.get("hallucination_count")),
        "type_counts": type_counts,
        "error_code": result.get("error_code"),
        "error_message": result.get("error_message"),
        "duration_seconds": result.get("duration_seconds"),
        "result_file": str(RESULTS_DIR / f"{sha}.json"),
    }


def main():
    global EXPECTED

    RESULTS_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(RESULTS_DIR, 0o700)
    EXPECTED = read_json(EXPECTED_PATH, {})
    if not isinstance(EXPECTED, dict):
        raise ValueError(f"{EXPECTED_PATH} must contain a JSON object")

    state = read_json(STATE_PATH, {})
    mapping = read_json(MAPPING_PATH, {})
    session = make_session()
    summaries = []
    log(f"Endpoint={BASE_URL} search_mode={SEARCH_MODE} results_dir={RESULTS_DIR}")

    pdf_paths = sorted(ROOT.glob("*.pdf"), key=lambda p: p.name)
    if not pdf_paths:
        log("No PDFs found.")
        return 1

    for pdf_path in pdf_paths:
        sha = sha256_file(pdf_path)
        external_name = f"refs-{sha[:12]}.pdf"
        compressed_path = RESULTS_DIR / f"{pdf_path.stem}-compressed.pdf"
        upload_path = compressed_path if compressed_path.exists() else pdf_path
        entry = state.get(sha, {})
        mapping[sha] = {
            "file": pdf_path.name,
            "uploaded_as": external_name,
            "sha256": sha,
            "size_bytes": upload_path.stat().st_size,
            "original_size_bytes": pdf_path.stat().st_size,
            "expected_references": EXPECTED.get(pdf_path.name),
        }
        write_json(MAPPING_PATH, mapping)

        log(
            f"{pdf_path.name}: sha256={sha[:12]} upload_name={external_name} "
            f"upload_size={upload_path.stat().st_size} source={upload_path.name}"
        )

        result_path = RESULTS_DIR / f"{sha}.json"
        if entry.get("status") == "completed" and result_path.exists():
            log(f"{pdf_path.name}: already completed; reusing saved result")
            result = read_json(result_path, {})
            summaries.append(summarize_result(pdf_path.name, sha, entry.get("task_id"), result))
            continue

        task_id = entry.get("task_id")
        status_data = None

        if task_id and entry.get("status") not in TERMINAL:
            log(f"{pdf_path.name}: resuming existing task {task_id}")
            try:
                status_data = poll_task(session, task_id)
            except Exception as exc:
                log(f"{pdf_path.name}: resume failed: {exc}")
                entry.update({"status": "error", "error": str(exc), "updated_at": utc_now()})
                state[sha] = entry
                write_json(STATE_PATH, state)
                continue

        if not task_id:
            try:
                submission = submit_pdf(session, upload_path, external_name)
                task_id = submission.get("task_id")
                if not task_id:
                    raise ValueError(f"submit response missing task_id: {submission}")
                entry = {
                    "file": pdf_path.name,
                    "sha256": sha,
                    "uploaded_as": external_name,
                    "task_id": task_id,
                    "status": submission.get("status", "pending"),
                    "submitted_at": utc_now(),
                    "updated_at": utc_now(),
                }
                state[sha] = entry
                write_json(STATE_PATH, state)
                log(f"{pdf_path.name}: submitted task {task_id}")
            except Exception as exc:
                log(f"{pdf_path.name}: submit failed: {exc}")
                entry.update({"status": "error", "error": str(exc), "updated_at": utc_now()})
                state[sha] = entry
                write_json(STATE_PATH, state)
                continue

        try:
            status_data = status_data or poll_task(session, task_id)
            terminal_status = status_data.get("status")
            entry.update(
                {
                    "status": terminal_status,
                    "stage": status_data.get("stage"),
                    "progress": status_data.get("progress"),
                    "updated_at": utc_now(),
                }
            )
            state[sha] = entry
            write_json(STATE_PATH, state)

            result = get_json(session, f"/validation/result/{task_id}")
            write_json(result_path, result)
            summary = summarize_result(pdf_path.name, sha, task_id, result)
            summaries.append(summary)
            write_json(SUMMARY_PATH, summaries)
            log(
                f"{pdf_path.name}: {summary['status']} total={summary['total_references']} "
                f"validated={summary['validated_count']} real={summary['real_count']} "
                f"hallucinated={summary['hallucination_count']}"
            )
        except Exception as exc:
            log(f"{pdf_path.name}: processing failed: {exc}")
            entry.update({"status": "error", "error": str(exc), "updated_at": utc_now()})
            state[sha] = entry
            write_json(STATE_PATH, state)

    write_json(SUMMARY_PATH, summaries)

    print()
    print("Summary")
    print("-------")
    for summary in summaries:
        expected = summary.get("expected_references")
        expected_label = expected if expected is not None else "?"
        refs_check = "n/a" if expected is None else ("ok" if expected == summary["total_references"] else "MISMATCH")
        print(
            f"{summary['file']}: refs={summary['total_references']}/{expected_label} "
            f"validated={summary['validated_count']} real={summary['real_count']} "
            f"hallucinated={summary['hallucination_count']} refs_check={refs_check}"
        )

    return 0 if len(summaries) == len(pdf_paths) else 1


if __name__ == "__main__":
    sys.exit(main())
