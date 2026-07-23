#!/usr/bin/env python3
"""Enrich pety/searchThema metadata with year-aware Korean place geocodes.

The source metadata JSON files are large generated artifacts, so this script
updates the analytics DuckDB and writes a JSONL sidecar instead of rewriting
every item JSON. The full item JSON can be patched from the sidecar later.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


KEEP_NORMALIZED_RE = re.compile(r"[^0-9A-Za-z가-힣]+")
MIN_GENERIC_PLACE_LEN = 3
DEFAULT_BATCH_SIZE = 10_000
DEFAULT_MAX_MENTIONS_PER_ITEM = 8


@dataclass(frozen=True)
class PlaceCandidate:
    normalized_name: str
    display_name: str
    match_source: str
    match_level: str
    admin_code: str
    admin_name: str
    parent_name: str
    parent_chain: tuple[str, ...]
    active: bool | None
    valid_from: str
    valid_to: str
    lon: float | None
    lat: float | None
    source_crs: str
    base_confidence: float
    temporal_note: str = ""


@dataclass(frozen=True)
class MentionMatch:
    field: str
    mention_text: str
    normalized_mention: str
    start: int
    end: int
    candidate: PlaceCandidate
    candidate_count: int
    confidence: float
    temporal_relation: str


@dataclass
class TrieNode:
    children: dict[str, "TrieNode"] = field(default_factory=dict)
    key: str | None = None


def normalize_korean_key(value: Any) -> str:
    text = "" if value is None else str(value)
    text = unicodedata.normalize("NFKC", text).casefold()
    return KEEP_NORMALIZED_RE.sub("", text)


def legal_admin_level(code: str) -> str:
    if len(code) != 10 or not code.isdigit():
        return "unknown"
    if code[2:] == "00000000":
        return "sido"
    if code[5:] == "00000":
        return "sigungu"
    if code[8:] == "00":
        return "eupmyeondong"
    return "ri"


def normalize_admin_code(value: Any) -> str:
    code = str(value or "").strip()
    if len(code) == 2 and code.isdigit():
        return f"{code}00000000"
    if len(code) == 5 and code.isdigit():
        return f"{code}00000"
    return code


def inferred_validity(admin_code: str, admin_name: str) -> tuple[str, str]:
    if admin_code.startswith("42") or admin_name.startswith("강원도"):
        return "", "2023-06-10"
    if admin_code.startswith("51") or admin_name.startswith("강원특별자치도"):
        return "2023-06-11", ""
    if admin_code.startswith("45") or admin_name.startswith("전라북도"):
        return "", "2024-01-17"
    if admin_code.startswith("52") or admin_name.startswith("전북특별자치도"):
        return "2024-01-18", ""
    if admin_code.startswith("49") or admin_name.startswith("제주도"):
        return "", "2006-06-30"
    if admin_code.startswith("50") or admin_name.startswith("제주특별자치도"):
        return "2006-07-01", ""
    if admin_name.startswith("세종특별자치시"):
        return "2012-07-01", ""
    return "", ""


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def query_records(con: Any, sql: str, params: Sequence[Any] | None = None) -> list[dict[str, Any]]:
    cursor = con.execute(sql, params or [])
    keys = [description[0] for description in cursor.description]
    return [dict(zip(keys, row, strict=True)) for row in cursor.fetchall()]


def parse_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value)
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def date_in_range(publication_date: date | None, valid_from: str, valid_to: str) -> str:
    if publication_date is None:
        return "unknown"
    start = parse_date(valid_from) if valid_from else None
    end = parse_date(valid_to) if valid_to else None
    if start and publication_date < start:
        return "before_validity"
    if end and publication_date > end:
        return "after_validity"
    if start or end:
        return "in_validity"
    return "not_versioned"


def temporal_score(publication_date: date | None, candidate: PlaceCandidate) -> tuple[float, str]:
    relation = date_in_range(publication_date, candidate.valid_from, candidate.valid_to)
    if relation == "in_validity":
        return 0.12, relation
    if relation in {"before_validity", "after_validity"}:
        return -0.25, relation
    return 0.0, relation


def candidate_score(
    *,
    candidate: PlaceCandidate,
    mention_len: int,
    candidate_count: int,
    publication_date: date | None,
) -> tuple[float, str]:
    temporal_bonus, relation = temporal_score(publication_date, candidate)
    active_bonus = 0.04 if candidate.active is True else 0.0
    coordinate_bonus = 0.04 if candidate.lon is not None and candidate.lat is not None else 0.0
    length_bonus = min(0.10, max(0, mention_len - 2) * 0.012)
    ambiguity_penalty = min(0.20, math.log10(max(candidate_count, 1)) * 0.05)
    confidence = candidate.base_confidence + temporal_bonus + active_bonus + coordinate_bonus + length_bonus - ambiguity_penalty
    return max(0.05, min(0.99, confidence)), relation


def add_candidate(index: dict[str, list[PlaceCandidate]], candidate: PlaceCandidate) -> None:
    key = candidate.normalized_name
    if not key:
        return
    if len(key) < MIN_GENERIC_PLACE_LEN and candidate.match_level != "sido":
        return
    index[key].append(candidate)


def add_candidate_variant(
    index: dict[str, list[PlaceCandidate]],
    *,
    source: PlaceCandidate,
    variant: str,
    base_confidence_delta: float,
    temporal_note: str = "",
    valid_from: str | None = None,
    valid_to: str | None = None,
) -> None:
    key = normalize_korean_key(variant)
    if not key:
        return
    add_candidate(
        index,
        PlaceCandidate(
            normalized_name=key,
            display_name=variant,
            match_source=source.match_source,
            match_level=source.match_level,
            admin_code=source.admin_code,
            admin_name=source.admin_name,
            parent_name=source.parent_name,
            parent_chain=source.parent_chain,
            active=source.active,
            valid_from=source.valid_from if valid_from is None else valid_from,
            valid_to=source.valid_to if valid_to is None else valid_to,
            lon=source.lon,
            lat=source.lat,
            source_crs=source.source_crs,
            base_confidence=max(0.05, min(0.98, source.base_confidence + base_confidence_delta)),
            temporal_note=temporal_note or source.temporal_note,
        ),
    )


def build_trie(keys: Iterable[str]) -> TrieNode:
    root = TrieNode()
    for key in keys:
        node = root
        for char in key:
            node = node.children.setdefault(char, TrieNode())
        node.key = key
    return root


def scan_trie(root: TrieNode, text: str) -> list[tuple[int, int, str]]:
    matches: list[tuple[int, int, str]] = []
    for start in range(len(text)):
        node = root
        best: tuple[int, int, str] | None = None
        for index in range(start, len(text)):
            node = node.children.get(text[index])
            if node is None:
                break
            if node.key is not None:
                best = (start, index + 1, node.key)
        if best is not None:
            matches.append(best)
    return drop_contained_matches(matches)


def drop_contained_matches(matches: Sequence[tuple[int, int, str]]) -> list[tuple[int, int, str]]:
    ordered = sorted(matches, key=lambda row: (-(row[1] - row[0]), row[0], row[1]))
    selected: list[tuple[int, int, str]] = []
    occupied: list[tuple[int, int]] = []
    for start, end, key in ordered:
        if any(start >= used_start and end <= used_end for used_start, used_end in occupied):
            continue
        selected.append((start, end, key))
        occupied.append((start, end))
    return sorted(selected, key=lambda row: (row[0], row[1]))


def pick_candidate(
    candidates: Sequence[PlaceCandidate],
    *,
    mention_len: int,
    publication_date: date | None,
) -> tuple[PlaceCandidate, float, str]:
    candidate_count = len(candidates)
    scored = [
        (
            candidate_score(
                candidate=candidate,
                mention_len=mention_len,
                candidate_count=candidate_count,
                publication_date=publication_date,
            ),
            candidate,
        )
        for candidate in candidates
    ]
    scored.sort(
        key=lambda row: (
            row[0][0],
            row[1].match_level == "sido",
            row[1].active is True,
            len(row[1].admin_name),
        ),
        reverse=True,
    )
    (confidence, relation), candidate = scored[0]
    return candidate, confidence, relation


def candidate_to_json(candidate: PlaceCandidate, *, candidate_count: int, temporal_relation: str) -> dict[str, Any]:
    return {
        "display_name": candidate.display_name,
        "match_source": candidate.match_source,
        "match_level": candidate.match_level,
        "admin_code": candidate.admin_code,
        "admin_name": candidate.admin_name,
        "parent_name": candidate.parent_name,
        "parent_chain": list(candidate.parent_chain),
        "active": candidate.active,
        "valid_from": candidate.valid_from,
        "valid_to": candidate.valid_to,
        "temporal_relation": temporal_relation,
        "candidate_count": candidate_count,
        "lon": candidate.lon,
        "lat": candidate.lat,
        "source_crs": candidate.source_crs,
        "temporal_note": candidate.temporal_note,
    }


def build_place_index(geocode_db: Path, *, include_inactive: bool) -> dict[str, list[PlaceCandidate]]:
    try:
        import duckdb  # type: ignore
    except ImportError as exc:
        raise RuntimeError("duckdb is required; use `uv run --with duckdb==1.5.3`") from exc

    con = duckdb.connect(str(geocode_db), read_only=True)
    index: dict[str, list[PlaceCandidate]] = defaultdict(list)
    coordinate_rows = con.execute(
        """
        SELECT admin_code, lon, lat, source_crs
        FROM geo.place_names
        WHERE admin_code IS NOT NULL AND lon IS NOT NULL AND lat IS NOT NULL
        UNION ALL
        SELECT admin_code, representative_longitude, representative_latitude, representative_point_crs
        FROM geo.raw_vworld_boundary_observations
        WHERE admin_code IS NOT NULL
          AND representative_longitude IS NOT NULL
          AND representative_latitude IS NOT NULL
        UNION ALL
        SELECT admin_code, representative_longitude, representative_latitude, representative_point_crs
        FROM geo.raw_admin_area_observations
        WHERE admin_code IS NOT NULL
          AND representative_longitude IS NOT NULL
          AND representative_latitude IS NOT NULL
        """
    ).fetchall()
    coords: dict[str, tuple[float | None, float | None, str]] = {}
    for admin_code, lon, lat, crs in coordinate_rows:
        coords.setdefault(str(admin_code), (lon, lat, crs or "EPSG:4326"))

    legal_rows = con.execute(
        """
        SELECT legal_dong_code, legal_dong_name, status, active
        FROM geo.raw_geocoder_legal_dong_codes
        WHERE legal_dong_name IS NOT NULL
          AND (? OR active)
        """,
        [include_inactive],
    ).fetchall()
    for code, name, status, active in legal_rows:
        code = str(code or "")
        name = str(name or "")
        parts = tuple(part for part in name.split() if part)
        if not parts:
            continue
        level = legal_admin_level(code)
        valid_from, valid_to = inferred_validity(code, name)
        lon, lat, crs = coords.get(code, (None, None, ""))
        candidate = PlaceCandidate(
            normalized_name=normalize_korean_key(name),
            display_name=name,
            match_source="legal_dong_code",
            match_level=level,
            admin_code=code,
            admin_name=name,
            parent_name=parts[-2] if len(parts) > 1 else "",
            parent_chain=parts[:-1],
            active=bool(active),
            valid_from=valid_from,
            valid_to=valid_to,
            lon=lon,
            lat=lat,
            source_crs=crs,
            base_confidence=0.82 if level in {"sido", "sigungu"} else 0.72,
            temporal_note=str(status or ""),
        )
        add_candidate(index, candidate)
        leaf = parts[-1]
        if leaf != name:
            add_candidate_variant(index, source=candidate, variant=leaf, base_confidence_delta=-0.14)
        for alias in administrative_aliases(name):
            add_candidate_variant(
                index,
                source=candidate,
                variant=alias,
                base_confidence_delta=-0.05,
                temporal_note="administrative_alias",
            )

    admin_rows = con.execute(
        """
        SELECT
            sido_name,
            sigungu_name,
            coalesce(sigungu_code, latest_sigungu_code, sido_code, latest_sido_code) AS admin_code
        FROM geo.raw_geocoder_admin_code_matches
        WHERE sigungu_name IS NOT NULL
        """
    ).fetchall()
    for sido_name, sigungu_name, code in admin_rows:
        sido_name = str(sido_name or "")
        sigungu_name = str(sigungu_name or "")
        full = f"{sido_name} {sigungu_name}".strip()
        admin_code = normalize_admin_code(code)
        valid_from, valid_to = inferred_validity(admin_code, full)
        parts = tuple(part for part in full.split() if part)
        candidate = PlaceCandidate(
            normalized_name=normalize_korean_key(full),
            display_name=full,
            match_source="admin_code_match",
            match_level="sigungu",
            admin_code=admin_code,
            admin_name=full,
            parent_name=sido_name,
            parent_chain=(sido_name,) if sido_name else tuple(),
            active=None,
            valid_from=valid_from,
            valid_to=valid_to,
            lon=None,
            lat=None,
            source_crs="",
            base_confidence=0.76,
            temporal_note="geocoder_code_match",
        )
        if parts:
            add_candidate(index, candidate)
            add_candidate_variant(index, source=candidate, variant=sigungu_name, base_confidence_delta=-0.08)

    con.close()
    return dict(index)


def administrative_aliases(name: str) -> list[str]:
    aliases = {
        "서울특별시": ["서울", "서울시"],
        "부산광역시": ["부산", "부산시"],
        "대구광역시": ["대구", "대구시"],
        "인천광역시": ["인천", "인천시"],
        "광주광역시": ["광주", "광주시"],
        "대전광역시": ["대전", "대전시"],
        "울산광역시": ["울산", "울산시"],
        "세종특별자치시": ["세종", "세종시"],
        "경기도": ["경기"],
        "충청북도": ["충북"],
        "충청남도": ["충남"],
        "전라남도": ["전남"],
        "경상북도": ["경북"],
        "경상남도": ["경남"],
        "제주도": ["제주"],
        "제주특별자치도": ["제주"],
        "강원도": ["강원"],
        "강원특별자치도": ["강원"],
        "전라북도": ["전북"],
        "전북특별자치도": ["전북", "전북도"],
    }.get(name, [])
    transitions = (
        ("제주도", "제주특별자치도"),
        ("강원도", "강원특별자치도"),
        ("전라북도", "전북특별자치도"),
    )
    for old_name, current_name in transitions:
        for source, target in ((old_name, current_name), (current_name, old_name)):
            if name == source or name.startswith(f"{source} "):
                aliases.append(f"{target}{name[len(source):]}")
    return list(dict.fromkeys(aliases))


def detect_mentions(
    *,
    item: Mapping[str, Any],
    trie: TrieNode,
    index: Mapping[str, list[PlaceCandidate]],
    max_mentions: int,
) -> list[MentionMatch]:
    publication_date = parse_date(item.get("publication_date") or item.get("date_text"))
    seen: set[tuple[str, str, str]] = set()
    results: list[MentionMatch] = []
    for field_name in ("title", "agency"):
        field_value = item.get(field_name)
        if not field_value:
            continue
        normalized_text = normalize_korean_key(field_value)
        for start, end, key in scan_trie(trie, normalized_text):
            candidates = index.get(key) or []
            if not candidates:
                continue
            candidate, confidence, relation = pick_candidate(
                candidates,
                mention_len=end - start,
                publication_date=publication_date,
            )
            dedupe_key = (field_name, key, candidate.admin_code or candidate.admin_name)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            results.append(
                MentionMatch(
                    field=field_name,
                    mention_text=normalized_text[start:end],
                    normalized_mention=key,
                    start=start,
                    end=end,
                    candidate=candidate,
                    candidate_count=len(candidates),
                    confidence=round(confidence, 4),
                    temporal_relation=relation,
                )
            )
    results.sort(key=lambda row: (row.confidence, row.end - row.start), reverse=True)
    return results[:max_mentions]


def iter_metadata_items(con: Any, *, sources: Sequence[str], limit: int | None) -> Iterable[dict[str, Any]]:
    source_filter = ""
    params: list[Any] = []
    if sources:
        placeholders = ", ".join(["?"] * len(sources))
        source_filter = f"WHERE source_key IN ({placeholders})"
        params.extend(sources)
    limit_sql = ""
    if limit is not None:
        limit_sql = " LIMIT ?"
        params.append(limit)
    query = f"""
        SELECT
            item_key,
            source_key,
            id,
            title,
            agency,
            publication_date,
            date_text,
            year
        FROM metadata_items
        {source_filter}
        ORDER BY source_key, year, item_key
        {limit_sql}
    """
    cursor = con.execute(query, params)
    columns = [description[0] for description in cursor.description]
    while True:
        rows = cursor.fetchmany(DEFAULT_BATCH_SIZE)
        if not rows:
            break
        for row in rows:
            yield dict(zip(columns, row, strict=True))


def mention_row(item: Mapping[str, Any], mention: MentionMatch, rank: int, run_id: str) -> list[Any]:
    candidate = mention.candidate
    candidate_json = candidate_to_json(
        candidate,
        candidate_count=mention.candidate_count,
        temporal_relation=mention.temporal_relation,
    )
    return [
        run_id,
        item.get("item_key"),
        item.get("source_key"),
        item.get("id"),
        item.get("publication_date"),
        item.get("year"),
        rank,
        mention.field,
        mention.mention_text,
        mention.normalized_mention,
        mention.start,
        mention.end,
        candidate.match_source,
        candidate.match_level,
        candidate.admin_code,
        candidate.admin_name,
        candidate.parent_name,
        json_dumps(list(candidate.parent_chain)),
        mention.candidate_count,
        mention.confidence,
        mention.temporal_relation,
        candidate.valid_from,
        candidate.valid_to,
        candidate.lon,
        candidate.lat,
        candidate.source_crs,
        json_dumps(candidate_json),
    ]


def item_geocode_record(item: Mapping[str, Any], mentions: Sequence[MentionMatch], run_id: str) -> dict[str, Any]:
    payload_mentions = [
        {
            "rank": index + 1,
            "field": mention.field,
            "mention_text": mention.mention_text,
            "normalized_mention": mention.normalized_mention,
            "start": mention.start,
            "end": mention.end,
            "confidence": mention.confidence,
            "geocode": candidate_to_json(
                mention.candidate,
                candidate_count=mention.candidate_count,
                temporal_relation=mention.temporal_relation,
            ),
        }
        for index, mention in enumerate(mentions)
    ]
    best = mentions[0] if mentions else None
    return {
        "run_id": run_id,
        "item_key": item.get("item_key"),
        "source_key": item.get("source_key"),
        "id": item.get("id"),
        "publication_date": str(item.get("publication_date") or item.get("date_text") or ""),
        "year": item.get("year"),
        "geocode_status": "matched" if best else "no_place_mention",
        "mention_count": len(mentions),
        "best_admin_code": best.candidate.admin_code if best else "",
        "best_place_name": best.candidate.display_name if best else "",
        "best_confidence": best.confidence if best else None,
        "geocode": {
            "status": "matched" if best else "no_place_mention",
            "run_id": run_id,
            "generated_at": run_id,
            "source": "metadata_title_agency_geocode",
            "publication_year": item.get("year"),
            "mentions": payload_mentions,
        },
    }


MENTION_COLUMNS = [
    "run_id",
    "item_key",
    "source_key",
    "id",
    "publication_date",
    "year",
    "mention_rank",
    "source_field",
    "mention_text",
    "normalized_mention",
    "mention_start",
    "mention_end",
    "match_source",
    "match_level",
    "admin_code",
    "admin_name",
    "parent_name",
    "parent_chain_json",
    "candidate_count",
    "confidence",
    "temporal_relation",
    "valid_from",
    "valid_to",
    "lon",
    "lat",
    "source_crs",
    "geocode_json",
]

ITEM_COLUMNS = [
    "run_id",
    "item_key",
    "source_key",
    "id",
    "publication_date",
    "year",
    "geocode_status",
    "mention_count",
    "best_admin_code",
    "best_place_name",
    "best_confidence",
    "geocode_json",
]


def create_output_schema(con: Any) -> None:
    con.execute("DROP VIEW IF EXISTS v_metadata_geocode_year_summary")
    con.execute("DROP VIEW IF EXISTS v_metadata_geocode_place_year_summary")
    con.execute("DROP TABLE IF EXISTS metadata_geocode_mentions")
    con.execute("DROP TABLE IF EXISTS metadata_item_geocode")
    con.execute(
        """
        CREATE TABLE metadata_geocode_mentions (
            run_id VARCHAR,
            item_key VARCHAR,
            source_key VARCHAR,
            id VARCHAR,
            publication_date DATE,
            year INTEGER,
            mention_rank INTEGER,
            source_field VARCHAR,
            mention_text VARCHAR,
            normalized_mention VARCHAR,
            mention_start INTEGER,
            mention_end INTEGER,
            match_source VARCHAR,
            match_level VARCHAR,
            admin_code VARCHAR,
            admin_name VARCHAR,
            parent_name VARCHAR,
            parent_chain_json VARCHAR,
            candidate_count INTEGER,
            confidence DOUBLE,
            temporal_relation VARCHAR,
            valid_from VARCHAR,
            valid_to VARCHAR,
            lon DOUBLE,
            lat DOUBLE,
            source_crs VARCHAR,
            geocode_json VARCHAR
        )
        """
    )
    con.execute(
        """
        CREATE TABLE metadata_item_geocode (
            run_id VARCHAR,
            item_key VARCHAR,
            source_key VARCHAR,
            id VARCHAR,
            publication_date VARCHAR,
            year INTEGER,
            geocode_status VARCHAR,
            mention_count INTEGER,
            best_admin_code VARCHAR,
            best_place_name VARCHAR,
            best_confidence DOUBLE,
            geocode_json VARCHAR
        )
        """
    )


def create_views(con: Any) -> None:
    con.execute(
        """
        CREATE OR REPLACE VIEW v_metadata_geocode_year_summary AS
        SELECT
            i.source_key,
            i.year,
            count(*) AS item_count,
            sum(CASE WHEN g.geocode_status = 'matched' THEN 1 ELSE 0 END) AS geocoded_item_count,
            sum(coalesce(g.mention_count, 0)) AS mention_count,
            round(avg(g.best_confidence), 4) AS avg_best_confidence
        FROM metadata_items i
        LEFT JOIN metadata_item_geocode g USING (item_key)
        GROUP BY i.source_key, i.year
        """
    )
    con.execute(
        """
        CREATE OR REPLACE VIEW v_metadata_geocode_place_year_summary AS
        SELECT
            source_key,
            year,
            admin_code,
            admin_name,
            match_level,
            count(*) AS mention_count,
            count(DISTINCT item_key) AS item_count,
            round(avg(confidence), 4) AS avg_confidence
        FROM metadata_geocode_mentions
        GROUP BY source_key, year, admin_code, admin_name, match_level
        """
    )


def ensure_metadata_items_columns(con: Any) -> None:
    existing = {
        row[1]
        for row in con.execute("PRAGMA table_info('metadata_items')").fetchall()
    }
    columns = {
        "geocode_status": "VARCHAR",
        "geocode_json": "VARCHAR",
        "geocode_updated_at": "VARCHAR",
    }
    for column, column_type in columns.items():
        if column not in existing:
            con.execute(f"ALTER TABLE metadata_items ADD COLUMN {quote_identifier(column)} {column_type}")


def insert_batch(con: Any, table: str, columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> None:
    if not rows:
        return
    placeholders = ", ".join(["?"] * len(columns))
    column_sql = ", ".join(quote_identifier(column) for column in columns)
    con.executemany(f"INSERT INTO {table} ({column_sql}) VALUES ({placeholders})", rows)


def apply_metadata_item_update(con: Any, run_id: str) -> None:
    ensure_metadata_items_columns(con)
    con.execute(
        """
        UPDATE metadata_items AS i
        SET
            geocode_status = g.geocode_status,
            geocode_json = g.geocode_json,
            geocode_updated_at = ?
        FROM metadata_item_geocode AS g
        WHERE i.item_key = g.item_key
        """,
        [run_id],
    )


def run_update(args: argparse.Namespace) -> dict[str, Any]:
    try:
        import duckdb  # type: ignore
    except ImportError as exc:
        raise RuntimeError("duckdb is required; use `uv run --with duckdb==1.5.3`") from exc

    db_path = Path(args.metadata_db).resolve()
    geocode_db = Path(args.geocode_db).resolve()
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now().astimezone().isoformat(timespec="seconds")

    place_index = build_place_index(geocode_db, include_inactive=args.include_inactive)
    trie = build_trie(place_index.keys())

    con = duckdb.connect(str(db_path))
    items = list(iter_metadata_items(con, sources=args.sources, limit=args.limit))
    create_output_schema(con)
    mention_rows: list[list[Any]] = []
    item_rows: list[list[Any]] = []
    jsonl_path = output_dir / "metadata_geocode_items.jsonl"
    counts: Counter[str] = Counter()
    source_counts: Counter[str] = Counter()
    source_matched_counts: Counter[str] = Counter()
    year_counts: Counter[str] = Counter()
    place_counter: Counter[str] = Counter()

    with jsonl_path.open("w", encoding="utf-8") as jsonl:
        for item in items:
            counts["items_seen"] += 1
            source_key = str(item.get("source_key") or "")
            year = str(item.get("year") or "")
            source_counts[source_key] += 1
            year_counts[f"{source_key}:{year}"] += 1
            mentions = detect_mentions(
                item=item,
                trie=trie,
                index=place_index,
                max_mentions=args.max_mentions_per_item,
            )
            record = item_geocode_record(item, mentions, run_id)
            if mentions:
                counts["items_matched"] += 1
                source_matched_counts[source_key] += 1
            jsonl.write(json_dumps(record) + "\n")
            item_rows.append(
                [
                    record["run_id"],
                    record["item_key"],
                    record["source_key"],
                    record["id"],
                    record["publication_date"],
                    record["year"],
                    record["geocode_status"],
                    record["mention_count"],
                    record["best_admin_code"],
                    record["best_place_name"],
                    record["best_confidence"],
                    json_dumps(record["geocode"]),
                ]
            )
            for rank, mention in enumerate(mentions, start=1):
                counts["mentions"] += 1
                place_counter[f"{mention.candidate.admin_code}:{mention.candidate.display_name}"] += 1
                mention_rows.append(mention_row(item, mention, rank, run_id))
            if len(item_rows) >= args.batch_size:
                insert_batch(con, "metadata_item_geocode", ITEM_COLUMNS, item_rows)
                item_rows.clear()
            if len(mention_rows) >= args.batch_size:
                insert_batch(con, "metadata_geocode_mentions", MENTION_COLUMNS, mention_rows)
                mention_rows.clear()
    insert_batch(con, "metadata_item_geocode", ITEM_COLUMNS, item_rows)
    insert_batch(con, "metadata_geocode_mentions", MENTION_COLUMNS, mention_rows)
    con.execute("CREATE INDEX idx_metadata_item_geocode_item_key ON metadata_item_geocode(item_key)")
    con.execute("CREATE INDEX idx_metadata_geocode_mentions_source_year ON metadata_geocode_mentions(source_key, year)")
    con.execute("CREATE INDEX idx_metadata_geocode_mentions_admin_code ON metadata_geocode_mentions(admin_code)")
    create_views(con)
    if args.update_metadata_items:
        apply_metadata_item_update(con, run_id)
    summary_rows = query_records(
        con,
        """
        SELECT *
        FROM v_metadata_geocode_year_summary
        ORDER BY source_key, year
        """,
    )
    top_places = query_records(
        con,
        """
        SELECT *
        FROM v_metadata_geocode_place_year_summary
        ORDER BY mention_count DESC, source_key, year
        LIMIT 50
        """,
    )
    table_counts = {
        "metadata_item_geocode": con.execute("SELECT count(*) FROM metadata_item_geocode").fetchone()[0],
        "metadata_geocode_mentions": con.execute("SELECT count(*) FROM metadata_geocode_mentions").fetchone()[0],
    }
    con.close()

    summary = {
        "run_id": run_id,
        "metadata_db": str(db_path),
        "geocode_db": str(geocode_db),
        "output_jsonl": str(jsonl_path),
        "place_index_keys": len(place_index),
        "place_candidate_count": sum(len(values) for values in place_index.values()),
        "sources": args.sources,
        "limit": args.limit,
        "counts": dict(counts),
        "source_counts": dict(source_counts),
        "source_matched_counts": dict(source_matched_counts),
        "table_counts": table_counts,
        "year_summary": summary_rows,
        "top_places": top_places,
        "updated_metadata_items": bool(args.update_metadata_items),
    }
    summary_path = output_dir / "metadata_geocode_summary.json"
    summary_path.write_text(json_dumps(summary) + "\n", encoding="utf-8")
    summary["summary_path"] = str(summary_path)
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metadata-db", default="artifacts/analytics/peti_metadata.duckdb")
    parser.add_argument("--geocode-db", default="/home/yakdoli/workspace/korean-ocr-pipeline/work/geocode/geocode.duckdb")
    parser.add_argument("--output-dir", default="artifacts/analytics/geocode")
    parser.add_argument("--sources", nargs="*", default=["pety", "searchThema"])
    parser.add_argument("--limit", type=int)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--max-mentions-per-item", type=int, default=DEFAULT_MAX_MENTIONS_PER_ITEM)
    parser.add_argument(
        "--include-inactive",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="include inactive legal-dong names so historical publications can still match old place names",
    )
    parser.add_argument("--update-metadata-items", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = run_update(args)
    print(json_dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
