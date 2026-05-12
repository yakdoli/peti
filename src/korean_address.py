"""Korean address normalization and conversion helpers.

The module wraps the official Juso road-name address APIs while keeping the
conversion surface small enough to test with mocked HTTP responses.

Primary reference:
https://eng.juso.go.kr/addrlink/openApi/searchApi.do
"""

from __future__ import annotations

import os
import re
from math import atan, cos, degrees, radians, sin, sqrt, tan
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Protocol

import requests


JUSO_API_KEY_ENV = "JUSO_API_KEY"
JUSO_SEARCH_URL = "https://business.juso.go.kr/addrlink/addrLinkApi.do"
JUSO_COORD_URL = "https://business.juso.go.kr/addrlink/addrCoordApi.do"
JUSO_COORD_CRS = "EPSG:5179"
GPS_CRS = "EPSG:4326"

ADDRESS_TYPE_ROAD = "road"
ADDRESS_TYPE_JIBUN = "jibun"
ADDRESS_TYPE_UNKNOWN = "unknown"

_WHITESPACE_RE = re.compile(r"\s+")
_ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\ufeff]")
_POSTAL_PREFIX_RE = re.compile(r"^\s*(?:\(?\d{5}\)?|\[\d{5}\]|\d{3}-\d{3})\s+")
_ROAD_ADDRESS_RE = re.compile(r"(?:^|\s)[가-힣A-Za-z0-9·.-]+(?:대로|로|길)\s+\d+(?:-\d+)?(?:\s|$|,|\))")
_JIBUN_ADDRESS_RE = re.compile(r"(?:^|\s)[가-힣A-Za-z0-9·.-]+(?:읍|면|동|리|가)\s+(?:산\s*)?\d+(?:-\d+)?(?:번지?)?")
_DISALLOWED_KEYWORD_RE = re.compile(r"[%=><\[\]]")


class HTTPClient(Protocol):
    """Minimal protocol implemented by ``requests.Session``."""

    def get(self, url: str, *, params: Mapping[str, Any], timeout: float) -> Any:
        """Perform a GET request."""


class JusoClientError(RuntimeError):
    """Base error for Juso client failures."""


class JusoAPIError(JusoClientError):
    """Raised when the Juso API returns a non-zero error code."""

    def __init__(self, error_code: str, error_message: str):
        super().__init__(f"Juso API error {error_code}: {error_message}")
        self.error_code = error_code
        self.error_message = error_message


