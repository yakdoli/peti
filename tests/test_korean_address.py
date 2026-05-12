from __future__ import annotations

import pytest

from src.korean_address import (
    ADDRESS_TYPE_JIBUN,
    ADDRESS_TYPE_ROAD,
    AddressCoordinate,
    GPSCoordinate,
    JusoAPIError,
    JusoClient,
    convert_historical_address,
    detect_address_type,
    gps_from_juso_coordinate,
    normalize_korean_address,
    parse_search_response,
    tag_addresses_with_current_and_gps,
)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, *payloads):
        self.payloads = list(payloads)
        self.calls = []

    def get(self, url, *, params, timeout):
        self.calls.append({"url": url, "params": dict(params), "timeout": timeout})
        return FakeResponse(self.payloads.pop(0))


def juso_payload(*items, common=None):
    return {
        "results": {
            "common": {
                "errorCode": "0",
                "errorMessage": "정상",
                "totalCount": str(len(items)),
                "currentPage": "1",
                "countPerPage": "10",
                **(common or {}),
            },
            "juso": list(items),
        }
    }


SAMPLE_JUSO = {
    "roadAddr": "서울특별시 중구 세종대로 110(태평로1가)",
    "roadAddrPart1": "서울특별시 중구 세종대로 110",
    "roadAddrPart2": "(태평로1가)",
    "jibunAddr": "서울특별시 중구 태평로1가 31",
    "engAddr": "110, Sejong-daero, Jung-gu, Seoul",
    "zipNo": "04524",
    "admCd": "1114010300",
    "rnMgtSn": "111402005001",
    "bdMgtSn": "1114010300100310000000001",
    "bdNm": "서울특별시청",
    "bdKdcd": "0",
    "siNm": "서울특별시",
    "sggNm": "중구",
    "emdNm": "태평로1가",
    "liNm": "",
    "rn": "세종대로",
    "udrtYn": "0",
    "buldMnnm": "110",
    "buldSlno": "0",
    "mtYn": "0",
    "lnbrMnnm": "31",
    "lnbrSlno": "0",
    "emdNo": "01",
    "hstryYn": "0",
    "relJibun": "",
    "hemdNm": "명동",
}


def test_normalize_korean_address_removes_postal_prefix_and_extra_spaces():
    assert (
        normalize_korean_address("  (04524)  서울특별시   중구,  세종대로 110  ( 태평로1가 ) ")
        == "서울특별시 중구 세종대로 110 (태평로1가)"
    )


def test_detect_address_type_distinguishes_road_and_jibun():
    assert detect_address_type("서울특별시 중구 세종대로 110") == ADDRESS_TYPE_ROAD
    assert detect_address_type("서울특별시 중구 태평로1가 31") == ADDRESS_TYPE_JIBUN


def test_parse_search_response_maps_official_fields():
    result = parse_search_response(juso_payload(SAMPLE_JUSO), query="서울특별시 중구 세종대로 110")

    assert result.ok is True
    assert result.total_count == 1
    assert result.first is not None
    assert result.first.road_address == "서울특별시 중구 세종대로 110(태평로1가)"
    assert result.first.jibun_address == "서울특별시 중구 태평로1가 31"
    assert result.first.building_main_number == 110
    assert result.first.community_center == "명동"


def test_client_convert_uses_detected_sort_and_returns_both_address_systems():
    session = FakeSession(juso_payload(SAMPLE_JUSO))
    client = JusoClient(api_key="test-key", session=session)

    result = client.convert("서울특별시 중구 세종대로 110")

    assert result.input_type == ADDRESS_TYPE_ROAD
    assert result.road_address == "서울특별시 중구 세종대로 110(태평로1가)"
    assert result.jibun_address == "서울특별시 중구 태평로1가 31"
    assert session.calls[0]["params"]["firstSort"] == "road"
    assert session.calls[0]["params"]["hstryYn"] == "Y"
    assert session.calls[0]["params"]["addInfoYn"] == "Y"


def test_client_search_raises_on_juso_error_code():
    session = FakeSession(
        juso_payload(
            common={
                "errorCode": "E0008",
                "errorMessage": "검색어는 두글자 이상 입력되어야 합니다.",
                "totalCount": "0",
            }
        )
    )
    client = JusoClient(api_key="test-key", session=session)

    with pytest.raises(JusoAPIError) as exc:
        client.search("서울")

    assert exc.value.error_code == "E0008"


