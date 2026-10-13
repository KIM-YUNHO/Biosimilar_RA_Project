# Biosimilar RA Project

바이오시밀러 허가(RA) 문서를 근거로 질문에 답하는 RAG 시스템입니다. 기관이 공개한 평가보고서·심사 문서·라벨·가이던스를 모아 파싱하고 색인합니다. 질문이 들어오면 근거를 찾아, 출처(문서·쪽·원문 인용)가 달린 답변을 만듭니다.

## 1. 무엇을 하는 프로젝트인가

| 항목 | 내용 |
|---|---|
| 대상 성분 | 우스테키누맙(ustekinumab) |
| 바이오시밀러 | SB17(Pyzchiva), ABP654(Wezlana), CT-P43(Steqeyma), BMAB1200(Yesintek) |
| 원 제품 | Stelara. 세 가지 목적으로만 씁니다: 원 허가 시험, 적응증 이력, 대조 라벨 |
| 기관 | EMA, FDA, Health Canada, 그리고 각 기관의 바이오시밀러 가이던스 |
| 보류(holdout) | BAT2206. 신규 제품 추가 시간을 재기 위해 남겨 둔 제품입니다 |

**답하려는 질문 예시**
- SB17의 비교 임상(3상) 시험 번호와 대상 환자는?
- 바이오시밀러 4개의 비교 효능 시험 1차 평가변수와 동등성 한계를 비교해줘
- Wezlana는 어떤 근거로 인터체인저블로 지정됐나?
- FDA는 비교 임상 효능 시험 요구를 2015 가이던스와 지금 사이에 어떻게 바꿨나?
- Wezlana와 Stelara 라벨의 적응증은 어떻게 다른가?

전처리와 검색은 LLM 없이 모델과 규칙만으로 돕니다. 같은 입력이면 같은 결과가 나옵니다. LLM은 마지막 답변 생성에서 한 번만 호출합니다.

## 2. 파이프라인 한눈에

```
ra-ingest  ──▶  ra-prep  ──▶  ra-index  ──▶  ra-answer
 적재            전처리         색인·검색        답변 생성
```

| 단계 | CLI | 하는 일 | 핵심 도구 |
|---|---|---|---|
| 적재 | `ra-ingest` | 기관 카탈로그에서 제품·문서를 찾아 원문을 받고, 메타데이터를 DB에 넣습니다 | requests, EMA JSON, Drugs@FDA·Purple Book, HC DPD·SBD |
| 전처리 | `ra-prep` | 쪽마다 경로를 정해 추출하고, 깨진 글자·OCR 오류를 규칙으로 고칩니다. 절 경로와 말한 주체(기관/신청자)를 붙입니다 | **Docling**(레이아웃, TableFormer), **RapidOCR**, pypdfium2 |
| 색인 | `ra-index` | 절 단위로 청크를 만들어 임베딩하고, 규칙 라우팅과 혼합 검색으로 근거를 찾습니다 | **bge-m3**(dense + sparse), **bge-reranker-v2-m3**(재순위) |
| 답변 | `ra-answer` | 근거를 인용 구간으로 나눠 LLM에 한 번 보냅니다. 돌아온 주장은 코드로 검증합니다(인용 ID, 제품, 숫자) | OpenAI API |

단계별 상세 설명은 다음 문서에 있습니다.
- [적재](docs/ingestion.md)
- [전처리 워크플로우](docs/preprocessing-workflow.md)
- [청킹·임베딩 전략](docs/chunking-strategy.md)
- [답변 생성](docs/answer-generation.md)

## 3. 빠른 시작

### 설치

```bash
uv venv .venv --python 3.13
uv pip install -p .venv -e ".[prep,rag,llm,postgres,dev,eval]"
```

- extra는 단계별로 나뉩니다.
  - `prep`: 전처리. Docling과 torch가 함께 설치됩니다
  - `rag`: 색인
  - `llm`: 답변
  - `postgres`, `s3`: 저장소