@dataclass(frozen=True)
class GPSCoordinate:
    """WGS84 latitude/longitude suitable for GPS and web maps."""

    latitude: float
    longitude: float
    crs: str = GPS_CRS
    source_crs: str = JUSO_COORD_CRS
    source_x: str = ""
    source_y: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serializable representation."""

        return {
            "latitude": self.latitude,
            "longitude": self.longitude,
            "crs": self.crs,
            "source_crs": self.source_crs,
            "source_x": self.source_x,
            "source_y": self.source_y,
        }


@dataclass(frozen=True)
class KoreanAddressRecord:
    """A normalized record returned by the Juso search API."""

    road_address: str = ""
    road_address_without_detail: str = ""
    road_address_reference: str = ""
    jibun_address: str = ""
    english_address: str = ""
    zip_no: str = ""
    administrative_code: str = ""
    road_name_code: str = ""
    building_management_number: str = ""
    detail_building_names: str = ""
    building_name: str = ""
    building_kind_code: str = ""
    sido: str = ""
    sigungu: str = ""
    eupmyeondong: str = ""
    li: str = ""
    road_name: str = ""
    underground_yn: str = ""
    building_main_number: Optional[int] = None
    building_sub_number: Optional[int] = None
    mountain_yn: str = ""
    lot_main_number: Optional[int] = None
    lot_sub_number: Optional[int] = None
    emd_serial_number: str = ""
    history_yn: str = ""
    related_jibun: str = ""
    community_center: str = ""
    source_query: str = ""
    raw: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_juso(cls, data: Mapping[str, Any], source_query: str = "") -> "KoreanAddressRecord":
        """Create a record from one ``results.juso`` item."""

        return cls(
            road_address=_clean(data.get("roadAddr")),
            road_address_without_detail=_clean(data.get("roadAddrPart1")),
            road_address_reference=_clean(data.get("roadAddrPart2")),
            jibun_address=_clean(data.get("jibunAddr")),
            english_address=_clean(data.get("engAddr")),
            zip_no=_clean(data.get("zipNo")),
            administrative_code=_clean(data.get("admCd")),
            road_name_code=_clean(data.get("rnMgtSn")),
            building_management_number=_clean(data.get("bdMgtSn")),
            detail_building_names=_clean(data.get("detBdNmList")),
            building_name=_clean(data.get("bdNm")),
            building_kind_code=_clean(data.get("bdKdcd")),
            sido=_clean(data.get("siNm")),
            sigungu=_clean(data.get("sggNm")),
            eupmyeondong=_clean(data.get("emdNm")),
            li=_clean(data.get("liNm")),
            road_name=_clean(data.get("rn")),
            underground_yn=_clean(data.get("udrtYn")),
            building_main_number=_to_int(data.get("buldMnnm")),
            building_sub_number=_to_int(data.get("buldSlno")),
            mountain_yn=_clean(data.get("mtYn")),
            lot_main_number=_to_int(data.get("lnbrMnnm")),
            lot_sub_number=_to_int(data.get("lnbrSlno")),
            emd_serial_number=_clean(data.get("emdNo")),
            history_yn=_clean(data.get("hstryYn")),
            related_jibun=_clean(data.get("relJibun")),
            community_center=_clean(data.get("hemdNm")),
            source_query=source_query,
            raw=dict(data),
        )

    @property
    def canonical_address(self) -> str:
        """Return the best display address, preferring full road-name address."""

        return self.road_address or self.road_address_without_detail or self.jibun_address

    @property
    def is_historical(self) -> bool:
        """Whether the result was found through changed-address history."""

        return self.history_yn == "1"

    def to_coordinate_params(self) -> Dict[str, str]:
        """Return identifiers required by the Juso coordinate API."""

        missing = [
            name
            for name, value in {
                "admCd": self.administrative_code,
                "rnMgtSn": self.road_name_code,
                "udrtYn": self.underground_yn,
                "buldMnnm": self.building_main_number,
                "buldSlno": self.building_sub_number,
            }.items()
            if value in ("", None)
        ]
        if missing:
            raise JusoClientError(f"좌표 조회에 필요한 Juso 필드가 없습니다: {', '.join(missing)}")

        return {
            "admCd": self.administrative_code,
            "rnMgtSn": self.road_name_code,
            "udrtYn": self.underground_yn,
            "buldMnnm": str(self.building_main_number),
            "buldSlno": str(self.building_sub_number),
        }

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serializable representation."""

        return {
            "canonical_address": self.canonical_address,
            "road_address": self.road_address,
            "road_address_without_detail": self.road_address_without_detail,
            "road_address_reference": self.road_address_reference,
            "jibun_address": self.jibun_address,
            "english_address": self.english_address,
            "zip_no": self.zip_no,
            "administrative_code": self.administrative_code,
            "road_name_code": self.road_name_code,
            "building_management_number": self.building_management_number,
            "building_name": self.building_name,
            "sido": self.sido,
            "sigungu": self.sigungu,
            "eupmyeondong": self.eupmyeondong,
            "li": self.li,
            "road_name": self.road_name,
            "underground_yn": self.underground_yn,
            "building_main_number": self.building_main_number,
            "building_sub_number": self.building_sub_number,
            "mountain_yn": self.mountain_yn,
            "lot_main_number": self.lot_main_number,
            "lot_sub_number": self.lot_sub_number,
            "emd_serial_number": self.emd_serial_number,
            "history_yn": self.history_yn,
            "is_historical": self.is_historical,
            "related_jibun": self.related_jibun,
            "community_center": self.community_center,
            "source_query": self.source_query,
            "raw": dict(self.raw),
        }


