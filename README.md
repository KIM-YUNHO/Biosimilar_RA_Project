# Biosimilar RA Project — 문서 적재 (`ra-ingest`)

성분명 또는 제품명으로 대상을 지정하고, 기관(EMA, FDA, Health Canada)을 골라 공개 규제 문서를 적재합니다.

**적재 대상**
- 제품 카탈로그(L0)
- 문서 인덱스와 원문 버전(L1)
- 가이던스 축
- ClinicalTrials.gov 시험 등록 정보

전처리(파싱·청킹·임베딩)는 다음 단계에서 다루며, `data/manifest.jsonl`을 입력으로 받습니다.

## 설치

```bash
uv venv .venv && uv pip install -p .venv -e ".[dev,postgres]"
```

## 사용

```bash
# 기관 사이트 접속 점검
.venv/bin/ra-ingest check

# 성분 단위 (holdout인 BAT2206은 제외)
.venv/bin/ra-ingest ingest --ingredient ustekinumab --agencies ema,fda,hc,guidance,ctgov

# 제품 단위. 개발 코드 / 기관별 브랜드명 / FDA 접미사 이름 모두 가능
.venv/bin/ra-ingest ingest --product SB17 --product Wezenla --agencies ema,fda

# 신규 제품 추가 측정 (holdout 포함)
.venv/bin/ra-ingest ingest --product BAT2206 --include-holdout

# 메타데이터만 수집 / 문서 유형 제한
.venv/bin/ra-ingest ingest --ingredient ustekinumab --no-download
.venv/bin/ra-ingest ingest --ingredient ustekinumab --doc-types assessment_report,review,sbd

# 현황 (제품 × 기관), 매니페스트 내보내기
.venv/bin/ra-ingest status
.venv/bin/ra-ingest manifest --out data/manifest.jsonl

# PostgreSQL 사용 (기본은 SQLite data/ra.db)
.venv/bin/ra-ingest --db postgresql+psycopg://user:pass@localhost/ra ingest --ingredient ustekinumab
```

코드에서 호출할 때(이후 RAG/에이전트 프레임에서 쓰는 방식):

```python
from ra_ingest.pipeline import IngestConfig, run_ingest
result = run_ingest(IngestConfig(products=["CT-P43"], agencies=["ema", "fda", "hc"]))
```

## 구조

| 경로 | 내용 |
|---|---|
| `config/products.yaml` | 제품 레지스트리: 기관별 별칭, 식별자 힌트, holdout, 기관별 미승인 표시, HC 문서 시드 URL |
| `config/guidances.yaml` | 가이던스 목록: 관할, 상태, 날짜, 적용 범위, 쟁점 |
| `src/ra_ingest/connectors/` | 기관별 커넥터(`ema`, `fda`, `hc`, `guidance`, `ctgov`). 모두 같은 인터페이스(`base.Connector`)를 따름 |
| `src/ra_ingest/pipeline.py` | 대상 해석 → 허가 정보 → 문서 → 원문 버전 저장, 실행 로그 |
| `src/ra_ingest/db.py` | `programs`, `registrations`, `documents`, `document_versions`, `trials`, `ingest_runs`, `ingest_events` |
| `src/ra_ingest/metadata.py` | 문서 메타데이터 계약, 청크 메타데이터 스키마, 컨텍스트 헤더 |
| `data/raw/<agency>/` | 해시 기반 원문 저장소(git 제외) |

**새 기관을 추가하는 방법**
1. `connectors/<agency>.py`에 `Connector`를 구현합니다.
2. `pipeline.CONNECTORS`에 등록합니다.
3. `models.AGENCIES`에 추가합니다.

## 테스트

```bash
.venv/bin/pytest -q                                                            # SQLite
RA_TEST_DB_URL=postgresql+psycopg://ra:ra@localhost/ra_test .venv/bin/pytest -q  # PostgreSQL
```

테스트 목업은 2026-10-08에 실제 응답으로 확인한 형식(EMA JSON, Purple Book CSV, Drugs@FDA ZIP, FDA 승인 패키지 목차, DPD API, DHPP 검색, FDA 가이던스 목록)을 따릅니다.

주의: FDA는 브라우저가 아닌 User-Agent를 차단하므로 기본 UA가 브라우저형입니다(`http.USER_AGENT`).

## 문서

- [적재 실행 결과 (2026-10-08)](docs/ingestion-report.md)

- [RA 문서 소스 카탈로그](docs/ra-document-sources.md)
- [진행 계획](docs/project-plan.md)
- [청크·임베딩 메타데이터 전략](docs/metadata-strategy.md)
