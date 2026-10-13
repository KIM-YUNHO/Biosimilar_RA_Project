# 로컬 실행 안내

적재(`ra-ingest`) → 모델 준비 → 전처리(`ra-prep`) → 색인(`ra-index`: 청크·임베딩) → 현황 확인까지를 내 컴퓨터에서 돌리는 방법입니다. Windows, macOS, Linux에서 같은 명령으로 돌아갑니다.

## 1. 준비물

| 항목 | 권장 |
|---|---|
| Python | 3.11 ~ 3.13 (개발·시험은 3.13) |
| 디스크 | 약 12 GB: 패키지·모델 6 GB, 원문 0.6 GB, 전처리 결과 수백 MB |
| 메모리 | 워커 1개당 약 3 GB. 16 GB면 워커 2~3개 |
| 네트워크 | 기관 사이트(EMA, FDA, Health Canada, ClinicalTrials.gov)와 모델 다운로드(아래 표) |
| GPU(선택) | NVIDIA + CUDA. 있으면 Docling 레이아웃·표 모델이 크게 빨라짐 |

**처음 실행할 때 받는 모델**

| 모델 | 받는 곳 | 용도 |
|---|---|---|
| Docling 레이아웃·표 모델 | `huggingface.co`(및 `*.hf.co` CDN) | 쪽 구조 인식, 표 구조 |
| bge-m3(약 2.2 GB), bge-reranker-v2-m3(재순위를 쓸 때) | `huggingface.co` | 임베딩, 재순위 |
| RapidOCR(PP-OCRv6) | `www.modelscope.cn` | OCR, 두 번째 판독 |

회사망에서 위 주소가 막혀 있으면, 열린 망에서 한 번 `ra-prep warmup`을 돌린 뒤 두 캐시 폴더를 복사해 쓰면 됩니다.
- Docling: `~/.cache/huggingface`
- RapidOCR: 가상환경의 `site-packages/rapidocr/models`

## 2. 설치