@dataclass(frozen=True)
class AddressSearchResult:
    """Parsed response from the Juso search API."""

    query: str
    normalized_query: str
    input_type: str
    total_count: int
    current_page: int
    count_per_page: int
    error_code: str
    error_message: str
    records: List[KoreanAddressRecord]
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """Whether the API reported success."""

        return self.error_code == "0"

    @property
    def first(self) -> Optional[KoreanAddressRecord]:
        """Return the first candidate, if present."""

        return self.records[0] if self.records else None

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serializable representation."""

        return {
            "query": self.query,
            "normalized_query": self.normalized_query,
            "input_type": self.input_type,
            "total_count": self.total_count,
            "current_page": self.current_page,
            "count_per_page": self.count_per_page,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "records": [record.to_dict() for record in self.records],
            "raw": dict(self.raw),
        }


@dataclass(frozen=True)
class AddressCoordinate:
    """Coordinate returned by the Juso coordinate API."""

    entrance_x: str = ""
    entrance_y: str = ""
    source_crs: str = JUSO_COORD_CRS
    gps: Optional[GPSCoordinate] = None
    administrative_code: str = ""
    road_name_code: str = ""
    building_management_number: str = ""
    building_name: str = ""
    underground_yn: str = ""
    building_main_number: Optional[int] = None
    building_sub_number: Optional[int] = None
    raw: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_juso(cls, data: Mapping[str, Any]) -> "AddressCoordinate":
        """Create a coordinate record from one coordinate API item."""

        entrance_x = _clean(data.get("entX"))
        entrance_y = _clean(data.get("entY"))
        return cls(
            entrance_x=entrance_x,
            entrance_y=entrance_y,
            gps=gps_from_juso_coordinate(entrance_x, entrance_y),
            administrative_code=_clean(data.get("admCd")),
            road_name_code=_clean(data.get("rnMgtSn")),
            building_management_number=_clean(data.get("bdMgtSn")),
            building_name=_clean(data.get("bdNm")),
            underground_yn=_clean(data.get("udrtYn")),
            building_main_number=_to_int(data.get("buldMnnm")),
            building_sub_number=_to_int(data.get("buldSlno")),
            raw=dict(data),
        )

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serializable representation."""

        return {
            "entrance_x": self.entrance_x,
            "entrance_y": self.entrance_y,
            "source_crs": self.source_crs,
            "gps": self.gps.to_dict() if self.gps else None,
            "administrative_code": self.administrative_code,
            "road_name_code": self.road_name_code,
            "building_management_number": self.building_management_number,
            "building_name": self.building_name,
            "underground_yn": self.underground_yn,
            "building_main_number": self.building_main_number,
            "building_sub_number": self.building_sub_number,
            "raw": dict(self.raw),
        }


