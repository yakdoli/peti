# 실제 관보 API 구성 가이드

## 🌐 관보 공개 API 정보

### API 엔드포인트

관보 공개 API는 다음과 같은 구조를 가집니다:

**Base URL**: `https://open.gwanbo.go.kr`

### 가능한 API 경로

1. **공직자재산공개 API**
   - 엔드포인트: `/OpenApi/web/petyList`
   - 데이터: 공직자 재산 공개 정보

2. **입법예고 API**
   - 엔드포인트: `/OpenApi/web/signifyInfoList` (추정)
   - 데이터: 입법 예고 정보

3. **관보 API**
   - 엔드포인트: `/OpenApi/web/...` (정확한 경로 필요)
   - 데이터: 정부 공시 정보

### 요청 파라미터

```
GET /OpenApi/web/petyList

파라미터:
- pblancDate: 발행일자 (YYYY-MM-DD)
- pblancStartDate: 발행 시작일자
- pblancEndDate: 발행 종료일자
- pageNum: 페이지 번호
- pageSize: 페이지당 항목 수
- searchTitle: 제목 검색
- searchAgency: 발행기관 검색
- searchLaw: 근거법령 검색
```

### 응답 형식

#### JSON 응답 예시

```json
{
  "code": "000",
  "message": "SUCCESS",
  "data": [
    {
      "id": "202401001",
      "title": "정부공시 제목",
      "pblancDate": "2024-01-01",
      "pblancAgency": "부처명",
      "pblancLaw": "근거법령",
      "pdfUrl": "https://...",
      "createDateTime": "2024-01-01T00:00:00",
      "modifyDateTime": "2024-01-01T00:00:00"
    }
  ],
  "pagination": {
    "pageNum": 1,
    "pageSize": 10,
    "totalCount": 1234,
    "totalPage": 124
  }
}
```

#### HTML 테이블 응답 (웹페이지)

```html
<table class="table">
  <thead>
    <tr>
      <th>관보제목</th>
      <th>발행기관</th>
      <th>근거법령</th>
      <th>발행일자</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td>정부공시 제목</td>
      <td>부처명</td>
      <td>근거법령</td>
      <td>2024-01-01</td>
    </tr>
  </tbody>
</table>
```

## 🔧 크롤러 구성

### 지원되는 크롤링 방식

1. **API 직접 호출 (권장)**
   - 파일: `src/crawler.py`
   - 방식: REST API JSON 응답 파싱
   - 장점: 빠르고 안정적

2. **웹 스크래핑**
   - 파일: `src/crawler_web.py`
   - 방식: HTML 테이블 파싱
   - 장점: API 문서 불필요

3. **브라우저 자동화 (Playwright)**
   - 파일: `src/crawler_browser.py` (미구현)
   - 방식: Playwright로 브라우저 자동 제어
   - 장점: 자바스크립트 실행 필요한 페이지 처리

### 데이터 흐름

```
관보 API/웹사이트
    ↓
[크롤러]
  - API 호출 또는 HTML 파싱
  - 메타데이터 추출
  - PDF URL 수집
    ↓
[메타데이터 관리자]
  - JSON 저장
  - CSV 변환
  - 카테고리 분류
  - 통계 생성
    ↓
[PDF 처리]
  - PDF 다운로드
  - OCR 준비 (이미지 변환)
  - 무결성 검증
    ↓
[최종 저장소]
  - data/pdfs/
  - data/metadata/
  - data/ocr_ready/
```

## 📋 메타데이터 구조

### 저장되는 정보

```json
{
  "202401001": {
    "id": "202401001",
    "title": "정부공시 제목",
    "date": "2024-01-01",
    "agency": "발행기관",
    "law": "근거법령",
    "url": "https://open.gwanbo.go.kr/...",
    "pdf_path": "data/pdfs/202401001.pdf",
    "status": "completed|pending|failed",
    "source": "api|web_scraping|browser",
    "download_date": "2024-05-07T10:30:00",
    "file_size": 1024000,
    "md5_hash": "...",
    "validation_status": "pass|fail"
  }
}
```

## 🚀 실제 사용 방법

### 1단계: 날짜 범위 설정

[config/config.yaml](config/config.yaml) 수정:

```yaml
crawler:
  start_date: "1994-01-01"
  end_date: "2026-05-07"
  api_base_url: "https://open.gwanbo.go.kr/OpenApi/web/petyList"
```

### 2단계: 크롤러 선택 및 실행

#### API 방식 (권장)
```bash
python crawl.py
```

#### 웹 스크래핑 방식
```bash
python crawl_web.py
```

### 3단계: 결과 확인

```bash
# 메타데이터 확인
ls -lh data/metadata/
cat data/metadata/metadata.json | python3 -m json.tool | head -50

# CSV 확인
cat data/metadata/metadata.csv

# PDF 파일 확인
ls -lh data/pdfs/ | head -20
du -sh data/pdfs/
```

### 4단계: 무결성 검증

```bash
python validate_pdfs.py
cat data/validation_report.json | python3 -m json.tool
```

## 🔍 API 응답 형식 추론

현재 웹페이지에서 확인된 데이터 구조:

```
행 1: 관보제목 | 발행기관 | 근거법령 | 발행일자
     ┌─────────────────────────────────────────┐
     │ "정부공직자윤리위원회공고제2026-5호..." │
     │ "인사혁신처"                            │
     │ "공직자윤리법 제10조"                   │
     │ "2026.04.24"                           │
     └─────────────────────────────────────────┘
```

## ⚙️ 커스터마이징

### 새로운 크롤링 방식 추가

1. `src/crawler_*.py` 파일 생성
2. 기본 클래스 상속
3. `crawl()` 메서드 구현
4. 메타데이터 저장

### 필터링 추가

```python
# config/config.yaml에 추가
crawler:
  filters:
    agencies: ["인사혁신처", "행정안전부"]  # 특정 기관만
    laws: ["공직자윤리법"]                  # 특정 법령만
    title_keywords: ["공고", "공시"]       # 제목 키워드
```

## 🔗 참고 자료

- [관보 공개 포털](https://open.gwanbo.go.kr)
- [정부 공개 데이터](https://www.data.go.kr)
- [aiohttp 문서](https://docs.aiohttp.org)
- [BeautifulSoup 문서](https://www.crummy.com/software/BeautifulSoup)

## 🆘 문제 해결

### Q: API 응답이 없어요
**A**: 다음을 확인하세요
- 인터넷 연결
- API 엔드포인트 정확성
- 파라미터 형식
- 방화벽/프록시 설정

### Q: 속도가 느려요
**A**: 다음을 조정하세요
- `max_concurrent_downloads` 증가
- `batch_size` 감소
- `retry_delay` 감소

### Q: PDF 다운로드가 실패해요
**A**: 다음을 확인하세요
- PDF URL 유효성
- 디스크 공간
- 파일 권한
- 타임아웃 설정

## 📝 로그 분석

```bash
# 실시간 로그 모니터링
tail -f logs/crawler.log

# 오류만 필터링
grep ERROR logs/crawler.log

# 특정 날짜 조회
grep "2026-05-07" logs/crawler.log
```

## 🎯 성과 지표

크롤링 완료 후 확인할 지표:

- ✓ 수집된 항목 수
- ✓ 다운로드 성공률
- ✓ PDF 무결성 (100% pass)
- ✓ 메타데이터 완성도
- ✓ 처리 시간
- ✓ 디스크 사용량

---

**이 문서는 실제 API 연동을 위한 가이드입니다.**
네트워크 환경이나 API 문서 업데이트에 따라 조정이 필요할 수 있습니다.
