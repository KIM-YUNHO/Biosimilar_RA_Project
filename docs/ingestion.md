# 문서 적재 (`ra-ingest`)

성분명이나 제품명으로 대상을 정하고 기관(EMA, FDA, Health Canada)을 골라 공개 규제 문서를 적재합니다. README에 있던 적재 세부 내용을 옮긴 문서입니다.

**적재 대상**
- 제품 카탈로그(L0)
- 문서 인덱스와 원문 버전(L1)
- 가이던스 축
- ClinicalTrials.gov 시험 등록 정보

원문은 sha256 이름으로 저장소(`raw/<agency>/ab/<sha256>.pdf`)에 넣고, 문서 메타데이터(기관, 유형, 제품, 날짜)는 DB에 넣습니다. 이미 받은 문서는 다시 받지 않습니다.

## 요청한 대로 받아지는 방식

- **프로그램 묶기:** 성분이나 제품을 요청하면 세 기관 카탈로그에서 허가를 모읍니다. 레지스트리에 없는 제품은 같은 개발 프로그램끼리 묶은 뒤(근거는 `auto_program` 이벤트에 기록) 문서를 받습니다.
- **실패 처리:** 실패는 문서·출처·호스트 단위로 가두고 기록한 뒤 계속합니다. 이전에 받은 문서는 이번 실패로 상태가 바뀌지 않습니다.
- **FDA 차단:** FDA가 이 클라이언트를 봇으로 차단하면(`www.fda.gov`가 `abuse-detection-apology`로 이동) 차단으로 기록하고 그 호스트를 잠시 건너뜁니다.
  - FDA 문서 목록은 `accessdata.fda.gov` 개요 페이지로 대신 가져옵니다.
  - `ra-ingest check`가 이 차단을 NG로 표시합니다.
  - FDA는 브라우저가 아닌 User-Agent를 차단하므로 기본 UA가 브라우저형입니다(`http.USER_AGENT`).
- **빠진 원문 채우기:** 같은 요청을 다시 실행하면 빠진 원문만 채웁니다(`refill`).
- **HTML 지문 갱신:** HTML 문서의 버전 지문 방식이 바뀌었습니다. 이전에 적재한 DB는 `ra-ingest rekey-html`을 한 번 실행합니다(`--dry-run`으로 먼저 확인).

## 명령

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

# DB 지정 (기본은 SQLite data/ra.db, 또는 RA_DB 환경변수)
.venv/bin/ra-ingest --db postgresql+psycopg://user:pass@localhost/ra ingest --ingredient ustekinumab
```

## 코드에서 호출

이후 RAG·에이전트 프레임에서 쓰는 방식입니다.

```python
from ra_ingest.pipeline import IngestConfig, run_ingest
result = run_ingest(IngestConfig(products=["CT-P43"], agencies=["ema", "fda", "hc"]))
```

## 코드 구조

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
| `src/ra_ingest/settings.py` | `RA_DB`, `RA_STORE` 기본값 |

## 새 기관을 추가하는 방법

1. `connectors/<agency>.py`에 `Connector`를 구현합니다.
2. `pipeline.CONNECTORS`에 등록합니다.
3. `models.AGENCIES`에 추가합니다.

## 테스트 목업

테스트 목업은 2026-10-08에 실제 응답으로 확인한 형식을 따릅니다: EMA JSON, Purple Book CSV, Drugs@FDA ZIP, FDA 승인 패키지 목차, DPD API, DHPP 검색, FDA 가이던스 목록.

실제 적재 결과는 [적재 실행 결과](ingestion-report.md)에 있습니다.
