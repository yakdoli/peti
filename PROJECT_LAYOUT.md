# 프로젝트 정리 기준

이 저장소는 코드와 생성 산출물을 분리해서 관리합니다. 커밋 대상은 가능한 한 소스와 설정에 한정하고, 대용량 산출물은 아티팩트로 취급합니다.

## 1. 크롤러 소스 프로젝트

소스와 실행 진입점:

- `src/`
- `crawl.py`
- `crawl_batches.py`
- `crawl_search_thema.py`
- `validate_pdfs.py`
- `scripts/`

이 구간은 크롤링 로직, 파서, 검증, 변환 도구만 포함합니다.

## 2. 아티팩트 프로젝트

관보 수집 결과는 소스 구분별, 상세 구분별로 나눠서 다룹니다.

- 소스: 관보
- 소스: 공공data API
- 상세 구분: `pety`, `searchThema`, 기타 파생 묶음

현재 저장 경로는 `config/config.yaml` 기준으로 다음과 같이 운영합니다.

- 메타데이터: `artifacts/metadata/`
- PDF: `artifacts/pdfs/`
- OCR 준비본: `artifacts/ocr_ready/`
- 재시작 상태: `artifacts/state/`
- SearchThema 전용 아티팩트: `artifacts/searchThema/`

## 3. 아티팩트 OCR 프로젝트

OCR 관련 처리는 PDF를 이미지로 바꾸는 후처리 단계로 취급합니다.

- 입력: `artifacts/pdfs/`
- 출력: `artifacts/ocr_ready/`
- 관련 코드: `src/pdf_handler.py`

OCR 결과물은 별도 분석이나 재처리 대상이므로 소스와 섞지 않습니다.

## 4. 허깅페이스 데이터셋

데이터셋은 단일 허깅페이스 데이터셋으로 묶어 관리합니다.

- 하나의 데이터셋 스키마를 기준으로 export
- source/detail 분류는 메타데이터 필드로 보존
- 대용량 원본 파일은 저장소에 직접 포함하지 않음

## 5. 저장소 관리 원칙

1. 코드와 설정만 커밋한다.
2. 대용량 산출물은 `artifacts/`, `datasets/` 아래에서 분리한다.
3. 허깅페이스 업로드용 결과물은 단일 데이터셋 단위로 묶는다.
4. 저장소에 남길 필요가 없는 결과물은 `.gitignore`로 차단한다.