- NVIDIA GPU, Windows 경로, 회사망 모델 캐시 같은 환경 차이는 [로컬 실행 안내](docs/local-run.md)에 있습니다.
- 아래 예시는 macOS / Linux 경로(`.venv/bin/`)입니다. Windows는 `.venv\Scripts\`로 바꿉니다.

### 방법 A: 처음부터 한 번에 실행

적재 → 모델 준비 → 전처리 → 색인 → 현황까지 한 번에 돌립니다. 모든 단계는 이어서 실행할 수 있습니다. 이미 받은 원문, 이미 파싱한 문서, 이미 색인한 문서는 건너뜁니다.

```bash
.venv/bin/python scripts/run_local.py --workers 2      # 우스테키누맙 바이오시밀러 4개 + 가이던스
```

CPU 4코어 기준 전처리가 가장 오래 걸립니다(쪽당 약 2~6초).

### 방법 B: 저장소에 올려 둔 결과로 시작

`temp_data/`에 전처리 결과와 색인 내보내기가 들어 있습니다. 쓰면 전처리와 임베딩을 다시 돌리지 않아도 됩니다.

| 경로 | 내용 |
|---|---|
| `temp_data/ra_parsed/ra.db` | SQLite DB: 문서 메타데이터, 쪽별 경로, 전처리 상태 |
| `temp_data/ra_parsed/parsed/` | 파싱 결과 JSON(문서당 1개, `<sha256>.json`) |
| `temp_data/ra_parsed/export/documents.csv` | 문서 목록(기관, 유형, 제품, 제목, 원문 URL) |
| `temp_data/ra_index_V2m.tar.gz.part00~02` | V2m 색인 내보내기(청크, 부모, dense·sparse 벡터). 나눠 둔 압축 파일 |

```bash
cat temp_data/ra_index_V2m.tar.gz.part0* | tar xzf - -C temp_data      # → temp_data/export/

export RA_DB=sqlite:///$PWD/temp_data/ra_parsed/ra.db
export RA_STORE=$PWD/temp_data/ra_parsed
.venv/bin/python scripts/import_index.py --export temp_data/export      # 임베딩 모델 없이 색인 표 채우기
.venv/bin/ra-index status
```

- `import_index.py`는 청크 토큰 수를 세려고 bge-m3 토크나이저(몇 MB)만 받습니다. 모델 본체는 받지 않습니다.
- 원문 PDF는 들어 있지 않습니다. 원문이 필요하면 `ra-ingest`로 다시 받습니다(sha256이 같으면 같은 키로 저장됩니다).
- PostgreSQL에 넣으려면 `--db`에 PostgreSQL URL을 줍니다.

### 질문하기

```bash
# 검색만
.venv/bin/ra-index search "SB17의 PK 시험 동등성 한계는?" --k 10 --rerank

