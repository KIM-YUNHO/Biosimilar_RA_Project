# 로컬 실행 안내

적재(`ra-ingest`) → 모델 준비 → 전처리(`ra-prep`) → 현황 확인까지를 내 컴퓨터에서 돌리는 방법입니다. Windows, macOS, Linux에서 같은 명령으로 돌아갑니다.

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
uv pip install -p .venv-prep -e ".[prep,dev,eval]"
```

가상환경의 python 경로:
- macOS / Linux: `.venv-prep/bin/python`
- Windows: `.venv-prep\Scripts\python.exe`

아래 예시는 macOS / Linux 기준입니다. Windows는 경로만 바꾸면 됩니다.

설치 확인:

```bash
.venv-prep/bin/python -m pytest -q          # 59개 통과 (모델 없이 도는 규칙 시험)
.venv-prep/bin/python -m ra_prep warmup     # 모델 다운로드 + 정답 세트 2쪽 처리 (1~3분)
```

`warmup`이 `ok - engine prep-1.1+docling2.137.0+...`로 끝나면 준비가 끝난 것입니다.

## 3. 한 번에 실행

```bash
.venv-prep/bin/python scripts/run_local.py --workers 2
```

순서:
1. 적재: 우스테키누맙, EMA·FDA·HC + 가이던스 + ClinicalTrials.gov. 원문 약 0.6 GB를 받습니다.
2. `warmup`
3. 가이던스 전처리
4. 제품 문서 전처리: 4개 바이오시밀러를 평가보고서·FDA 리뷰·SBD 먼저, 그다음 라벨·허가 서한, 나머지 순으로
5. 현황 출력

| 옵션 | 뜻 |
|---|---|
| `--skip-ingest` | 이미 적재했으면 건너뜀 |
| `--products SB17 CT-P43` | 제품 지정(개발 코드나 브랜드명) |
| `--agencies ema,fda` | 기관 지정 |
| `--workers N` | 문서 N개를 동시에 처리. 코어·메모리에 맞춰 2~4 |
| `--device cuda` | GPU 사용(워커는 1~2개 권장) |
| `--products BAT2206 --include-holdout` | 홀드아웃 제품을 신규 제품처럼 처리하며 시간 측정(P7) |
| `--db postgresql+psycopg://...` | SQLite 대신 PostgreSQL |

**중간에 멈춰도 됩니다.** 다시 실행하면 이미 받은 원문과 이미 처리한 문서(문서 버전 + 엔진 버전이 같은 것)는 건너뜁니다.

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
```

**코드를 새로 받은 뒤(엔진 버전이 올라갔을 때):** `run`은 엔진 버전이 다른 문서를 다시 처리합니다. 바뀐 것이 텍스트 교정·후처리뿐이면(예: prep-1 → prep-1.1) 먼저 `ra-prep refix`를 돌리면 Docling을 다시 돌리지 않고 몇 초 만에 갱신됩니다.

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
| `data/ra.db` | 적재 목록(`documents` 등)과 전처리 기록(`prep_documents`, `page_routes`) |
| `data/raw/<agency>/` | 원문(해시 이름) |
| `data/parsed/<sha256>.json` | 문서 1개 = 파일 1개. 블록마다 쪽·좌표·유형·섹션 경로·말한 주체·출처(text_layer/ocr/html)·상태(kept/reread/needs_review)·가림 표시·시험 ID, 문서마다 QA 지표 |

`ra-prep status`는 처리 문서 수, 쪽당 시간, "검토 필요" 블록 수를 보여 줍니다.

## 7. 문제 해결

| 증상 | 원인과 해결 |
|---|---|
| `warmup`이 다운로드에서 멈춤·실패 | 1장의 모델 주소가 막혀 있음. 열린 망에서 캐시를 만들어 복사 |
| FDA에서 403·"apology" 페이지 | FDA 봇 차단. 잠시 후 다시 실행(받은 것은 건너뜀) |
| `database is locked` | SQLite에 프로세스 두 개가 동시에 씀. 한 번에 하나만 실행하거나 PostgreSQL 사용 |
| 메모리 부족으로 워커가 죽음 | `--workers`를 줄임 |
| 특정 문서만 실패 | `ra-prep status`에서 failed 확인 → `ra-prep run --doc-id N --force`로 재시도. 실패 사유는 `prep_documents.reason`에 남음 |
