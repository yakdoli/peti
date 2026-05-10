# 관보 크롤러 (Gwanbo Crawler)

대한민국 전자관보 공개 화면에서 `petyList` 공직자 재산 공개 메타데이터와 PDF를 수집하는 Python 프로젝트입니다. Playwright 브라우저 컨텍스트로 세션을 만들고, 같은 세션에서 목록 AJAX와 PDF 다운로드 POST를 수행합니다.

## 저장소 정리 원칙

이 저장소는 크롤러 소스와 생성 산출물을 분리해서 관리합니다. 자세한 구조는 [PROJECT_LAYOUT.md](PROJECT_LAYOUT.md)를 참고하세요.

- 소스 프로젝트: `src/`, `crawl*.py`, `scripts/`
- 아티팩트 프로젝트: `artifacts/` 아래의 메타데이터, PDF, OCR 준비본
- 허깅페이스 데이터셋: 단일 export 단위로 취급

## 주요 기능

- 1994년 1월 1일부터 현재까지의 장기 날짜 범위 수집
- `petyListAjax` HTML 응답 파싱
- 항목별 PDF 다운로드와 SHA-256 해시 기록
- 항목별 JSON 원본 저장
- `metadata.json`, `metadata.csv`, `metadata_{category}.json` 인덱스 생성
- `artifacts/state/crawl_state.json` 기반 재시작
- OCR 준비용 metadata 필드와 PDF 이미지 변환 모듈

## 설치

```bash
cd /Users/yakdoli/workspace/ko-perty
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
```

OCR 준비 기능을 사용할 경우 macOS에서는 Poppler가 필요합니다.

```bash
brew install poppler
```

## 설정

`config/config.yaml`에서 날짜 범위, 저장 경로, 브라우저 설정을 조정합니다.

```yaml
crawler:
  start_date: "1994-01-01"
  end_date: "today"
  timeout: 30
  max_retries: 3
  retry_delay: 2
  window_days: 31
  row_per_page: 10
  headless: true
  browser_executable_path: ""
  themes:
    pety:
      thema_se: "02"
      list_url: "https://open.gwanbo.go.kr/OpenApi/web/petyList"
      ajax_url: "https://open.gwanbo.go.kr/OpenApi/web/petyListAjax"
      viewer_base_url: "https://gwanbo.go.kr/"

state:
  file: "artifacts/state/crawl_state.json"

download:
  pdf_directory: "artifacts/pdfs"
  metadata_directory: "artifacts/metadata"
  ocr_ready_directory: "artifacts/ocr_ready"
```

## 실행

전체 설정 범위 수집:

```bash
source venv/bin/activate
python crawl.py
```

1건 스모크 테스트:

```bash
source venv/bin/activate
python crawl.py --start-date 2026-04-24 --end-date 2026-04-24 --limit 1
```

메타데이터만 수집:

```bash
source venv/bin/activate
python crawl.py --metadata-only --start-date 2026-04-24 --end-date 2026-04-24
```

항목별 JSON에서 인덱스 재생성:

```bash
source venv/bin/activate
python crawl.py --rebuild-index
```

PDF 검증:

```bash
source venv/bin/activate
python validate_pdfs.py
```

## 데이터 구조

```text
src/
├── crawler.py
├── crawler_search_thema.py
├── metadata_manager.py
└── pdf_handler.py

artifacts/
├── metadata/
│   ├── items/{YYYY}/{YYYYMMDD}/{item_id}.json
│   ├── metadata.json
│   ├── metadata.csv
│   └── metadata_*.json
├── pdfs/{YYYY}/{YYYYMMDD}/{item_id}.pdf
├── state/crawl_state.json
├── ocr_ready/
└── searchThema/
  ├── metadata/
  ├── pdfs/
  └── state/
```

항목 JSON 예시:

```json
{
  "id": "I0000000000000001776650098435000",
  "theme": "pety",
  "title": "정부공직자윤리위원회공고제2026-5호...",
  "date": "2026-04-24",
  "book_name": "정호",
  "category": "공고",
  "agency": "인사혁신처",
  "law": "공직자윤리법 제10조",
  "content_id": "I0000000000000001776842961001000",
  "toc_id": "I0000000000000001776650098435000",
  "viewer_path": "/ezpdf/customLayout.jsp?...",
  "status": "completed",
  "pdf": {
    "status": "completed",
    "path": "data/pdfs/2026/20260424/I0000000000000001776650098435000.pdf",
    "size_bytes": 791843,
    "sha256": "...",
    "downloaded_at": "2026-05-07T15:03:45"
  },
  "ocr": {
    "status": "pending",
    "ready_dir": "data/ocr_ready/I0000000000000001776650098435000",
    "extracted_metadata": {}
  }
}
```

## OCR 준비

```python
from src.pdf_handler import PDFHandler

handler = PDFHandler()
result = handler.prepare_batch_for_ocr()
print(result)
```

## 주의사항

- 전체 기간 수집은 오래 걸리고 많은 저장 공간이 필요합니다.
- 사이트 부하를 줄이기 위해 검색 윈도우와 동시 다운로드 수를 보수적으로 설정하세요.
- macOS에서 Playwright 번들 Chromium이 실행되지 않으면 시스템 Chrome을 자동으로 시도합니다. 직접 지정하려면 `browser_executable_path`를 설정하세요.

## 참고 자료

- [전자관보 공개 포털](https://open.gwanbo.go.kr)
- [Playwright Python 문서](https://playwright.dev/python/)
- [aiohttp 문서](https://docs.aiohttp.org)

**마지막 업데이트**: 2026년 5월 7일