@dataclass(frozen=True)
class AddressConversionResult:
    """High-level conversion result for one input address."""

    source_address: str
    normalized_address: str
    input_type: str
    selected: Optional[KoreanAddressRecord]
    alternatives: List[KoreanAddressRecord]
    coordinate: Optional[AddressCoordinate]
    search: AddressSearchResult

    @property
    def road_address(self) -> str:
        """Selected road-name address, if available."""

        return self.selected.road_address if self.selected else ""

    @property
    def jibun_address(self) -> str:
        """Selected land-lot address, if available."""

        return self.selected.jibun_address if self.selected else ""

    @property
    def zip_no(self) -> str:
        """Selected postal code, if available."""

        return self.selected.zip_no if self.selected else ""

    @property
    def current_address(self) -> str:
        """Best current address returned by Juso."""

        return self.selected.canonical_address if self.selected else ""

    @property
    def is_history_match(self) -> bool:
        """Whether the input matched Juso changed-address history."""

        return bool(self.selected and self.selected.is_historical)

    @property
    def gps_coordinate(self) -> Optional[GPSCoordinate]:
        """Selected GPS coordinate converted to WGS84, if available."""

        return self.coordinate.gps if self.coordinate else None

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serializable representation."""

        return {
            "source_address": self.source_address,
            "normalized_address": self.normalized_address,
            "input_type": self.input_type,
            "current_address": self.current_address,
            "is_history_match": self.is_history_match,
            "selected": self.selected.to_dict() if self.selected else None,
            "alternatives": [record.to_dict() for record in self.alternatives],
            "coordinate": self.coordinate.to_dict() if self.coordinate else None,
            "gps_coordinate": self.gps_coordinate.to_dict() if self.gps_coordinate else None,
            "search": self.search.to_dict(),
        }


@dataclass
class JusoClient:
    """Small synchronous client for the official Korean Juso APIs."""

    api_key: Optional[str] = None
    search_url: str = JUSO_SEARCH_URL
    coord_url: str = JUSO_COORD_URL
    timeout: float = 10.0
    session: HTTPClient = field(default_factory=requests.Session)

    def __post_init__(self) -> None:
        if self.api_key is None:
            self.api_key = os.getenv(JUSO_API_KEY_ENV)

    def search(
        self,
        keyword: str,
        *,
        current_page: int = 1,
        count_per_page: int = 10,
        include_history: bool = True,
        include_additional_info: bool = True,
        first_sort: str = "none",
        raise_on_error: bool = True,
    ) -> AddressSearchResult:
        """Search one address and return normalized road/jibun candidates."""

        api_key = self._require_api_key()
        normalized = normalize_korean_address(keyword)
        validate_juso_keyword(normalized)
        count_per_page = max(1, min(int(count_per_page), 100))
        current_page = max(1, int(current_page))
        first_sort = _normalize_first_sort(first_sort)

        params = {
            "confmKey": api_key,
            "currentPage": current_page,
            "countPerPage": count_per_page,
            "keyword": normalized,
            "resultType": "json",
            "hstryYn": "Y" if include_history else "N",
            "addInfoYn": "Y" if include_additional_info else "N",
            "firstSort": first_sort,
        }
        payload = self._get_json(self.search_url, params=params)
        result = parse_search_response(payload, query=keyword, normalized_query=normalized)
        if raise_on_error and not result.ok:
            raise JusoAPIError(result.error_code, result.error_message)
        return result

    def convert(
        self,
        address: str,
        *,
        include_coordinates: bool = False,
        count_per_page: int = 10,
        raise_on_error: bool = True,
    ) -> AddressConversionResult:
        """Convert an input address into both road-name and land-lot forms."""

        normalized = normalize_korean_address(address)
        input_type = detect_address_type(normalized)
        search = self.search(
            normalized,
            count_per_page=count_per_page,
            include_history=True,
            include_additional_info=True,
            first_sort=_sort_for_address_type(input_type),
            raise_on_error=raise_on_error,
        )
        selected = search.first
        coordinate = self.fetch_coordinates(selected) if include_coordinates and selected else None
        alternatives = search.records[1:] if selected else search.records
        return AddressConversionResult(
            source_address=address,
            normalized_address=normalized,
            input_type=input_type,
            selected=selected,
            alternatives=alternatives,
            coordinate=coordinate,
            search=search,
        )

    def convert_historical_address(
        self,
        address: str,
        *,
        include_gps: bool = True,
        count_per_page: int = 10,
        raise_on_error: bool = True,
    ) -> AddressConversionResult:
        """Convert an old or current address to the current Juso address record.

        Juso marks changed-address matches with ``hstryYn == "1"`` while
        returning the current road-name and land-lot address fields.
        """

        return self.convert(
            address,
            include_coordinates=include_gps,
            count_per_page=count_per_page,
            raise_on_error=raise_on_error,
        )

    def fetch_coordinates(self, record: KoreanAddressRecord, *, raise_on_error: bool = True) -> Optional[AddressCoordinate]:
        """Fetch entrance coordinates for a Juso search record."""

        api_key = self._require_api_key()
        params = {
            "confmKey": api_key,
            "resultType": "json",
            **record.to_coordinate_params(),
        }
        payload = self._get_json(self.coord_url, params=params)
        results = payload.get("results", {}) if isinstance(payload, Mapping) else {}
        common = results.get("common", {}) if isinstance(results, Mapping) else {}
        error_code = _clean(common.get("errorCode"))
        error_message = _clean(common.get("errorMessage"))
        if raise_on_error and error_code and error_code != "0":
            raise JusoAPIError(error_code, error_message)

        items = _as_list(results.get("juso")) if isinstance(results, Mapping) else []
        return AddressCoordinate.from_juso(items[0]) if items else None

    def _require_api_key(self) -> str:
        if not self.api_key:
            raise JusoClientError(
                f"Juso 승인키가 필요합니다. 생성자 api_key 또는 {JUSO_API_KEY_ENV} 환경변수를 설정하세요."
            )
        return self.api_key

    def _get_json(self, url: str, *, params: Mapping[str, Any]) -> Mapping[str, Any]:
        response = self.session.get(url, params=params, timeout=self.timeout)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, Mapping):
            raise JusoClientError("Juso API 응답이 JSON 객체가 아닙니다.")
        return payload


def normalize_korean_address(value: str) -> str:
    """Normalize whitespace and common postal-code prefixes in an address."""

    text = _clean(value)
    text = _ZERO_WIDTH_RE.sub("", text)
    text = text.replace("\u3000", " ")
    text = _POSTAL_PREFIX_RE.sub("", text)
    text = re.sub(r"\s*,\s*", " ", text)
    text = _WHITESPACE_RE.sub(" ", text)
    text = re.sub(r"\s+([),])", r"\1", text)
    text = re.sub(r"([(])\s+", r"\1", text)
    return text.strip()


def detect_address_type(value: str) -> str:
    """Classify an address as road-name, land-lot, or unknown."""

    text = normalize_korean_address(value)
    if _ROAD_ADDRESS_RE.search(f" {text} "):
        return ADDRESS_TYPE_ROAD
    if _JIBUN_ADDRESS_RE.search(f" {text} "):
        return ADDRESS_TYPE_JIBUN
    return ADDRESS_TYPE_UNKNOWN


def validate_juso_keyword(keyword: str) -> None:
    """Validate the subset of Juso keyword rules that is safe client-side."""

    text = normalize_korean_address(keyword)
    if not text:
        raise ValueError("주소 검색어가 비어 있습니다.")
    if len(text) < 2:
        raise ValueError("주소 검색어는 두 글자 이상이어야 합니다.")
    if len(text) > 80:
        raise ValueError("주소 검색어는 80자를 넘을 수 없습니다.")
    if text.isdigit():
        raise ValueError("주소 검색어는 숫자만으로 구성될 수 없습니다.")
    if _DISALLOWED_KEYWORD_RE.search(text):
        raise ValueError("주소 검색어에 Juso API가 허용하지 않는 특수문자가 포함되어 있습니다.")


def parse_search_response(payload: Mapping[str, Any], *, query: str = "", normalized_query: str = "") -> AddressSearchResult:
    """Parse an official Juso search JSON response."""

    results = payload.get("results", {}) if isinstance(payload, Mapping) else {}
    common = results.get("common", {}) if isinstance(results, Mapping) else {}
    normalized = normalized_query or normalize_korean_address(query)
    records = [
        KoreanAddressRecord.from_juso(item, source_query=normalized)
        for item in _as_list(results.get("juso") if isinstance(results, Mapping) else [])
        if isinstance(item, Mapping)
    ]
    return AddressSearchResult(
        query=query,
        normalized_query=normalized,
        input_type=detect_address_type(normalized),
        total_count=_to_int(common.get("totalCount")) or 0,
        current_page=_to_int(common.get("currentPage")) or 1,
        count_per_page=_to_int(common.get("countPerPage")) or len(records),
        error_code=_clean(common.get("errorCode")),
        error_message=_clean(common.get("errorMessage")),
        records=records,
        raw=dict(payload),
    )


def convert_address(
    address: str,
    *,
    api_key: Optional[str] = None,
    client: Optional[JusoClient] = None,
    include_coordinates: bool = False,
    count_per_page: int = 10,
) -> AddressConversionResult:
    """Convert one Korean address using a provided client or API key."""

    active_client = client or JusoClient(api_key=api_key)
    return active_client.convert(
        address,
        include_coordinates=include_coordinates,
        count_per_page=count_per_page,
    )


def convert_historical_address(
    address: str,
    *,
    api_key: Optional[str] = None,
    client: Optional[JusoClient] = None,
    include_gps: bool = True,
    count_per_page: int = 10,
) -> AddressConversionResult:
    """Convert an old address to the current address and optionally tag GPS."""

    active_client = client or JusoClient(api_key=api_key)
    return active_client.convert_historical_address(
        address,
        include_gps=include_gps,
        count_per_page=count_per_page,
    )


def convert_addresses(
    addresses: Iterable[str],
    *,
    api_key: Optional[str] = None,
    client: Optional[JusoClient] = None,
    include_coordinates: bool = False,
    count_per_page: int = 10,
) -> List[AddressConversionResult]:
    """Convert multiple Korean addresses in order."""

    active_client = client or JusoClient(api_key=api_key)
    return [
        active_client.convert(
            address,
            include_coordinates=include_coordinates,
            count_per_page=count_per_page,
        )
        for address in addresses
    ]


def tag_addresses_with_current_and_gps(
    addresses: Iterable[str],
    *,
    api_key: Optional[str] = None,
    client: Optional[JusoClient] = None,
    count_per_page: int = 10,
) -> List[Dict[str, Any]]:
    """Return compact current-address and GPS tags for many addresses."""

    active_client = client or JusoClient(api_key=api_key)
    tagged: List[Dict[str, Any]] = []
    for address in addresses:
        result = active_client.convert_historical_address(
            address,
            include_gps=True,
            count_per_page=count_per_page,
        )
        tagged.append(
            {
                "source_address": result.source_address,
                "normalized_address": result.normalized_address,
                "current_address": result.current_address,
                "road_address": result.road_address,
                "jibun_address": result.jibun_address,
                "zip_no": result.zip_no,
                "is_history_match": result.is_history_match,
                "gps_coordinate": result.gps_coordinate.to_dict() if result.gps_coordinate else None,
                "coordinate": result.coordinate.to_dict() if result.coordinate else None,
            }
        )
    return tagged


def gps_from_juso_coordinate(x: Any, y: Any, *, source_crs: str = JUSO_COORD_CRS) -> Optional[GPSCoordinate]:
    """Convert Juso entrance coordinates to WGS84 GPS latitude/longitude.

    Juso ``entX``/``entY`` values are handled as Korea 2000 Unified CS
    coordinates (EPSG:5179). If a provider already returns WGS84-like values,
    they are passed through.
    """

    parsed_x = _to_float(x)
    parsed_y = _to_float(y)
    if parsed_x is None or parsed_y is None:
        return None

    if 120 <= parsed_x <= 140 and 30 <= parsed_y <= 45:
        return GPSCoordinate(
            latitude=parsed_y,
            longitude=parsed_x,
            source_crs=GPS_CRS,
            source_x=_clean(x),
            source_y=_clean(y),
        )

    if source_crs.upper() != JUSO_COORD_CRS:
        raise JusoClientError(f"지원하지 않는 좌표계입니다: {source_crs}")

    latitude, longitude = _epsg5179_to_wgs84(parsed_x, parsed_y)
    return GPSCoordinate(
        latitude=latitude,
        longitude=longitude,
        source_crs=source_crs,
        source_x=_clean(x),
        source_y=_clean(y),
    )


def _epsg5179_to_wgs84(easting: float, northing: float) -> tuple[float, float]:
    """Inverse Transverse Mercator for EPSG:5179 to WGS84-like degrees."""

    pyproj_result = _epsg5179_to_wgs84_with_pyproj(easting, northing)
    if pyproj_result:
        return pyproj_result

    semi_major_axis = 6378137.0
    inverse_flattening = 298.257222101
    flattening = 1 / inverse_flattening
    eccentricity_squared = 2 * flattening - flattening * flattening
    second_eccentricity_squared = eccentricity_squared / (1 - eccentricity_squared)
    scale_factor = 0.9996
    false_easting = 1000000.0
    false_northing = 2000000.0
    latitude_origin = radians(38.0)
    longitude_origin = radians(127.5)

    meridional_arc_origin = _meridional_arc(semi_major_axis, eccentricity_squared, latitude_origin)
    meridional_arc = meridional_arc_origin + (northing - false_northing) / scale_factor
    footprint_latitude = _footprint_latitude(semi_major_axis, eccentricity_squared, meridional_arc)

    sin_latitude = sin(footprint_latitude)
    cos_latitude = cos(footprint_latitude)
    tan_latitude = tan(footprint_latitude)

    radius_prime_vertical = semi_major_axis / sqrt(1 - eccentricity_squared * sin_latitude * sin_latitude)
    radius_meridian = (
        semi_major_axis
        * (1 - eccentricity_squared)
        / (1 - eccentricity_squared * sin_latitude * sin_latitude) ** 1.5
    )
    tangent_squared = tan_latitude * tan_latitude
    eta_squared = second_eccentricity_squared * cos_latitude * cos_latitude
    delta = (easting - false_easting) / (radius_prime_vertical * scale_factor)

    latitude = footprint_latitude - (
        radius_prime_vertical
        * tan_latitude
        / radius_meridian
        * (
            delta**2 / 2
            - (5 + 3 * tangent_squared + 10 * eta_squared - 4 * eta_squared**2 - 9 * second_eccentricity_squared)
            * delta**4
            / 24
            + (
                61
                + 90 * tangent_squared
                + 298 * eta_squared
                + 45 * tangent_squared**2
                - 252 * second_eccentricity_squared
                - 3 * eta_squared**2
            )
            * delta**6
            / 720
        )
    )
    longitude = longitude_origin + (
        delta
        - (1 + 2 * tangent_squared + eta_squared) * delta**3 / 6
        + (
            5
            - 2 * eta_squared
            + 28 * tangent_squared
            - 3 * eta_squared**2
            + 8 * second_eccentricity_squared
            + 24 * tangent_squared**2
        )
        * delta**5
        / 120
    ) / cos_latitude

    return degrees(latitude), degrees(longitude)


def _epsg5179_to_wgs84_with_pyproj(easting: float, northing: float) -> Optional[tuple[float, float]]:
    """Use pyproj/PROJ when available, without making it a hard dependency."""

    try:
        from pyproj import Transformer
    except ImportError:
        return None

    transformer = Transformer.from_crs(JUSO_COORD_CRS, GPS_CRS, always_xy=True)
    longitude, latitude = transformer.transform(easting, northing)
    return float(latitude), float(longitude)


def _meridional_arc(semi_major_axis: float, eccentricity_squared: float, latitude: float) -> float:
    e4 = eccentricity_squared**2
    e6 = eccentricity_squared**3
    return semi_major_axis * (
        (1 - eccentricity_squared / 4 - 3 * e4 / 64 - 5 * e6 / 256) * latitude
        - (3 * eccentricity_squared / 8 + 3 * e4 / 32 + 45 * e6 / 1024) * sin(2 * latitude)
        + (15 * e4 / 256 + 45 * e6 / 1024) * sin(4 * latitude)
        - (35 * e6 / 3072) * sin(6 * latitude)
    )


def _footprint_latitude(semi_major_axis: float, eccentricity_squared: float, meridional_arc: float) -> float:
    e1 = (1 - sqrt(1 - eccentricity_squared)) / (1 + sqrt(1 - eccentricity_squared))
    mu = meridional_arc / (
        semi_major_axis
        * (1 - eccentricity_squared / 4 - 3 * eccentricity_squared**2 / 64 - 5 * eccentricity_squared**3 / 256)
    )
    return (
        mu
        + (3 * e1 / 2 - 27 * e1**3 / 32) * sin(2 * mu)
        + (21 * e1**2 / 16 - 55 * e1**4 / 32) * sin(4 * mu)
        + (151 * e1**3 / 96) * sin(6 * mu)
        + (1097 * e1**4 / 512) * sin(8 * mu)
    )


def _sort_for_address_type(address_type: str) -> str:
    if address_type == ADDRESS_TYPE_ROAD:
        return "road"
    if address_type == ADDRESS_TYPE_JIBUN:
        return "location"
    return "none"


def _normalize_first_sort(value: str) -> str:
    normalized = (value or "none").strip().lower()
    if normalized not in {"none", "road", "location"}:
        raise ValueError("first_sort는 none, road, location 중 하나여야 합니다.")
    return normalized


def _as_list(value: Any) -> List[Any]:
    if value in (None, ""):
        return []
    if isinstance(value, list):
        return value
    return [value]


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _to_int(value: Any) -> Optional[int]:
    text = _clean(value)
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _to_float(value: Any) -> Optional[float]:
    text = _clean(value)
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None
