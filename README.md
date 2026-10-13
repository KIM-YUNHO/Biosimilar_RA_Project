# Biosimilar RA Project — 문서 적재 (`ra-ingest`)

성분명 또는 제품명으로 대상을 지정하고, 기관(EMA, FDA, Health Canada)을 골라 공개 규제 문서를 적재합니다.

**적재 대상**
- 제품 카탈로그(L0)
- 문서 인덱스와 원문 버전(L1)
- 가이던스 축
- ClinicalTrials.gov 시험 등록 정보

적재 다음 단계는 전처리(`ra-prep`: 파싱·OCR)와 색인(`ra-index`: 청킹·임베딩·검색)입니다. 세 단계는 같은 DB와 같은 원문 저장소를 씁니다.

## 설치

```bash
uv venv .venv && uv pip install -p .venv -e ".[dev,postgres]"
```

## DB와 원문 저장소

세 CLI(`ra-ingest`, `ra-prep`, `ra-index`)는 같은 설정을 읽습니다.

| 환경변수 / 옵션 | 기본값 | 다른 값 |
|---|---|---|
| `RA_DB` / `--db` | SQLite `data/ra.db` | PostgreSQL + pgvector: `postgresql+psycopg://ra:ra@localhost:5432/ra` |
| `RA_STORE` / `--store` | `data/` 폴더 | 공유 볼륨 경로, 또는 S3 호환 버킷 `s3://bucket/prefix`(`.[s3]` 설치) |

- DB에는 파일 경로 대신 저장소 키(`raw/fda/ab/<sha256>.pdf`, `parsed/<sha256>.json`)가 저장됩니다. 저장소 위치를 바꿔도 DB는 그대로 씁니다.
- PostgreSQL은 `docker compose up -d`(PostgreSQL 16 + pgvector 0.8)로 띄웁니다.
- 기존 SQLite 데이터 옮기기:
  1. `ra-ingest migrate-paths`: 예전 행의 절대 경로를 키로 바꿉니다.
  2. `ra-ingest copy-db --to $RA_DB`: 빈 PostgreSQL로 모든 표를 복사합니다.
  - 자세한 순서는 [로컬 실행 안내](docs/local-run.md) 8장에 있습니다.
- 다른 DB가 5432를 쓰고 있으면 `RA_PG_PORT=5434 docker compose up -d`로 포트를 바꿉니다.

**요청한 대로 받아지는 방식**
- 성분이나 제품을 요청하면 세 기관 카탈로그에서 허가를 모으고, 레지스트리에 없는 제품은 같은 개발 프로그램끼리 묶어(`auto_program` 이벤트에 근거) 문서를 받습니다.
- 실패는 문서·출처·호스트 단위로 가둬 기록하고 계속합니다. 이전에 받은 문서는 이번 실패로 상태가 바뀌지 않습니다.
- FDA가 이 클라이언트를 봇으로 차단하면(`www.fda.gov`가 `abuse-detection-apology`로 이동) 차단으로 기록하고 그 호스트를 잠시 건너뜁니다. FDA 문서 목록은 `accessdata.fda.gov` 개요 페이지로 대체합니다. `ra-ingest check`가 이 차단을 NG로 표시합니다.
- 같은 요청을 다시 실행하면 빠진 원문만 채워집니다(`refill`).
- HTML 문서 버전 지문 방식이 바뀌어, 이전에 적재한 DB는 한 번 `ra-ingest rekey-html`을 실행합니다(`--dry-run`으로 먼저 확인).

## 사용

