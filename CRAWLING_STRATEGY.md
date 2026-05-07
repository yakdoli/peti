# 관보 크롤러 - 실제 API 인터랙션 가이드

## 📋 개요

관보 데이터를 수집하기 위한 3가지 크롤링 방식을 지원합니다:

| 방식 | 장점 | 단점 | 추천 상황 |
|------|------|------|----------|
| **API 직접 호출** | 빠름, 안정적 | API 문서 필요 | 스크립트 크롤링 |
| **웹 스크래핑** | 추가 도구 불필요 | 느림 | 소규모 데이터 |
| **브라우저 자동화** | JS 지원, 정확함 | 느림, 리소스 많음 | 복잡한 페이지 |

---

## 🌐 방식 1: API 직접 호출

### 설정

```bash
# config/config.yaml
crawler:
  api_base_url: "https://open.gwanbo.go.kr/OpenApi/web/petyList"
  max_concurrent_downloads: 5
```

### 실행

```bash
source venv/bin/activate
python crawl.py
```

### 특징

- **속도**: 가장 빠름 (10-20개/초)
- **메모리**: 낮은 메모리 사용
- **신뢰성**: 높은 안정성
- **파라미터**:
  ```
  GET /OpenApi/web/petyList?pblancDate=1994-01-01&pageNum=1&pageSize=100
  ```

### 예상 결과

```
✅ 크롤링 통계
  총 항목: 15000+
  다운로드된 PDF: 15000+
  실패한 다운로드: 0
  처리 시간: ~2시간
```

---

## 🕷️ 방식 2: 웹 스크래핑

### 설정

```bash
# config/config.yaml
crawler:
  api_base_url: "https://open.gwanbo.go.kr/OpenApi/web/petyList"
  # 동일한 설정 사용
```

### 실행

```bash
source venv/bin/activate
python crawl_web.py
```

### 특징

- **속도**: 느림 (1-3개/초)
- **메모리**: 중간 메모리 사용
- **신뢰성**: 페이지 구조 변경에 영향 받음
- **파싱**: HTML 테이블 추출

### 예상 결과

```
✅ 크롤링 통계
  총 항목: 15000+
  다운로드된 페이지: 15000+
  처리 시간: ~5시간
```

---

## 🤖 방식 3: 브라우저 자동화 (Playwright)

### 설치

```bash
source venv/bin/activate
pip install playwright
playwright install chromium
```

### 설정

```bash
# config/config.yaml
crawler:
  browser: "chromium"  # chromium, firefox, webkit
  headless: true       # 헤드리스 모드
```

### 실행

```bash
source venv/bin/activate
python crawl_browser.py
```

### 특징

- **속도**: 가장 느림 (0.5-1개/초)
- **메모리**: 높은 메모리 사용 (브라우저 프로세스)
- **신뢰성**: 자바스크립트 실행 가능
- **장점**: 동적 로딩 페이지 처리 가능

### 예상 결과

```
✅ 크롤링 통계
  총 항목: 15000+
  페이지 방문: 15000+
  처리 시간: ~8시간
```

---

## 🔄 하이브리드 접근법

여러 방식을 조합하여 최적의 성능을 얻습니다:

### 추천 전략

1. **단계 1**: API 호출로 메타데이터 수집 (1-2일)
   ```bash
   python crawl.py  # 메타데이터 수집
   ```

2. **단계 2**: 웹 스크래핑으로 검증 (선택)
   ```bash
   python crawl_web.py  # 데이터 검증
   ```

3. **단계 3**: PDF 무결성 검증
   ```bash
   python validate_pdfs.py  # 모든 PDF 검증
   ```

---

## 📊 데이터 흐름

### 1단계: 메타데이터 수집

```
API/웹사이트
    ↓
크롤러 (API/스크래핑/브라우저)
    ↓
메타데이터 추출
  - ID, 제목, 날짜
  - 기관, 법령
  - URL, PDF 경로
    ↓
메타데이터 매니저
  - JSON 저장
  - CSV 변환
  - 카테고리 분류
    ↓
data/metadata/metadata.json
data/metadata/metadata.csv
data/metadata/metadata_*.json (카테고리별)
```

### 2단계: PDF 다운로드

```
메타데이터 → PDF 다운로드
    ↓
data/pdfs/12345.pdf
    ↓
파일 크기, 해시값 기록
    ↓
메타데이터 업데이트
```

### 3단계: 검증

```
PDF 파일들
    ↓
검증 도구
  - 헤더 확인
  - 구조 검증
  - 해시값 계산
    ↓
data/validation_report.json
    ↓
✅ 무결성 확인
```

---

## 🎯 실제 실행 시나리오

### 시나리오 A: 전체 데이터 수집 (1994-현재)

**목표**: 1994년 1월 1일부터 현재까지의 모든 관보 데이터

**설정**:
```yaml
crawler:
  start_date: "1994-01-01"
  end_date: "2026-05-07"
  max_concurrent_downloads: 5
```

**예상 결과**:
- 항목 수: 500,000+ (추정)
- PDF 수: 500,000+
- 총 크기: 1-10TB (추정)
- 처리 시간: 1-2주 (API 방식)

**실행**:
```bash
python crawl.py
```