def test_fetch_coordinates_uses_required_juso_identifiers():
    coordinate_payload = juso_payload(
        {
            "entX": "953177.123",
            "entY": "1952056.456",
            "admCd": SAMPLE_JUSO["admCd"],
            "rnMgtSn": SAMPLE_JUSO["rnMgtSn"],
            "bdMgtSn": SAMPLE_JUSO["bdMgtSn"],
            "bdNm": SAMPLE_JUSO["bdNm"],
            "udrtYn": "0",
            "buldMnnm": "110",
            "buldSlno": "0",
        }
    )
    session = FakeSession(juso_payload(SAMPLE_JUSO), coordinate_payload)
    client = JusoClient(api_key="test-key", session=session)

    result = client.convert("서울특별시 중구 세종대로 110", include_coordinates=True)

    assert isinstance(result.coordinate, AddressCoordinate)
    assert result.coordinate.entrance_x == "953177.123"
    assert result.coordinate.entrance_y == "1952056.456"
    assert session.calls[1]["params"]["admCd"] == SAMPLE_JUSO["admCd"]
    assert session.calls[1]["params"]["buldMnnm"] == "110"


def test_gps_from_juso_coordinate_converts_epsg5179_to_wgs84():
    gps = gps_from_juso_coordinate("953177.123", "1952056.456")

    assert isinstance(gps, GPSCoordinate)
    assert gps.crs == "EPSG:4326"
    assert gps.source_crs == "EPSG:5179"
    assert 37.5 < gps.latitude < 37.7
    assert 126.8 < gps.longitude < 127.1


def test_convert_historical_address_returns_current_address_and_gps_tag():
    historical_match = {**SAMPLE_JUSO, "hstryYn": "1"}
    coordinate_payload = juso_payload(
        {
            "entX": "953177.123",
            "entY": "1952056.456",
            "admCd": SAMPLE_JUSO["admCd"],
            "rnMgtSn": SAMPLE_JUSO["rnMgtSn"],
            "bdMgtSn": SAMPLE_JUSO["bdMgtSn"],
            "bdNm": SAMPLE_JUSO["bdNm"],
            "udrtYn": "0",
            "buldMnnm": "110",
            "buldSlno": "0",
        }
    )
    session = FakeSession(juso_payload(historical_match), coordinate_payload)
    client = JusoClient(api_key="test-key", session=session)

    result = convert_historical_address("서울특별시 중구 태평로1가 31", client=client)

    assert result.is_history_match is True
    assert result.current_address == "서울특별시 중구 세종대로 110(태평로1가)"
    assert result.road_address == "서울특별시 중구 세종대로 110(태평로1가)"
    assert result.gps_coordinate is not None
    assert result.to_dict()["gps_coordinate"]["crs"] == "EPSG:4326"


def test_tag_addresses_with_current_and_gps_returns_compact_batch_records():
    session = FakeSession(
        juso_payload(SAMPLE_JUSO),
        juso_payload(
            {
                "entX": "953177.123",
                "entY": "1952056.456",
                "admCd": SAMPLE_JUSO["admCd"],
                "rnMgtSn": SAMPLE_JUSO["rnMgtSn"],
                "bdMgtSn": SAMPLE_JUSO["bdMgtSn"],
                "bdNm": SAMPLE_JUSO["bdNm"],
                "udrtYn": "0",
                "buldMnnm": "110",
                "buldSlno": "0",
            }
        ),
    )
    client = JusoClient(api_key="test-key", session=session)

    tagged = tag_addresses_with_current_and_gps(["서울특별시 중구 세종대로 110"], client=client)

    assert len(tagged) == 1
    assert tagged[0]["source_address"] == "서울특별시 중구 세종대로 110"
    assert tagged[0]["current_address"] == "서울특별시 중구 세종대로 110(태평로1가)"
    assert tagged[0]["jibun_address"] == "서울특별시 중구 태평로1가 31"
    assert tagged[0]["zip_no"] == "04524"
    assert tagged[0]["is_history_match"] is False
    assert tagged[0]["gps_coordinate"]["crs"] == "EPSG:4326"