# 답변 생성: 먼저 .env에 OPENAI_API_KEY를 넣습니다
cp .env.example .env
.venv/bin/ra-answer "SB17의 PK 시험 동등성 한계는?" --dry-run    # 모델 호출 없이 근거와 토큰 수 확인
.venv/bin/ra-answer "SB17의 PK 시험 동등성 한계는?" --rerank
```

답변 보고서(근거, 주장, 인용문, 제외 사유, 사용량)는 `data/answers/<시각>.json`에 저장됩니다.

## 4. 현재 상태와 성능

2026-10-13 기준입니다.

| 단계 | 상태 |
|---|---|
| 적재 | 문서 296건: 바이오시밀러 4개, Stelara, 가이던스 |
| 전처리 | **191건, 7,321쪽 완료**(엔진 `prep-1.4`). 66건은 선별 규칙으로 건너뜀(지난 라벨판, 중복 허가 문서 등). RMP 등 4단계 문서는 이번 범위에서 제외 |
| 색인 | V2m 청크 + bge-m3. 색인 내보내기가 `temp_data/`에 있음 |
| 검색 평가 | 정답 세트 23문항, 근거 묶음 54개(문서·쪽·원문 인용) |
| 답변 생성 | 1차 구현(LLM 1회 + 코드 검증). 단위 시험은 통과했고, 실제 모델 호출 평가는 아직 하지 않음 |

**전처리 정확도** (정답 세트 30쪽, 표 칸 1,190개)
- 디지털 쪽 표 칸: 약 99.8%
- 이미지 쪽 숫자 칸: 95~98%

**검색 재현율** (191건 코퍼스, 23문항 / 54묶음, 한국어 질문 + 용어집 + 재순위)

| | @5 | @10 |
|---|---|---|
| 청크에서 찾음 | 50.0% | 61.1% |
| 부모(절·표 전체)에서 찾음 | 63.0% | **70.4%** |

- "부모에서 찾음"은 답변 단계가 실제로 읽는 범위입니다.
- 58건 코퍼스(1차, 41묶음)에서는 @10 부모 포함 78.0%였습니다. 차이는 대부분 새로 넣은 어려운 문항(원 제품 시험, 적응증 이력, 라벨 비교)에서 나옵니다.
- 아직 못 찾는 근거
  - Q19: 같은 문서 안에 비슷한 표가 여러 개 있음
  - Q22: 정답 근거 설계 문제

자세한 내용은 다음 문서에 있습니다.
- [청킹·임베딩 전략](docs/chunking-strategy.md) 7~12장
- [시험 세트 정리](docs/test-sets.md)

## 5. 저장소 구조

| 경로 | 내용 |
|---|---|
| `src/ra_ingest/` | 적재. 기관별 커넥터, 프로그램 묶기, DB 모델, 원문 저장소(로컬/S3), 설정 |
| `src/ra_prep/` | 전처리 |
| `src/ra_chunk/` | 색인과 검색 |
| `src/ra_answer/` | 답변 생성 |
| `config/` | 설정 파일(아래) |
| `scripts/` | 보조 스크립트(아래) |
| `eval/` | 평가. `gold/`, `candidates.py`, `evaluate.py`는 전처리 정답 세트와 채점, `retrieval/`은 검색 정답 세트·비교 시험·결과 |
| `tests/` | 단위 시험. 모델 없이 돕니다(가짜 임베더·가짜 LLM 클라이언트) |
| `docs/` | 설계·실행·시험 결과 문서 |
| `temp_data/` | 전처리 결과와 색인 내보내기(3장 방법 B) |
| `data/` | 로컬 DB와 원문·파싱 결과 저장소(git 제외) |
| `docker-compose.yml` | PostgreSQL 16 + pgvector 0.8 |

**`src/` 모듈**
- `src/ra_prep/`
  - `select`: 대상 선별
  - `reference`: 원 제품 문서 목적
  - `signals`, `route`: 쪽 판정
  - `sanitize`: 워터마크
  - `extract`: Docling
  - `textfix`: 텍스트층 교정
  - `reocr`, `grid`: 두 번째 판독
  - `html`: HC 웹 문서
  - `structure`: RA 후처리
  - `qa`, `pipeline`, `cli`
- `src/ra_chunk/`
  - `chunker`: V2m 청크
  - `embed`: bge-m3
  - `index`, `store`: 색인 표, pgvector
  - `search`: 혼합 검색, `search_plan`
  - `query`: 규칙 라우팅·용어집
  - `rerank`, `retrieve`, `cli`
- `src/ra_answer/`
  - `evidence`: 근거 블록·인용 구간
  - `generate`: LLM 1회 + 코드 검증
  - `llm`, `cli`

**`config/` 파일**
- `products.yaml`: 제품 레지스트리
- `guidances.yaml`: 가이던스 목록
- `reference_docs.yaml`: 원 제품 문서 목적
- `query_glossary.yaml`: 한국어 RA 용어집

**`scripts/` 파일**
- `run_local.py`: 한 번에 실행
- `reference_plan.py`: 원 제품 처리 계획
- `export_index.py`, `import_index.py`: 색인 내보내기·가져오기
- `profile_corpus.py`

## 6. DB·저장소 설정

네 CLI(`ra-ingest`, `ra-prep`, `ra-index`, `ra-answer`)는 같은 설정을 읽습니다. 설정 하나로 모든 단계가 바뀝니다.

| 환경변수 / 옵션 | 기본값 | 다른 값 |
|---|---|---|
| `RA_DB` / `--db` | SQLite `data/ra.db` | PostgreSQL + pgvector: `postgresql+psycopg://ra:ra@localhost:5432/ra` |
| `RA_STORE` / `--store` | `data/` 폴더 | 공유 볼륨 경로, 또는 S3 호환 버킷 `s3://bucket/prefix`(`.[s3]` 설치) |

**저장소 키**
- DB에는 파일 경로 대신 저장소 키(`raw/fda/ab/<sha256>.pdf`, `parsed/<sha256>.json`)가 저장됩니다.
- 저장소 위치를 바꿔도 DB는 그대로 씁니다.

**벡터 저장**
- PostgreSQL: pgvector `halfvec`(dense)와 `sparsevec`(sparse). 검색은 SQL로 합니다.
- SQLite: 바이트와 JSON. 검색은 numpy로 합니다.

