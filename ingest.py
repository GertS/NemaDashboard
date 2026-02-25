#!/usr/bin/env python3
"""
HOW TO RUN:
- pip install -r requirements.txt
- python ingest.py --pdf_dir /pad/naar/pdfs --db_path nematodes.db
- streamlit run app.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import pdfplumber

LOGGER = logging.getLogger("nema.ingest")

DUTCH_MONTHS = {
    "januari": 1,
    "februari": 2,
    "maart": 3,
    "april": 4,
    "mei": 5,
    "juni": 6,
    "juli": 7,
    "augustus": 8,
    "september": 9,
    "oktober": 10,
    "november": 11,
    "december": 12,
}

CATEGORY_LABELS = {
    "wortellesieaaltjes",
    "wortelknobbelaaltjes",
    "vrijlevende aaltjes",
    "stengelaaltjes",
    "trichodoriden",
    "spiralen",
    "ringaaltjes",
    "cysteaaltjes",
    "overige",
}

META_PATTERNS = {
    "debtor_id": re.compile(r"Debiteurnummer\s*:\s*(.+)", re.IGNORECASE),
    "raw_field_name": re.compile(r"Perceel\s*:\s*(.+)", re.IGNORECASE),
    "sample_id": re.compile(r"Monsternummer\s*:\s*([A-Za-z0-9\-_/]+)", re.IGNORECASE),
    "sample_type": re.compile(r"Soort\s+monster\s*:\s*(.+)", re.IGNORECASE),
    "receive_date": re.compile(r"Datum\s+ontvangst\s*:\s*(.+)", re.IGNORECASE),
    "report_date": re.compile(r"Datum\s+verslag\s*:\s*(.+)", re.IGNORECASE),
}


@dataclass
class ParsedResult:
    analysis_type: str
    species: str
    value: Optional[int]
    unit: str
    cysts: Optional[int]
    lle: Optional[int]
    infection_class: Optional[str]


@dataclass
class ParsedSample:
    sample_id: str
    raw_field_name: Optional[str]
    report_date: Optional[str]
    receive_date: Optional[str]
    sample_type: Optional[str]
    debtor_id: Optional[str]
    source_pdf: str
    source_pdf_hash: str
    text: str
    results: List[ParsedResult]


def normalize_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def parse_int(value: str) -> Optional[int]:
    m = re.search(r"-?\d+", value)
    return int(m.group(0)) if m else None


def date_to_iso(date_raw: Optional[str]) -> Optional[str]:
    if not date_raw:
        return None
    text = normalize_whitespace(date_raw.lower().replace(",", " "))
    # try dd-mm-yyyy or dd/mm/yyyy
    m = re.search(r"(\d{1,2})[\-/](\d{1,2})[\-/](\d{2,4})", text)
    if m:
        day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if year < 100:
            year += 2000
        try:
            return datetime(year, month, day).strftime("%Y-%m-%d")
        except ValueError:
            return date_raw

    m = re.search(r"(\d{1,2})\s+([a-z]+)\s+(\d{4})", text)
    if m:
        day, month_name, year = int(m.group(1)), m.group(2), int(m.group(3))
        month = DUTCH_MONTHS.get(month_name)
        if month:
            try:
                return datetime(year, month, day).strftime("%Y-%m-%d")
            except ValueError:
                return date_raw
    return date_raw


def compute_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def extract_pdf_pages(path: Path) -> List[str]:
    pages: List[str] = []
    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages:
            pages.append(page.extract_text() or "")
    return pages


def detect_sample_id(text: str) -> Optional[str]:
    m = META_PATTERNS["sample_id"].search(text)
    return m.group(1).strip() if m else None


def split_samples_from_pages(pages: List[str]) -> Dict[str, List[str]]:
    blocks: Dict[str, List[str]] = {}
    active_id: Optional[str] = None
    unknown_counter = 0

    for page_text in pages:
        current_id = detect_sample_id(page_text)
        if current_id:
            active_id = current_id
            blocks.setdefault(active_id, []).append(page_text)
        else:
            if active_id is None:
                unknown_counter += 1
                active_id = f"UNKNOWN_SAMPLE_{unknown_counter}"
                blocks.setdefault(active_id, [])
            blocks[active_id].append(page_text)
    return blocks


def extract_metadata(text: str) -> Dict[str, Optional[str]]:
    out: Dict[str, Optional[str]] = {}
    for key, pattern in META_PATTERNS.items():
        m = pattern.search(text)
        out[key] = normalize_whitespace(m.group(1)) if m else None
    out["report_date"] = date_to_iso(out.get("report_date"))
    out["receive_date"] = date_to_iso(out.get("receive_date"))
    return out


def sanitize_species(s: str) -> str:
    s = re.sub(r"[*•·]+", "", s)
    s = re.sub(r"[^\w\s\-/(),.]", " ", s)
    return normalize_whitespace(s)


def parse_vrije_aaltjes(text: str) -> List[ParsedResult]:
    lines = [normalize_whitespace(l) for l in text.splitlines() if normalize_whitespace(l)]
    start = None
    end = len(lines)
    for i, line in enumerate(lines):
        if re.search(r"Vrijlevende\s+aaltjes", line, re.IGNORECASE):
            start = i + 1
            break
    if start is None:
        return []
    for i in range(start, len(lines)):
        if re.search(r"Cysteaaltjes\s+aantal\s+cysten\s+aantal\s+lle\s+besmettingsgraad", lines[i], re.IGNORECASE):
            end = i
            break

    results: List[ParsedResult] = []
    for line in lines[start:end]:
        if re.search(r"aantallen\s+per\s+100\s*gram", line, re.IGNORECASE):
            continue
        m = re.match(r"(.+?)\s+(-?\d+)\s*$", line)
        if not m:
            continue
        species = sanitize_species(m.group(1))
        if species.lower() in CATEGORY_LABELS:
            continue
        if species.lower().startswith("cysteaaltjes"):
            # indicator row in vrijlevende list; ignore
            continue
        value = parse_int(m.group(2))
        if not species or value is None:
            continue
        results.append(
            ParsedResult(
                analysis_type="vrije_aaltjes",
                species=species,
                value=value,
                unit="per_100g",
                cysts=None,
                lle=None,
                infection_class=None,
            )
        )
    return results


def parse_cysteaaltjes(text: str) -> List[ParsedResult]:
    lines = [normalize_whitespace(l) for l in text.splitlines() if normalize_whitespace(l)]
    start = None
    for i, line in enumerate(lines):
        if re.search(r"Cysteaaltjes\s+aantal\s+cysten\s+aantal\s+lle\s+besmettingsgraad", line, re.IGNORECASE):
            start = i + 1
            break
    if start is None:
        return []

    results: List[ParsedResult] = []
    pattern = re.compile(r"^(.+?)\s+(\d+)\s+(\d+)\s+(.+)$")
    for line in lines[start:]:
        if re.search(r"aantallen\s+per\s+200\s*(ml|cc)", line, re.IGNORECASE):
            continue
        m = pattern.match(line)
        if not m:
            # end of table heuristic
            if results:
                break
            continue
        species = sanitize_species(m.group(1))
        cysts = parse_int(m.group(2))
        lle = parse_int(m.group(3))
        infection_class = sanitize_species(m.group(4))
        if cysts is None or lle is None:
            continue
        results.append(
            ParsedResult(
                analysis_type="cysteaaltjes",
                species=species,
                value=None,
                unit="per_200cc",
                cysts=cysts,
                lle=lle,
                infection_class=infection_class,
            )
        )
    return results


def parse_sample_block(sample_key: str, pages: List[str], source_pdf: str, source_hash: str) -> ParsedSample:
    text = "\n".join(pages)
    meta = extract_metadata(text)
    sample_id = meta.get("sample_id") or sample_key

    results = []
    results.extend(parse_vrije_aaltjes(text))
    results.extend(parse_cysteaaltjes(text))

    return ParsedSample(
        sample_id=sample_id,
        raw_field_name=meta.get("raw_field_name"),
        report_date=meta.get("report_date"),
        receive_date=meta.get("receive_date"),
        sample_type=meta.get("sample_type"),
        debtor_id=meta.get("debtor_id"),
        source_pdf=source_pdf,
        source_pdf_hash=source_hash,
        text=text,
        results=results,
    )


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS samples (
            sample_id TEXT PRIMARY KEY,
            raw_field_name TEXT,
            field_name TEXT,
            report_date TEXT,
            receive_date TEXT,
            sample_type TEXT,
            debtor_id TEXT,
            source_pdf TEXT,
            source_pdf_hash TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS results_long (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sample_id TEXT,
            analysis_type TEXT,
            species TEXT,
            value INTEGER,
            unit TEXT,
            cysts INTEGER,
            lle INTEGER,
            infection_class TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS field_mapping (
            raw_field_name TEXT PRIMARY KEY,
            field_name TEXT
        )
        """
    )
    conn.commit()