[uv](https://docs.astral.sh/uv/)를 쓰는 방법입니다(pip도 같은 방식으로 됩니다).

```bash
git clone https://github.com/kim-yunho/biosimilar_ra_project.git Biosimilar_RA_Project && cd Biosimilar_RA_Project
git checkout claude/biosimilar-ra-rag-system-9rfk8h

uv venv .venv-prep --python 3.13
# NVIDIA GPU가 있으면 먼저 CUDA용 torch를 설치 (CUDA 버전에 맞는 index 사용)
#   uv pip install -p .venv-prep torch torchvision --index-url https://download.pytorch.org/whl/cu124
uv pip install -p .venv-prep -e ".[prep,rag,postgres,dev,eval]"
```

가상환경의 python 경로:
- macOS / Linux: `.venv-prep/bin/python`
- Windows: `.venv-prep\Scripts\python.exe`

아래 예시는 macOS / Linux 기준입니다. Windows는 경로만 바꾸면 됩니다.

설치 확인:

```bash
.venv-prep/bin/python -m pytest -q          # 모두 통과 (모델 없이 도는 규칙 시험)
.venv-prep/bin/python -m ra_prep warmup     # 모델 다운로드 + 정답 세트 2쪽 처리 (1~3분)
```

`warmup`이 `ok - engine prep-1.2+docling2.137.0+...`로 끝나면 준비가 끝난 것입니다.

## 3. 한 번에 실행

```bash
.venv-prep/bin/python scripts/run_local.py --workers 2
```

순서:
1. 적재: 우스테키누맙, EMA·FDA·HC + 가이던스 + ClinicalTrials.gov. 원문 약 0.6 GB를 받습니다.
2. `warmup`
3. 가이던스 전처리
4. 제품 문서 전처리: 4개 바이오시밀러를 평가보고서·FDA 리뷰·SBD 먼저, 그다음 라벨·허가 서한, 나머지 순으로
5. 전처리 현황 출력
6. 색인: 파싱된 문서 전부를 청크로 나누고 bge-m3로 임베딩해 색인에 넣음(바뀐 것만)
7. 색인 현황 출력

| 옵션 | 뜻 |
|---|---|
| `--skip-ingest` | 이미 적재했으면 건너뜀 |
| `--products SB17 CT-P43` | 제품 지정(개발 코드나 브랜드명) |
| `--agencies ema,fda` | 기관 지정 |
| `--workers N` | 문서 N개를 동시에 처리. 코어·메모리에 맞춰 2~4 |
| `--device cuda` | GPU 사용(워커는 1~2개 권장) |
| `--products BAT2206 --include-holdout` | 홀드아웃 제품을 신규 제품처럼 처리하며 시간 측정(P7) |
| `--skip-index` | 전처리까지만 |
| `--db postgresql+psycopg://...` | SQLite 대신 PostgreSQL(환경변수 `RA_DB`도 됨) |
| `--store <폴더 또는 s3://...>` | 원문·파싱 결과 저장소(기본 `data/`, 환경변수 `RA_STORE`) |

**중간에 멈춰도 됩니다.** 다시 실행하면 이미 받은 원문, 이미 처리한 문서(문서 버전 + 엔진 버전이 같은 것), 이미 색인한 문서는 건너뜁니다.

## 4. 단계별로 직접 실행할 때

```bash
P=.venv-prep/bin/python
$P -m ra_ingest ingest --ingredient ustekinumab --agencies ema,fda,hc,guidance,ctgov
$P -m ra_prep select --product SB17                       # 무엇을 처리할지 미리 보기
$P -m ra_prep run --guidance --workers 2
$P -m ra_prep run --product SB17 --doc-types assessment_report,review,sbd,rds --workers 2
$P -m ra_prep run --doc-id 48 --force                     # 문서 하나 다시 처리
$P -m ra_prep status
$P -m ra_prep refix                                       # 엔진이 바뀌었을 때 이미 처리한 문서를 갱신(재파싱 없음)
$P -m ra_chunk run                                        # 색인 (= ra-index run)
$P -m ra_chunk status
$P -m ra_chunk search "Wezlana는 어떤 근거로 인터체인저블로 지정됐나?" --rerank
```

**코드를 새로 받은 뒤(엔진 버전이 올라갔을 때):** 바뀐 것이 텍스트 교정·후처리뿐이면(`ra_prep.REFIXABLE_FROM`에 있는 이전 버전), `run`이 저장된 결과에 그 단계만 다시 적용합니다. Docling은 다시 돌리지 않으므로 문서당 1초 이내입니다(출력의 `refixed`). 한꺼번에 갱신하려면 `ra-prep refix`를 씁니다. 추출 단계(Docling·OCR)가 바뀐 버전이면 다시 처리합니다.

## 5. 걸리는 시간 (참고)

클라우드 CPU 4코어, 워커 1개 기준 실측입니다.

| 범위 | 쪽 | 시간 |
|---|---|---|
| 가이던스 28건 | 약 440 | 약 10분 |
| 제품 1개(선별 후) | 1,000~1,400 | 1.2~2시간 |
| 4개 바이오시밀러 + 가이던스 | 약 5,000 | 약 6~7시간 |

- 코어가 많으면 워커를 늘리는 만큼 거의 그대로 빨라집니다.
- GPU에서는 OCR이 필요 없는 쪽이 크게 빨라집니다. OCR 판독(RapidOCR)은 CPU에서 돕니다.

## 6. 결과물

| 위치 | 내용 |
|---|---|
| `data/ra.db` | 적재 목록(`documents` 등), 전처리 기록(`prep_documents`, `page_routes`), 색인(`index_documents`, `index_chunks`, `index_parents`) |
| `data/raw/<agency>/` | 원문(해시 이름) |
| `data/parsed/<sha256>.json` | 문서 1개 = 파일 1개. 블록마다 쪽·좌표·유형·섹션 경로·말한 주체·출처(text_layer/ocr/html)·상태(kept/reread/needs_review)·가림 표시·시험 ID, 문서마다 QA 지표 |

`ra-prep status`는 처리 문서 수, 쪽당 시간, "검토 필요" 블록 수를 보여 줍니다.

`data/raw`, `data/parsed`의 위치는 DB에 경로가 아니라 저장소 키(`raw/...`, `parsed/...`)로 기록됩니다. 저장소 폴더를 옮기면 `--store`(또는 `RA_STORE`)만 바꾸면 됩니다.

## 7. 문제 해결

| 증상 | 원인과 해결 |
|---|---|
| `warmup`이 다운로드에서 멈춤·실패 | 1장의 모델 주소가 막혀 있음. 열린 망에서 캐시를 만들어 복사 |
| FDA에서 403·"apology" 페이지 | FDA 봇 차단. 잠시 후 다시 실행(받은 것은 건너뜀) |
| `ra-ingest check`가 FDA에 `봇 차단 페이지`, 적재 이벤트에 `source_blocked`/`host_blocked` | `www.fda.gov`가 이 IP를 봇으로 보고 차단함(404로 보임). FDA 제품 문서는 `accessdata.fda.gov`로 계속 받아지고, 가이던스 문서와 Drugs@FDA ZIP만 막힘. 차단은 시간이 지나야 풀리며, 풀린 뒤 같은 요청을 다시 실행하면 빠진 것이 채워짐 |
| HC 문서마다 실행할 때 새 버전이 생김 | 옛 지문 방식(조회일 포함)으로 적재된 DB. `ra-ingest rekey-html --dry-run` 후 `ra-ingest rekey-html` |
| `database is locked` | SQLite에 프로세스 두 개가 동시에 씀. 한 번에 하나만 실행하거나 PostgreSQL 사용 |
| 메모리 부족으로 워커가 죽음 | `--workers`를 줄임 |
| `type "vector" does not exist`, `extension "vector" is not available` | PostgreSQL에 pgvector가 없음. `docker compose up -d`의 이미지(pgvector/pgvector)를 쓰거나 pgvector 0.7 이상을 설치 |
| `ra-index run`에 `not parsed`가 나옴 | 최신 판이 아직 전처리되지 않은 문서. `ra-prep run` 후 다시 실행 |
| 특정 문서만 실패 | `ra-prep status`에서 failed 확인 → `ra-prep run --doc-id N --force`로 재시도. 실패 사유는 `prep_documents.reason`에 남음 |

## 8. 기존 데이터를 PostgreSQL로 옮기기

이미 SQLite(`data/ra.db`)로 적재·전처리한 데이터가 있을 때의 순서입니다. 원문과 파싱 결과 파일은 그대로 두고, DB만 옮깁니다.

```bash
P=.venv-prep/bin/python

# 1) 코드를 받고 패키지 다시 설치(위 2장의 명령)
# 2) SQLite 안의 절대 경로를 저장소 키로 바꿈(먼저 --dry-run으로 몇 건인지 확인)
$P -m ra_ingest migrate-paths --dry-run
$P -m ra_ingest migrate-paths

# 3) PostgreSQL + pgvector 띄우기
docker compose up -d

# 4) 모든 표를 빈 PostgreSQL로 복사
$P -m ra_ingest copy-db --to postgresql+psycopg://ra:ra@localhost:5432/ra

# 5) 이후 모든 명령이 PostgreSQL을 쓰도록 설정
export RA_DB=postgresql+psycopg://ra:ra@localhost:5432/ra      # Windows PowerShell: $env:RA_DB="..."

# 6) 색인. 평가 때 계산한 임베딩 캐시를 재사용하면 다시 임베딩하지 않음
$P -m ra_chunk run --embed-cache data/emb_cache/bge-m3.pkl

# 7) 색인 검색이 평가 때 검색과 같은지 확인(같으면 exit 0). 확장 코퍼스는 --corpus eval/retrieval/corpus_docs_v2.txt
$P eval/retrieval/check_index_parity.py
```

- 2단계는 SQLite를 계속 쓸 때도 필요합니다. 새 코드는 키를 읽지만 예전 절대 경로도 계속 읽을 수 있어서, 하지 않아도 동작은 합니다.
- `copy-db`는 대상 DB의 표가 비어 있을 때만 복사합니다. PostgreSQL의 id 순번도 복사한 값 다음부터 이어지게 맞춥니다.
- 5432 포트를 다른 DB가 쓰고 있으면 `RA_PG_PORT=5434 docker compose up -d`로 띄우고 URL의 포트를 맞춥니다.
- 다른 컴퓨터에서 만든 SQLite라면 `/home/.../data/raw/...` 같은 경로도 `copy-db`가 저장소 키로 바꿉니다(`data/raw`, `data/parsed` 아래 경로만).
- 원문 저장소를 공유 볼륨이나 S3 호환 저장소로 옮길 때는 `data/raw`, `data/parsed` 폴더를 그대로 복사합니다. 그다음 `RA_STORE`를 그 위치로 바꾸면 됩니다(예: `s3://ra-docs/prod`, `.[s3]` 설치).

## 9. 색인 export를 PostgreSQL로 가져오기 (모델 없이)

`scripts/export_index.py`로 만든 export(`chunks.jsonl`, `dense_fp16.npy`, `sparse.jsonl`, `parents.jsonl`)와 파싱 결과(`parsed/*.json`)가 있으면 bge-m3 모델 없이 색인 표를 채울 수 있습니다. 8장의 1~5단계(DB 복사)와 파싱 결과를 `data/parsed/`에 두는 것까지 끝난 상태에서 실행합니다.

```bash
P=.venv/bin/python        # Windows: .venv\Scripts\python.exe  (".[postgres,rag]" 설치, torch 불필요)
$P scripts/import_index.py --export path/to/export --store data
```

- 벡터는 임베딩한 텍스트의 해시로 export에서 찾아 씁니다. 청크는 평소처럼 파싱 결과에서 다시 만들기 때문에 `speaker`, `table_caption`, `study_ids` 같은 필드가 모두 채워지고, 색인 키(문서 버전·엔진·청커·모델·메타데이터 해시)가 `ra-index run`이 기대하는 값과 같아 이후 실행에서 건너뜁니다.
- 파싱 결과가 저장소에 없는 문서는 export 행을 그대로 넣습니다(위 필드는 비고, 부모 절의 쪽 번호는 청크 쪽의 합집합). `ra-prep run --doc-id N --force` 후 `ra-index run --force --doc-id N`으로 채울 수 있습니다.
- `prep_documents.output_path`가 저장소 키가 아니면 `parsed/<sha256>.json`으로 바꿉니다.
- 끝에 문서별 청크 수·텍스트 해시를 export와 비교해 다르면 종료 코드 1로 끝납니다.
- 토크나이저(수 MB)만 huggingface.co에서 받습니다.
