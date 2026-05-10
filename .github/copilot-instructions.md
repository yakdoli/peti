# 관보 크롤러 - Copilot 지침

## 프로젝트 개요
관보(정부공시) API를 크롤링하여 1994년 1월 1일부터 현재까지의 PDF 문서와 메타데이터를 수집하는 프로젝트입니다.

## 프로젝트 특징
- **날짜 범위**: 1994-01-01 ~ 현재
- **데이터 수집**: PDF 다운로드 + 메타데이터 JSON
- **OCR 준비**: PDF를 이미지로 변환하여 향후 OCR 처리 가능
- **비동기 처리**: asyncio를 활용한 동시 다운로드
- **로깅**: 상세한 처리 로그 기록

## 프로젝트 구조
```
ko-perty/
├── config/
│   └── config.yaml          # 크롤러 설정
├── src/
│   ├── config.py            # 설정 관리
│   ├── logger.py            # 로깅 설정
│   ├── crawler.py           # 메인 크롤러
│   ├── crawler_search_thema.py # SearchThema 크롤러
│   ├── metadata_manager.py  # 메타데이터 관리
│   └── pdf_handler.py       # PDF 처리
├── crawl.py                 # 관보 크롤러 진입점
├── crawl_search_thema.py    # SearchThema 진입점
├── artifacts/
│   ├── pdfs/                # 다운로드 PDF
│   ├── metadata/            # 메타데이터 JSON/CSV
│   ├── ocr_ready/           # OCR 준비 이미지
│   └── searchThema/         # SearchThema 전용 아티팩트
├── datasets/                # Hugging Face export 대상
├── logs/                    # 로그 파일
├── requirements.txt         # 의존성
└── README.md               # 프로젝트 문서
```

## 정리 원칙

- 소스 프로젝트는 `src/`와 실행 진입점만 포함합니다.
- 생성 산출물은 소스 구분별, 상세 구분별로 `artifacts/` 아래에 분리해서 저장합니다.
- OCR 결과와 허깅페이스 데이터셋은 별도 아티팩트로 취급하고, 저장소에는 대용량 원본을 두지 않습니다.

## 사용 방법

### 1. 환경 설정
```bash
# Python 3.8+ 필요
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. 설정 커스터마이징
`config/config.yaml`에서 필요한 설정을 수정:
- `start_date`: 크롤링 시작 날짜
- `end_date`: 크롤링 종료 날짜
- `max_concurrent_downloads`: 동시 다운로드 수

### 3. 크롤링 실행
```bash
cd src
python crawler.py
```

### 4. OCR 준비 (선택)
```python
from pdf_handler import PDFHandler

pdf_handler = PDFHandler()
pdf_handler.prepare_batch_for_ocr()
```

## 주요 모듈

### crawler.py
- `GwanboCrawler`: 비동기 크롤러 클래스
- 날짜 범위별 배치 처리
- 동시 PDF 다운로드
- 에러 재시도 로직

### metadata_manager.py
- 메타데이터 JSON 관리
- CSV 내보내기
- 카테고리별 분류
- 통계 제공

### pdf_handler.py
- PDF OCR 준비 (이미지 변환)
- 파일 정보 조회
- OCR 상태 추적

## 메타데이터 구조
```json
{
  "id": "항목ID",
  "title": "제목",
  "date": "2024-01-01",
  "category": "카테고리",
  "url": "원본URL",
  "pdf_path": "data/pdfs/filename.pdf",
  "status": "completed|pending|failed",
  "download_date": "2024-05-07T10:30:00",
  "file_size": 1024000
}
```

## 주의사항
- API 서버 부하 방지를 위해 배치 처리 사이에 지연 포함
- 타임아웃: 30초
- 재시도: 최대 3회
- 동시 다운로드: 기본 5개

## 향후 계획
1. OCR 처리 자동화
2. 텍스트 추출 및 검색 인덱싱
3. 웹 대시보드 추가
4. 분산 처리 지원
