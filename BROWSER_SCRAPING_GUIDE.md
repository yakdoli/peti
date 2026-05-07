# Playwright 브라우저 엔진 크롤링 가이드

## 개요

`petyList` 수집기는 Playwright 브라우저 컨텍스트로 전자관보 세션을 열고, 같은 세션에서 목록 AJAX 요청과 PDF 다운로드 POST를 수행합니다. 목록 HTML은 BeautifulSoup 기반 파서로 구조화합니다.

| 방식 | 기술 | 적용 시기 |
|------|------|----------|
| 브라우저 세션 수집 | Playwright + aiohttp | 실제 수집 기본 경로 |
| HTML 파싱 | BeautifulSoup | `petyListAjax` 응답 구조화 |
| 호환 스크립트 | `src/crawler_browser.py` | 기존 브라우저 엔트리포인트 유지 |

## 환경 설정

```bash
cd /Users/yakdoli/workspace/ko-perty
source venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
```

## 실행

기본 실행:

```bash
source venv/bin/activate
python crawl.py --theme pety
```

1건 스모크:

```bash
source venv/bin/activate
python crawl.py --start-date 2026-04-24 --end-date 2026-04-24 --limit 1
```

메타데이터만 확인:

```bash
source venv/bin/activate
python crawl.py --start-date 2026-04-24 --end-date 2026-04-24 --metadata-only --no-resume
```

인덱스 재생성:

```bash
source venv/bin/activate
python crawl.py --rebuild-index
```

## 처리 흐름

1. `https://open.gwanbo.go.kr/OpenApi/web/petyList`를 브라우저 컨텍스트에서 열어 세션을 준비합니다.
2. `/OpenApi/web/petyListAjax`에 `reqFrom`, `reqTo`, `currentPage`, `themaSe=02`를 POST합니다.
3. 응답 HTML의 `countArea`, pagination, `fnDetail(...)` 인자를 파싱합니다.
4. `viewer_path`로 `https://gwanbo.go.kr/ezpdf/customLayout.jsp?...`를 열어 다운로드 엔드포인트를 얻습니다.
5. `/user/common/ofcttCntntDownload.do`에 `cntnt_seq_no=toc_id`를 POST해 항목별 PDF를 저장합니다.
6. 항목별 JSON과 aggregate 인덱스를 저장합니다.

## 저장 위치

```text
data/metadata/items/{YYYY}/{YYYYMMDD}/{item_id}.json
data/metadata/metadata.json
data/metadata/metadata.csv
data/metadata/metadata_{category}.json
data/pdfs/{YYYY}/{YYYYMMDD}/{item_id}.pdf
data/state/crawl_state.json
```

## 문제 해결

네트워크 연결 오류:

```bash
curl -I https://open.gwanbo.go.kr/OpenApi/web/petyList
```

Playwright 브라우저 설치 확인:

```bash
python -m playwright --version
python -m playwright install chromium
```

macOS에서 번들 Chromium 실행이 실패하면 크롤러가 `/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`을 자동으로 시도합니다. 특정 브라우저를 고정하려면 `config/config.yaml`의 `browser_executable_path`를 설정합니다.

## 검증

```bash
source venv/bin/activate
python validate_pdfs.py
python -m py_compile src/*.py crawl.py crawl_web.py crawl_beautiful.py validate_pdfs.py
```

**작성일**: 2026년 5월 7일
