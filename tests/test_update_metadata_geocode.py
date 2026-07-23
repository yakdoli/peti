from datetime import date

from scripts.update_metadata_geocode import (
    PlaceCandidate,
    administrative_aliases,
    build_trie,
    candidate_score,
    detect_mentions,
    inferred_validity,
    legal_admin_level,
    normalize_korean_key,
    normalize_admin_code,
    scan_trie,
)


def candidate(name: str, code: str, *, valid_from: str = "", valid_to: str = "") -> PlaceCandidate:
    return PlaceCandidate(
        normalized_name=normalize_korean_key(name),
        display_name=name,
        match_source="test",
        match_level=legal_admin_level(code),
        admin_code=code,
        admin_name=name,
        parent_name="",
        parent_chain=tuple(),
        active=True,
        valid_from=valid_from,
        valid_to=valid_to,
        lon=None,
        lat=None,
        source_crs="",
        base_confidence=0.8,
    )


def test_scan_trie_prefers_longest_containing_match() -> None:
    trie = build_trie(["서울", "서울특별시", "서울특별시중구"])

    matches = scan_trie(trie, normalize_korean_key("서울특별시 중구 공고"))

    assert matches == [(0, len("서울특별시중구"), "서울특별시중구")]


def test_candidate_score_penalizes_out_of_range_temporal_alias() -> None:
    old = candidate("강원도", "4200000000", valid_to="2023-06-10")
    new = candidate("강원특별자치도", "5100000000", valid_from="2023-06-11")

    old_score, old_relation = candidate_score(
        candidate=old,
        mention_len=2,
        candidate_count=2,
        publication_date=date(2022, 1, 1),
    )
    new_score, new_relation = candidate_score(
        candidate=new,
        mention_len=2,
        candidate_count=2,
        publication_date=date(2022, 1, 1),
    )

    assert old_relation == "in_validity"
    assert new_relation == "before_validity"
    assert old_score > new_score


def test_inferred_validity_tracks_major_admin_renames() -> None:
    assert normalize_admin_code("42110") == "4211000000"
    assert inferred_validity("4211000000", "강원도 춘천시") == ("", "2023-06-10")
    assert inferred_validity("5111000000", "강원특별자치도 춘천시") == ("2023-06-11", "")
    assert inferred_validity("4900000000", "제주도") == ("", "2006-06-30")
    assert inferred_validity("5000000000", "제주특별자치도") == ("2006-07-01", "")


def test_detect_mentions_uses_title_and_agency_fields() -> None:
    index = {
        "안산시": [candidate("안산시", "4127100000")],
        "전북": [candidate("전라북도", "4500000000", valid_to="2024-01-17")],
    }
    trie = build_trie(index.keys())

    mentions = detect_mentions(
        item={
            "title": "안산시공고제2000-790호",
            "agency": "전북지방조달청",
            "publication_date": "2001-01-02",
        },
        trie=trie,
        index=index,
        max_mentions=4,
    )

    assert {mention.normalized_mention for mention in mentions} == {"안산시", "전북"}


def test_administrative_aliases_translate_renamed_province_descendants() -> None:
    assert "전라북도 전주시" in administrative_aliases("전북특별자치도 전주시")
    assert "전북특별자치도 전주시" in administrative_aliases("전라북도 전주시")
