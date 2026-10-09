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
| `src/ra_prep/` | 전처리: `select`(대상 선별), `signals`·`route`(쪽 판정), `sanitize`(워터마크), `extract`(Docling), `reocr`·`grid`(두 번째 판독), `html`(HC 웹 문서), `structure`(RA 후처리), `qa`, `pipeline`, `cli` |
| `eval/` | 전처리 정답 세트(`gold/`: 쪽 PDF 30개 + 정답 JSON)와 후보 실행·채점 스크립트 |

**새 기관을 추가하는 방법**
1. `connectors/<agency>.py`에 `Connector`를 구현합니다.
2. `pipeline.CONNECTORS`에 등록합니다.
3. `models.AGENCIES`에 추가합니다.

## 전처리 (`ra-prep`)

적재된 문서를 파싱해 `data/parsed/<sha256>.json`(블록 단위: 본문·표·섹션 경로·말한 주체·가림 표시·QA 지표)으로 저장합니다. LLM은 쓰지 않습니다. Docling의 레이아웃·표 모델, RapidOCR, 그리고 규칙으로만 처리합니다.

```bash
uv venv .venv-prep && uv pip install -p .venv-prep -e ".[prep,dev,eval]"

.venv-prep/bin/ra-prep select --product SB17          # 처리 대상 미리보기 (최신 라벨만, 중복 허가 문서 제외)
.venv-prep/bin/ra-prep run --product SB17 --agencies ema,fda,hc
.venv-prep/bin/ra-prep run --doc-id 232               # 문서 하나만
.venv-prep/bin/ra-prep status                         # 처리 현황, 쪽당 시간, 검토 필요 블록 수
```

처리 순서: 워터마크 제거 → 쪽 단위 경로 판정(`DIGITAL`/`MIXED`/`IMAGE`/`LEGACY_OCR`/`BROKEN_TEXT`/`ARTWORK`/`BLANK`, HTML은 DOM) → Docling 추출(경로별 OCR 설정) → 두 번째 OCR 판독으로 깨진 줄 교정·그림으로 잡힌 표 복원 → RA 후처리(머리말·꼬리말, 섹션, 말한 주체, 가림 표시) → QA 지표.
같은 문서 버전과 같은 엔진 버전 조합이면 다시 처리하지 않습니다(`--force`로 재처리).

비교 시험(정답 세트 30쪽)과 결과는 [전처리 비교 시험](docs/preprocessing-benchmark.md)에 있습니다.

```bash
.venv-prep/bin/python eval/candidates.py A1 C        # 후보 실행 → eval/out/
.venv-prep/bin/python eval/evaluate.py A1 C          # 정답 세트 대비 채점
```

## 테스트

```bash
.venv/bin/pytest -q                                                            # SQLite
RA_TEST_DB_URL=postgresql+psycopg://ra:ra@localhost/ra_test .venv/bin/pytest -q  # PostgreSQL
```

테스트 목업은 2026-10-08에 실제 응답으로 확인한 형식(EMA JSON, Purple Book CSV, Drugs@FDA ZIP, FDA 승인 패키지 목차, DPD API, DHPP 검색, FDA 가이던스 목록)을 따릅니다.

주의: FDA는 브라우저가 아닌 User-Agent를 차단하므로 기본 UA가 브라우저형입니다(`http.USER_AGENT`).

## 문서

- [적재 실행 결과 (2026-10-08)](docs/ingestion-report.md)
- [전처리 파이프라인 설계](docs/preprocessing-design.md)
- [전처리 실행 계획 (확정안)](docs/preprocessing-plan.md)
- [전처리 비교 시험 (정답 세트 30쪽)](docs/preprocessing-benchmark.md)

- [RA 문서 소스 카탈로그](docs/ra-document-sources.md)
- [진행 계획](docs/project-plan.md)
- [청크·임베딩 메타데이터 전략](docs/metadata-strategy.md)