def upsert_sample(conn: sqlite3.Connection, sample: ParsedSample) -> None:
    conn.execute(
        """
        INSERT INTO samples(sample_id, raw_field_name, field_name, report_date, receive_date,
                            sample_type, debtor_id, source_pdf, source_pdf_hash)
        VALUES(?, ?, (SELECT field_name FROM field_mapping WHERE raw_field_name = ?), ?, ?, ?, ?, ?, ?)
        ON CONFLICT(sample_id) DO UPDATE SET
            raw_field_name=excluded.raw_field_name,
            report_date=excluded.report_date,
            receive_date=excluded.receive_date,
            sample_type=excluded.sample_type,
            debtor_id=excluded.debtor_id,
            source_pdf=excluded.source_pdf,
            source_pdf_hash=excluded.source_pdf_hash,
            field_name=(SELECT field_name FROM field_mapping WHERE raw_field_name = excluded.raw_field_name)
        """,
        (
            sample.sample_id,
            sample.raw_field_name,
            sample.raw_field_name,
            sample.report_date,
            sample.receive_date,
            sample.sample_type,
            sample.debtor_id,
            sample.source_pdf,
            sample.source_pdf_hash,
        ),
    )

    conn.execute("DELETE FROM results_long WHERE sample_id = ?", (sample.sample_id,))
    conn.executemany(
        """
        INSERT INTO results_long(sample_id, analysis_type, species, value, unit, cysts, lle, infection_class)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                sample.sample_id,
                r.analysis_type,
                r.species,
                r.value,
                r.unit,
                r.cysts,
                r.lle,
                r.infection_class,
            )
            for r in sample.results
        ],
    )


def all_sample_ids_exist(conn: sqlite3.Connection, sample_ids: Iterable[str]) -> bool:
    ids = list(set(sample_ids))
    if not ids:
        return False
    placeholders = ",".join(["?"] * len(ids))
    rows = conn.execute(f"SELECT sample_id FROM samples WHERE sample_id IN ({placeholders})", ids).fetchall()
    return len(rows) == len(ids)


def append_failed_sample(path: Path, sample_id: str, reason: str, excerpt: str) -> None:
    rec = {
        "sample_id": sample_id,
        "reason": reason,
        "excerpt": excerpt[:500],
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def ingest_pdf(path: Path, conn: sqlite3.Connection, failed_path: Path) -> Tuple[int, int]:
    source_hash = compute_sha256(path)
    pages = extract_pdf_pages(path)
    blocks = split_samples_from_pages(pages)
    parsed_samples: List[ParsedSample] = []

    for sample_key, block_pages in blocks.items():
        try:
            parsed_samples.append(parse_sample_block(sample_key, block_pages, path.name, source_hash))
        except Exception as exc:  # broad by design for resilient batch
            excerpt = "\n".join(block_pages)
            append_failed_sample(failed_path, sample_key, f"parse_error: {exc}", excerpt)
            LOGGER.exception("Failed parsing sample block %s in %s", sample_key, path)

    if not parsed_samples:
        return 0, 0

    sample_ids = [s.sample_id for s in parsed_samples]
    already_same_hash = conn.execute(
        "SELECT COUNT(*) FROM samples WHERE source_pdf_hash = ? AND source_pdf = ?", (source_hash, path.name)
    ).fetchone()[0]

    if already_same_hash > 0 and all_sample_ids_exist(conn, sample_ids):
        LOGGER.info("Skipping %s (already ingested with same hash)", path.name)
        return 0, len(sample_ids)

    new_count = 0
    for sample in parsed_samples:
        exists = conn.execute("SELECT 1 FROM samples WHERE sample_id = ?", (sample.sample_id,)).fetchone()
        upsert_sample(conn, sample)
        if not exists:
            new_count += 1

        if not sample.results:
            append_failed_sample(failed_path, sample.sample_id, "no_results_parsed", sample.text)

    conn.commit()
    return new_count, 0


def ingest_directory(pdf_dir: Path, db_path: Path, failed_path: Path) -> Dict[str, int]:
    conn = sqlite3.connect(db_path)
    init_db(conn)

    total_new = 0
    total_skipped = 0

    pdf_files = sorted(pdf_dir.glob("*.pdf"))
    for pdf_file in pdf_files:
        LOGGER.info("Ingesting %s", pdf_file)
        new_count, skipped = ingest_pdf(pdf_file, conn, failed_path)
        total_new += new_count
        total_skipped += skipped

    conn.close()
    return {"new_samples": total_new, "skipped_samples": total_skipped, "pdf_count": len(pdf_files)}


def self_test() -> None:
    test_text = """
    Vrijlevende aaltjes
    Pratylenchus penetrans 120
    Cysteaaltjes 0 *
    Cysteaaltjes aantal cysten aantal lle besmettingsgraad
    Aardappelcysteaaltjes 2 15 licht besmet
    """
    vrij = parse_vrije_aaltjes(test_text)
    cys = parse_cysteaaltjes(test_text)
    assert len(vrij) == 1, "Vrijlevende parser should ignore Cysteaaltjes indicator row"
    assert vrij[0].species.lower().startswith("pratylenchus"), "Species parse mismatch"
    assert len(cys) == 1 and cys[0].lle == 15, "Cysteaaltjes parser mismatch"
    assert date_to_iso("5 maart 2024") == "2024-03-05", "Dutch date parsing failed"
    print("Self-test passed")


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Ingest HLB AALTJES ANALYSE PDFs into SQLite")
    p.add_argument("--pdf_dir", type=Path, help="Directory containing PDF files")
    p.add_argument("--db_path", type=Path, default=Path("nematodes.db"), help="SQLite DB path")
    p.add_argument(
        "--failed_path", type=Path, default=Path("failed_samples.jsonl"), help="Path for failed sample logs"
    )
    p.add_argument("--self_test", action="store_true", help="Run parser self-test and exit")
    return p


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s")
    args = build_arg_parser().parse_args()

    if args.self_test:
        self_test()
        return

    if not args.pdf_dir or not args.pdf_dir.exists():
        raise SystemExit("Gebruik --pdf_dir met een bestaande map")

    stats = ingest_directory(args.pdf_dir, args.db_path, args.failed_path)
    LOGGER.info("Klaar. PDF's: %(pdf_count)s | Nieuwe samples: %(new_samples)s | Overgeslagen: %(skipped_samples)s", stats)


if __name__ == "__main__":
    main()