```bash
# 기관 사이트 접속 점검
.venv/bin/ra-ingest check

# 성분 단위 (holdout인 BAT2206은 제외)
.venv/bin/ra-ingest ingest --ingredient ustekinumab --agencies ema,fda,hc,guidance,ctgov

# 제품 단위. 개발 코드 / 기관별 브랜드명 / FDA 접미사 이름 모두 가능
.venv/bin/ra-ingest ingest --product SB17 --product Wezenla --agencies ema,fda

# 레지스트리에 없는 성분·제품도 요청만 하면 됨: 카탈로그에서 프로그램을 만들어 문서를 받음
.venv/bin/ra-ingest ingest --ingredient adalimumab
.venv/bin/ra-ingest ingest --product Hadlima --agencies ema,fda,hc

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
| `src/ra_ingest/pipeline.py` | 대상 해석 → 허가 정보 → 프로그램 → 문서 → 원문 버전 저장 → 빠진 원문 채우기, 실행 로그 |
| `src/ra_ingest/autoprogram.py` | 레지스트리에 없는 제품의 프로그램 묶기(브랜드·허가권자·유사 이름), 근거 기록 |
| `src/ra_ingest/db.py` | `programs`, `registrations`, `documents`, `document_versions`, `trials`, `ingest_runs`, `ingest_events` |
| `src/ra_ingest/metadata.py` | 문서 메타데이터 계약, 청크 메타데이터 스키마, 컨텍스트 헤더 |
| `src/ra_ingest/objstore.py` | 원문·파싱 결과 저장소(로컬 폴더 / S3). DB에는 키만 저장 |
| `data/raw/<agency>/` | 해시 기반 원문 저장소(git 제외) |
| `src/ra_prep/` | 전처리: `select`(대상 선별), `signals`·`route`(쪽 판정), `sanitize`(워터마크), `extract`(Docling), `textfix`(텍스트층 교정), `reocr`·`grid`(두 번째 판독), `html`(HC 웹 문서), `structure`(RA 후처리), `qa`, `pipeline`, `cli` |
| `src/ra_chunk/` | 색인: `chunker`(절 단위 청크), `embed`(bge-m3), `index`(색인 단계), `store`(색인 표, pgvector), `search`(하이브리드 검색), `query`(규칙 라우팅·용어집), `rerank`, `retrieve`, `cli` |
| `src/ra_answer/` | 답변 생성(`ra-answer`): `evidence`(근거 블록·인용 구간), `generate`(LLM 1회 + 코드 검증), `llm`(OpenAI), `cli` |
| `docker-compose.yml` | PostgreSQL 16 + pgvector |
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
.venv-prep/bin/ra-prep warmup                         # 모델 다운로드 + 시험 처리(긴 실행 전에 확인)
.venv-prep/bin/ra-prep run --product SB17 --workers 2 # 문서 단위 병렬 처리
.venv-prep/bin/ra-prep refix                          # 엔진 갱신 시 저장된 결과에 텍스트 교정·후처리만 다시 적용

# 로컬에서 적재부터 색인까지 한 번에 (docs/local-run.md)
.venv-prep/bin/python scripts/run_local.py --workers 2
```

처리 순서: 워터마크 제거 → 쪽 단위 경로 판정(`DIGITAL`/`MIXED`/`IMAGE`/`LEGACY_OCR`/`BROKEN_TEXT`/`ARTWORK`/`BLANK`, HTML은 DOM) → Docling 추출(경로별 OCR 설정) → 텍스트층 교정(깨진 글자를 pdfium 판독으로, 밀린 글꼴 인코딩 복원) → 두 번째 OCR 판독으로 깨진 줄 교정·그림으로 잡힌 표 복원 → RA 후처리(머리말·꼬리말, 섹션, 말한 주체, 가림 표시) → QA 지표.
같은 문서 버전과 같은 엔진 버전 조합이면 다시 처리하지 않습니다(`--force`로 재처리).

비교 시험(정답 세트 30쪽)과 결과는 [전처리 비교 시험](docs/preprocessing-benchmark.md)에 있습니다.

```bash
.venv-prep/bin/python eval/candidates.py A1 C        # 후보 실행 → eval/out/
.venv-prep/bin/python eval/evaluate.py A1 C          # 정답 세트 대비 채점
```

## 색인 (`ra-index`)

파싱 결과를 청크로 나누고 bge-m3(dense + sparse)로 임베딩해 색인 표(`index_documents`, `index_chunks`, `index_parents`)에 넣습니다. 청크 방식과 검색 흐름은 비교 시험으로 정한 것을 그대로 씁니다([청킹·임베딩 전략](docs/chunking-strategy.md) 7·8장).
- 청크: V2m(절 단위 약 400토큰, 120토큰 미만 소절은 상위 절에 합침, 표 전체, 부모, 컨텍스트 헤더)
- 검색: 규칙 라우팅 → 용어집 → 혼합 점수(dense + 0.3 × sparse) → 선택적 재순위
- 라우팅으로 정해지는 검색 묶음은 `ra_chunk.search.search_plan` 한 곳에 있습니다(제품별 검색, 규칙 질문의 가이던스 검색, 원 제품 문서 제외, 문서 유형 검색, 라벨 절 규칙). 평가 스크립트와 `ra-index search`가 같은 함수를 씁니다.