**PostgreSQL 쓰기**
- `docker compose up -d`로 띄웁니다. 다른 DB가 5432를 쓰고 있으면 `RA_PG_PORT=5434 docker compose up -d`로 포트를 바꿉니다.
- 기존 SQLite 데이터를 옮기려면 다음 순서로 실행합니다.
  1. `ra-ingest migrate-paths`: 예전 행의 절대 경로를 키로 바꿉니다.
  2. `ra-ingest copy-db --to $RA_DB`: 빈 PostgreSQL로 모든 표를 복사합니다.
  - 자세한 순서는 [로컬 실행 안내](docs/local-run.md) 8장에 있습니다.

**다시 처리하는 조건**

| 단계 | 건너뛰는 조건 | 다시 처리하는 조건 |
|---|---|---|
| 적재 | 같은 원문(sha256) | 빠진 원문만 채움 |
| 전처리 | 같은 문서 버전 + 같은 엔진 버전 | `--force`. 교정 규칙만 바뀐 경우는 `ra-prep refix`(Docling 없이 몇 초) |
| 색인 | 문서 버전, 전처리 엔진, 청커(설정과 코드), 임베딩 모델, 문서 메타데이터가 모두 같음 | 하나라도 바뀌면 다시 색인. 이전 색인은 은퇴(`retired_at`) 처리해 검색에서 빠짐. 전에 임베딩한 텍스트는 벡터를 복사 |

## 7. 자주 쓰는 명령

```bash
.venv/bin/ra-ingest check                                       # 기관 사이트 접속 점검
.venv/bin/ra-ingest ingest --ingredient ustekinumab --agencies ema,fda,hc,guidance,ctgov
.venv/bin/ra-ingest status

.venv/bin/ra-prep warmup                                        # 모델 다운로드 + 시험 처리
.venv/bin/ra-prep select --product SB17                         # 처리 대상 미리 보기
.venv/bin/ra-prep run --product SB17 --workers 2
.venv/bin/ra-prep status

.venv/bin/ra-index run                                          # 바뀐 문서만 다시 색인
.venv/bin/ra-index status

.venv/bin/pytest -q                                             # 단위 시험 (SQLite)
RA_TEST_DB_URL=postgresql+psycopg://ra:ra@localhost/ra_test .venv/bin/pytest -q   # PostgreSQL

.venv/bin/python eval/retrieval/check_gold.py                   # 검색 정답 인용문이 파싱 결과에 있는지
.venv/bin/python eval/retrieval/check_index_parity.py           # 색인 검색 = 평가 때 검색인지
```

적재 옵션(제품 단위, 신규 성분, 메타데이터만, 문서 유형 제한), 자동 프로그램 묶기, FDA 차단 처리는 [적재](docs/ingestion.md)에 있습니다.

## 8. 문서 목록

**설계와 실행**
- [적재: 명령, 프로그램 묶기, 실패 처리](docs/ingestion.md)
- [라벨 적재 개편 계획: 문서 계열 구분, 구조화 원천(SPL) 우선](docs/label-ingestion-plan.md)
- [전처리 워크플로우: 단계별 도구와 목적](docs/preprocessing-workflow.md)
- [전처리 파이프라인 설계](docs/preprocessing-design.md)
- [전처리 실행 계획(확정안)](docs/preprocessing-plan.md)
- [청킹·임베딩 전략(비교·재순위 시험 결과 포함)](docs/chunking-strategy.md)
- [청크·임베딩 메타데이터 전략](docs/metadata-strategy.md)
- [답변 생성 1차 구현(수정사항·미반영 부분)](docs/answer-generation.md)
- [로컬 실행 안내: 설치 → 적재 → 전처리 → 색인](docs/local-run.md)
- [파이프라인 운영·오케스트레이션 설계(요청형·감시형, Airflow 검토)](docs/pipeline-operations.md)

**시험과 결과**
- [시험 세트 정리: 전처리 30쪽, 검색 23문항](docs/test-sets.md)
- [전처리 비교 시험(정답 세트 30쪽)](docs/preprocessing-benchmark.md)
- [적재 실행 결과(2026-10-08)](docs/ingestion-report.md)

**배경**
- [RA 문서 소스 카탈로그](docs/ra-document-sources.md)
- [진행 계획](docs/project-plan.md)