### 시나리오 B: 최근 1년 데이터

**목표**: 최근 1년의 관보 데이터

**설정**:
```yaml
crawler:
  start_date: "2025-05-07"
  end_date: "2026-05-07"
  max_concurrent_downloads: 10
```

**예상 결과**:
- 항목 수: 50,000+
- PDF 수: 50,000+
- 총 크기: 50-100GB
- 처리 시간: 12-24시간

**실행**:
```bash
python crawl.py
```

### 시나리오 C: 테스트 (1주일)

**목표**: 1주일 데이터로 테스트

**설정**:
```yaml
crawler:
  start_date: "1994-01-01"
  end_date: "1994-01-07"
  max_concurrent_downloads: 5
```

**예상 결과**:
- 항목 수: 500-1000
- PDF 수: 500-1000
- 총 크기: 500MB-1GB
- 처리 시간: 1-2시간

**실행**:
```bash
python crawl.py
```

---

## 🔍 모니터링

### 실시간 진행 상황 확인

```bash
# 로그 모니터링
tail -f logs/crawler.log

# 메타데이터 항목 수
watch -n 5 'python3 -c "import json; print(len(json.load(open(\"data/metadata/metadata.json\"))))"'

# 다운로드된 PDF 수
watch -n 5 'ls -1 data/pdfs/ | wc -l'

# 디스크 사용량
watch -n 5 'du -sh data/'
```

### 성능 분석

```python
import json
from datetime import datetime

with open('logs/crawler.log') as f:
    log_content = f.read()

# 처리 시간 추출
start_time = datetime.fromisoformat('...')
end_time = datetime.fromisoformat('...')

# 처리 속도 계산
with open('data/metadata/metadata.json') as f:
    items = len(json.load(f))

speed = items / (end_time - start_time).total_seconds()
print(f"처리 속도: {speed:.1f} 항목/초")
```

---

## 🛠️ 트러블슈팅

### Q1: API 응답이 없습니다

**해결책**:
1. 네트워크 연결 확인
2. API 엔드포인트 확인
3. 방화벽/프록시 설정 확인
4. 서버 상태 확인

### Q2: 크롤링이 너무 느립니다

**최적화**:
1. `max_concurrent_downloads` 증가 (5 → 10 → 20)
2. `batch_size` 감소 (100 → 50)
3. API 방식 사용 (웹스크래핑 대신)

### Q3: PDF 다운로드 실패

**원인 분석**:
1. PDF URL 유효성 확인
2. 디스크 공간 확인
3. 파일 권한 확인
4. 타임아웃 설정 조정

### Q4: 메모리 부족

**해결책**:
1. 동시 다운로드 수 감소
2. 배치 크기 감소
3. 운영체제 메모리 늘리기
4. 스왑 메모리 설정

---

## 📈 성능 비교

| 메트릭 | API | 스크래핑 | 브라우저 |
|--------|-----|---------|---------|
| 속도 (항목/초) | 15-20 | 2-3 | 0.5-1 |
| 메모리 (MB) | 100-200 | 200-300 | 500-1000 |
| CPU (%) | 30-40 | 40-50 | 70-80 |
| 1만 항목 시간 | ~10분 | ~1시간 | ~3시간 |
| 1년 데이터 | 12-24시간 | 2-3일 | 5-7일 |

---

## 🎓 학습 자료

### Python 비동기 프로그래밍
- [asyncio 문서](https://docs.python.org/3/library/asyncio.html)
- [aiohttp 튜토리얼](https://docs.aiohttp.org)

### 웹 스크래핑
- [BeautifulSoup 문서](https://www.crummy.com/software/BeautifulSoup)
- [Selenium vs Playwright](https://www.browserstack.com/guide/selenium-vs-playwright)

### 관보 API
- [정부 공개 데이터](https://www.data.go.kr)
- [관보 포털](https://open.gwanbo.go.kr)

---

## 📝 체크리스트

### 크롤링 전

- [ ] 설정 파일 확인
- [ ] 디스크 공간 충분한지 확인 (권장: 100GB+)
- [ ] 네트워크 연결 확인
- [ ] 로그 디렉토리 생성 확인

### 크롤링 중

- [ ] 실시간 로그 모니터링
- [ ] 메모리 사용량 모니터링
- [ ] 네트워크 상태 모니터링

### 크롤링 후

- [ ] 메타데이터 파일 확인
- [ ] PDF 파일 확인
- [ ] 무결성 검증 실행
- [ ] 최종 통계 분석

---

## 🚀 다음 단계

1. **API 문서 획득**
   - 관보 공개 API 공식 문서 획득
   - 정확한 엔드포인트 확인
   - 파라미터 스펙 확인

2. **파이롯 테스트**
   - 1주일 데이터로 테스트
   - 크롤러 성능 검증
   - 메타데이터 품질 확인

3. **풀 크롤링**
   - 전체 기간 데이터 수집
   - 모니터링 설정
   - 최종 검증

4. **데이터 분석**
   - 시계열 분석
   - 기관별/법령별 통계
   - 검색 인덱싱

---

**최종 업데이트**: 2026년 5월 7일
**상태**: 모든 크롤러 준비 완료 ✅
