# peti — 관보 크롤러

**Workspace root**: `~/workspace/` (한 단계 위)
**역할**: 전자관보에서 공직자 재산 공개 메타데이터·PDF 수집

## 데이터 소스

| 소스 | 설명 | 크롤러 |
|------|------|--------|
| `pety` | petyListAjax (Playwright) | `src/crawler.py` |
| `searchThema` | SearchRestApi HTTP POST | `src/crawler_search_thema.py` |

## 프로젝트 구조

```
src/
├── crawler.py               # GwanboCrawler (pety 소스)
├── crawler_search_thema.py  # SearchThemaCrawler (searchThema 소스)
├── base_crawler.py          # 공통 HTTP/PDF 다운로드 로직
├── metadata_manager.py      # 메타데이터 읽기/쓰기
├── crawl_state.py           # 재시작 상태 관리
├── pety_parser.py           # pety HTML 파서
├── search_thema_parser.py   # searchThema JSON 파서
├── pdf_validator.py         # PDF 유효성 검사
└── config.py / logger.py

scripts/
├── final_upload_chunks.py          # HuggingFace 업로드 (체크포인트 재개)
├── convert_search_thema_to_duckdb.py  # metadata → DuckDB
└── batched_upload_pdfs.py          # PDF 실패 리포트
```

## 아티팩트 경로 (git 제외)

```
data/searchThema/metadata/metadata_{category}.json  # 18개 카테고리
data/searchThema/pdfs/                              # PDF 파일
data/searchThema/hf_batch_state.json               # HF 업로드 체크포인트
data/searchThema/pdf_failures.jsonl                # 실패 로그
```

## 자주 쓰는 명령

```bash
python crawl_search_thema.py --resume          # 크롤링 재개
python scripts/final_upload_chunks.py          # HF 업로드 재개
python scripts/batched_upload_pdfs.py          # PDF 실패 리포트
pytest tests/                                   # 테스트
```

## 연관 리포지터리

- **peti-ocr** (`../peti-ocr/`): PDF → PNG OCR 파이프라인
