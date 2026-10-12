# 전처리 워크플로우

작성일: 2026-10-12 · 관련 문서: [전처리 비교 시험](preprocessing-benchmark.md), [로컬 실행 안내](local-run.md), [전처리 실행 계획](preprocessing-plan.md)

전처리는 다음 순서로 진행합니다.
1. 쪽(page)마다 상태를 판정합니다.
2. Docling으로 추출합니다.
3. 규칙으로 고칩니다.

모든 단계는 LLM을 쓰지 않고 Docling의 모델, RapidOCR, 규칙만으로 돕니다. 같은 입력이면 같은 결과가 나옵니다.

현재 처리 결과는 문서 191건, 7,321쪽입니다. 구성은 바이오시밀러 4개, Stelara(목적별 선별), 가이던스입니다.

## 전체 흐름

```
[0 적재] → [1 대상 선별] → [2 워터마크 제거] → [3 쪽별 경로 판정]
        → [4 Docling 추출] → [5 텍스트층 교정] → [6 두 번째 OCR 판독] → [7 표 복원]
        → [8 RA 후처리] → [9 QA 지표·저장]
        (HTML 문서는 4~7 대신 DOM 파싱)
```

## 단계별 설명

| 단계 | 코드 | 쓴 것 | 목적 |
|---|---|---|---|
| **0. 적재** | `ra_ingest` (`ra-ingest`) | requests. EMA JSON, Drugs@FDA·Purple Book, Health Canada DPD·SBD, 가이던스 목록 | 기관별 원문을 받아 sha256 이름으로 저장하고, 문서 메타데이터(기관, 유형, 제품, 날짜)를 DB에 넣습니다. 이미 받은 문서는 다시 받지 않습니다 |
| **1. 대상 선별** | `ra_prep/select.py`, `ra_prep/reference.py` | 규칙 | 같은 내용을 두 번 처리하지 않습니다. 세부 규칙은 아래에 있습니다 |
| **2. 워터마크 제거** | `ra_prep/sanitize.py` | pypdfium2 | 대각선(15~75°)으로 놓인 워터마크 글자를 지웁니다. 철회된 EMA 문서에 있습니다. 지우지 않으면 본문에 섞이고, 그 아래 이미지 표가 "텍스트 있음"으로 잘못 판정됩니다 |
| **3. 쪽별 경로 판정** | `ra_prep/signals.py`, `ra_prep/route.py` | pypdfium2로 쪽 신호 측정 | 쪽마다 처리 방식을 정합니다. 신호와 경로는 아래에 있습니다 |
| **4. Docling 추출** | `ra_prep/extract.py` | **Docling 2.137**. docling-parse(텍스트층), 레이아웃 모델, **TableFormer(accurate)**, OCR 엔진 **RapidOCR** | 쪽 구조(제목, 본문, 목록, 표, 그림, 머리말·꼬리말)와 표의 행·열 구조를 뽑습니다. OCR은 3단계 경로에 따라 정합니다. DIGITAL은 끄고, MIXED는 필요한 곳만, IMAGE·BROKEN_TEXT는 전체 쪽에 강제로 켭니다 |
| **5. 텍스트층 교정** | `ra_prep/textfix.py` | pypdfium2 재판독, 글자 3-gram 언어 모델(`ra_prep/data/trigram_en.json`, 코퍼스로 학습) | FDA 문서의 글꼴 문제를 고칩니다. 세부 내용은 아래에 있습니다 |
| **6. 두 번째 OCR 판독** | `ra_prep/reocr.py` | **RapidOCR(PP-OCRv6)** 216dpi 독립 판독. 필요하면 잘라서 확대해 다시 읽습니다(배율 4가지) | OCR 오류를 잡습니다. 세부 기준은 아래에 있습니다 |
| **7. 그림으로 잡힌 표 복원** | `ra_prep/grid.py` | 6단계 OCR의 줄 좌표 | Docling이 "그림"으로 분류한 표 이미지(FDA 스캔 표 등)를, OCR 줄 위치로 행·열 격자를 만들어 표로 되살립니다 |
| **HTML 문서** | `ra_prep/html.py` | BeautifulSoup | HC SBD처럼 웹페이지로 된 문서는 Docling 대신 DOM을 직접 파싱해 제목·본문·표를 뽑습니다. 더 정확하고 빠릅니다 |
| **8. RA 후처리** | `ra_prep/structure.py` | 규칙 | 검색과 근거 제시에 필요한 정보를 붙입니다(아래 표) |
| **9. QA 지표·저장** | `ra_prep/qa.py`, `ra_prep/store.py`, `ra_prep/pipeline.py` | SQLite/PostgreSQL, 문서별 JSON | 문서마다 품질 지표(경로 분포, 깨짐, 검토 필요 블록 수)를 기록하고 `parsed/<sha256>.json`으로 저장합니다. 같은 원문과 같은 엔진 버전이면 다시 처리하지 않습니다. 교정 규칙만 바뀌면 `ra-prep refix`로 Docling 없이 몇 초 만에 갱신합니다 |

### 1단계: 대상 선별 규칙

- 라벨은 최신 판만 처리합니다.
- EMA 중복 허가 문서는 본문 유사도로 하나만 남깁니다(5단어 묶음 Jaccard 0.6 이상).
- 원 제품(Stelara)은 허가 후 절차 이력을 보고 적응증 관련 문서만 처리합니다.