```bash
uv pip install -p .venv-prep -e ".[prep,rag,postgres]"

.venv-prep/bin/ra-index run                          # 파싱된 문서 전부 (바뀐 것만 다시 색인)
.venv-prep/bin/ra-index run --product SB17 --device cuda
.venv-prep/bin/ra-index status                       # 모델·청커별 색인 문서·청크 수
.venv-prep/bin/ra-index search "SB17의 PK 시험 동등성 한계는?" --k 10 --rerank
.venv-prep/bin/python eval/retrieval/check_index_parity.py   # 색인 검색 = 평가 때 검색인지 확인
```

**다시 색인하는 경우와 건너뛰는 경우**
- 다음 조합이 그대로면 건너뜁니다: 문서 버전, 전처리 엔진, 청커(설정과 `chunker.py` 코드), 임베딩 모델, 문서 메타데이터(가이던스 상태 등 헤더에 들어가는 값 포함).
- 하나라도 바뀌면 다시 색인하고, 그 문서의 이전 색인은 은퇴(`retired_at`) 처리해 검색에서 뺍니다.
- 전에 임베딩한 적이 있는 텍스트는 벡터를 다시 계산하지 않고 색인에서 복사합니다.
- 전처리가 건너뛴 최신 판(지난 라벨, 중복 허가 문서)의 이전 색인도 은퇴시킵니다.

벡터 저장은 DB에 따라 다릅니다.
- **PostgreSQL:** pgvector `halfvec`(dense)와 `sparsevec`(sparse). 검색은 SQL로 합니다.
- **SQLite:** 바이트와 JSON. 검색은 numpy로 합니다.

## 테스트

```bash
.venv/bin/pytest -q                                                            # SQLite
RA_TEST_DB_URL=postgresql+psycopg://ra:ra@localhost/ra_test .venv/bin/pytest -q  # PostgreSQL (pgvector 필요)
```

색인 시험은 가짜 임베더를 써서 모델 없이 돕니다. 확인하는 것:
- 다시 색인하는 조건과 건너뛰는 조건
- 은퇴 처리
- 색인 검색 결과가 평가 스크립트의 메모리 검색과 순서까지 같은지

테스트 목업은 2026-10-08에 실제 응답으로 확인한 형식(EMA JSON, Purple Book CSV, Drugs@FDA ZIP, FDA 승인 패키지 목차, DPD API, DHPP 검색, FDA 가이던스 목록)을 따릅니다.

주의: FDA는 브라우저가 아닌 User-Agent를 차단하므로 기본 UA가 브라우저형입니다(`http.USER_AGENT`).

## 문서

- [적재 실행 결과 (2026-10-08)](docs/ingestion-report.md)
- [전처리 파이프라인 설계](docs/preprocessing-design.md)
- [전처리 워크플로우 (단계별 도구와 목적)](docs/preprocessing-workflow.md)
- [전처리 실행 계획 (확정안)](docs/preprocessing-plan.md)
- [로컬 실행 안내 (설치 → 적재 → 전처리 한 번에)](docs/local-run.md)
- [전처리 비교 시험 (정답 세트 30쪽)](docs/preprocessing-benchmark.md)
- [청킹·임베딩 전략 (비교·재순위 시험 결과 포함)](docs/chunking-strategy.md)
- [답변 생성 1차 구현 (수정사항·미반영 부분)](docs/answer-generation.md)
- [시험 세트 정리 (전처리 30쪽, 검색 20문항)](docs/test-sets.md)
- [파이프라인 운영·오케스트레이션 설계 (요청형·감시형, Airflow 검토)](docs/pipeline-operations.md)

- [RA 문서 소스 카탈로그](docs/ra-document-sources.md)
- [진행 계획](docs/project-plan.md)
- [청크·임베딩 메타데이터 전략](docs/metadata-strategy.md)