### 3단계: 쪽 신호와 경로

**측정하는 신호**
- 텍스트층 글자 수
- 이미지가 덮는 면적
- 텍스트 박스 면적 비율(8% 기준)
- 글자 깨짐 정도

| 경로 | 뜻 | 처리 |
|---|---|---|
| DIGITAL | 텍스트층이 온전함 | 텍스트층 사용, OCR 없음 |
| MIXED | 본문은 텍스트층, 표·그림은 이미지 | 이미지 부분만 OCR |
| IMAGE | 스캔 쪽(텍스트층 없음) | 전체 쪽 OCR |
| LEGACY_OCR | 2009년 스캔본의 내장 OCR층 | 내장 OCR층 사용(다시 OCR해도 나아지지 않음) |
| BROKEN_TEXT | 글꼴이 깨진 텍스트층 | 전체 쪽 OCR |
| ARTWORK | 포장·용기 그림 쪽 | 그림 라벨만 |
| BLANK | 빈 쪽 | 건너뜀 |

### 5단계: 텍스트층 교정 내용

- 대체 문자 "�"는 pdfium으로 다시 읽어 채웁니다.
- 문자 코드가 29칸 밀린 글꼴("DQG" → "and")은 영어 점수가 오를 때만 복원합니다.
- 공백으로 사라진 "9", "(" 같은 글자를 되살립니다("0%" → "90%").

### 6단계: 두 번째 판독 기준

- Docling 결과와 두 번째 판독을 비교합니다.
- 다음 징후가 있는 줄을 의심합니다: 깨진 조각, 띄어쓰기 이상, 숫자 불일치
- 의심 줄은 더 나은 쪽으로 바꿉니다. 판단 기준은 조각 수, 오류 토큰 수, 영어 점수입니다.
- 숫자가 끝까지 엇갈리면 `needs_review`로 표시합니다.

### 8단계: RA 후처리 내용

| 항목 | 내용 |
|---|---|
| 머리말·꼬리말 | 쪽 번호와 반복 머리말(예: "xiv Biosimilar Multidisciplinary Evaluation and Review")을 표시해 본문에서 뺍니다 |
| 절 경로 | 번호 체계(2.6.5, IV.C.1.b 등)로 각 블록의 소속 절을 정합니다. 예: `2.6 Clinical aspects > 2.6.2 Pharmacokinetics` |
| 말한 주체 | 기관 판단(`regulator`), 신청자 주장(`applicant`), 신청자 데이터 설명(`applicant_data`), 라벨 문구(`label`)를 구분합니다. 예를 들어 EMA는 Discussion·Conclusions 절이면 기관 판단으로 봅니다 |
| 가림 표시 | FDA 문서의 `(b)(4)` 가림을 기록합니다 |
| 칸 정리·시험 ID | 표 칸을 정리하고, 시험 ID(SB17-3001, NCT 번호 등)를 뽑습니다 |

## 출력 형식

문서 1건 = JSON 1개(`parsed/<sha256>.json`)입니다.

**블록마다 붙는 정보**
- 쪽, 좌표, 유형(text/table)
- 절 경로, 말한 주체
- 출처(`text_layer`/`ocr`/`html`)
- 상태(`kept`/`reread`/`needs_review`)
- 가림 표시, 시험 ID

**문서마다 붙는 정보:** 쪽별 경로와 QA 지표

다음 단계(청킹·임베딩)는 이 파일만 읽습니다([청킹·임베딩 전략](chunking-strategy.md)).

## 이렇게 정한 근거

- **비교 시험으로 선택:** 30쪽 정답 세트(표 칸 1,190개)로 후보를 비교해 이 구성(A1S)을 골랐습니다.
  - 디지털 쪽 표 칸 정확도는 약 99.8%, 이미지 쪽 숫자 칸 정확도는 95~98%입니다.
  - PaddleOCR-VL은 표 구조가 더 좋았지만 CPU에서 14배 느려 채택하지 않았습니다. GPU 환경용 선택지로 남겨 두었습니다.
  - 자세한 내용은 [전처리 비교 시험](preprocessing-benchmark.md)에 있습니다.
- **처리 속도:** CPU 4코어 기준 쪽당 약 2~6초입니다. 이미지가 섞인 쪽(MIXED·IMAGE)이 많을수록 느립니다.
- **엔진 버전:** `ra_prep.ENGINE_VERSION`(현재 `prep-1.4`)으로 관리합니다. 추출 단계(Docling·OCR)가 같고 교정·후처리만 바뀐 버전은 `REFIXABLE_FROM`에 넣어 재파싱 없이 갱신합니다.

## 실행

```bash
P=.venv-prep/bin/python
$P -m ra_ingest ingest --ingredient ustekinumab --agencies ema,fda,hc,guidance   # 0. 적재
$P -m ra_prep select --product SB17                                            # 1. 선별 결과 미리 보기
$P -m ra_prep run --product SB17 --workers 2                                   # 2~9
$P -m ra_prep status                                                           # 처리 현황
```

원 제품 문서는 목적별 계획을 만든 뒤 처리합니다(`scripts/reference_plan.py` → `config/reference_docs.yaml`).

로컬에서 한 번에 돌리는 방법은 [로컬 실행 안내](local-run.md)를 보세요.
