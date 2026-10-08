# 적재 실행 결과 — 2026-10-08

실행 명령: `ra-ingest ingest --ingredient ustekinumab --agencies ema,fda,hc,guidance,ctgov`
대상: Stelara(대조약) + SB17, ABP 654, CT-P43, BMAB1200. BAT2206은 holdout으로 제외(카탈로그에는 기록됨).
저장 위치: `data/ra.db`(SQLite), `data/raw/`(원문 300개 버전, 약 537MB). 둘 다 git에 포함하지 않음.

## 1. 수집 현황

| 구분 | EMA | FDA | HC |
|---|---|---|---|
| 제품 문서 | 73 | 168 (수집 167, 1건은 URL 교정으로 대체됨) | 16 |
| 가이던스 | 5 | 25 (FDA 공식 목록에서 자동 수집: 최종 13, 초안 12) | 2 |

시험 등록 정보(ClinicalTrials.gov): 9건. 예: SB17 3상 NCT04967508, SB17 1상 NCT04772274(EU·US Stelara 3-way PK), ABP 654 인터체인저빌리티 시험 NCT04761627.

### 1.1 프로그램 × 기관 허가 (L0)

| 프로그램 | EMA | FDA | HC |
|---|---|---|---|
| Stelara | 2009-01-16 | 2009-09-25 (IV BLA 761044: 2016-09-23) | NOC 2008-12-12 |
| SB17 | Pyzchiva 2024-04-19 (+ Eksunbi 중복 허가, 철회) | Pyzchiva 2024-06-28, 인터체인저블 2025-04-30 | NOC 2024-08-07 |
| ABP 654 | Wezenla 2024-06-20 | Wezlana 2023-10-31, 인터체인저블 2023-10-31 | NOC 2023-12-27 |
| CT-P43 | Steqeyma 2024-08-22 (+ Qoyvolma 중복 허가, 철회) | Steqeyma 2024-12-17, 인터체인저블 2025-04-30 | NOC 2024-07-30 |
| BMAB1200 | Yesintek 2025-02-14 (+ Usrenty 중복 허가) | Yesintek 2024-11-29, 인터체인저블 2025-04-30 | NOC 2025-10-17 |

- **FDA 인터체인저블 날짜 패턴:** Wezlana만 승인일에 인터체인저블이고, 나머지는 모두 2025-04-30입니다. Purple Book에 Wezlana의 첫 인터체인저블 독점기간 만료가 2025-04-30으로 기재돼 있어 서로 들어맞습니다. 데모 D3에 바로 쓸 수 있는 사실입니다.
- **HC 날짜:** NOC(허가)일은 SBD 페이지에서 추출했습니다. DPD의 날짜는 시판일이라 `identifiers.first_market_date`에 따로 둡니다.

### 1.2 심사 문서 가용성

| 프로그램 | EMA | FDA 승인 패키지 | HC |
|---|---|---|---|
| SB17 | 평가보고서 2, 허가 후 절차 4 | 통합 리뷰, 품질 리뷰, 기타(최초 + 보충 신청) | SBD 2, RDS 6 |
| ABP 654 | 평가보고서 1, 허가 후 절차 2 | 통합 리뷰, 품질 리뷰, 기타 | SBD 2, RDS 2 |
| CT-P43 | 평가보고서 2, 허가 후 절차 3 | 통합 리뷰, 품질 리뷰, CDTL 리뷰, 기타 | SBD 2, RDS 4 |
| BMAB1200 | 평가보고서 2, 허가 후 절차 2 | 통합 리뷰, 품질 리뷰, 기타 | SBD 2 |

**우려했던 "최근 승인 제품의 FDA 리뷰 미공개"는 해당되지 않았습니다.** 4개 프로그램 모두 FDA 리뷰가 공개돼 있습니다.

## 2. "FDA는 스캔본이 많다" 확인 결과

| 구분 | 전체 페이지 | 텍스트가 거의 없는 페이지 |
|---|---|---|
| EMA (평가보고서·절차) | 2,394 | 102 (4%) |
| FDA (리뷰) | 2,591 | 409 (16%) |

- **스캔본이 몰린 곳은 FDA 품질 리뷰(chemR)입니다.** ABP 654는 46쪽 중 43쪽, SB17은 54쪽 중 31~32쪽, BMAB1200은 56쪽 중 27쪽이 텍스트층이 없는 전체 페이지 이미지(1224×1584)입니다.
- **통합 리뷰(multidisciplineR)는 텍스트층이 있습니다.**
- 전처리 단계에서는 품질 리뷰에 OCR을 적용해야 합니다.

## 3. 실제 데이터로 확인하고 고친 것

| 항목 | 내용 |
|---|---|
| FDA 봇 차단 | 브라우저가 아닌 User-Agent에는 "apology" 페이지로 리다이렉트 → 브라우저형 UA를 기본값으로 사용 |
| EMA 차단 페이지 | User-Agent가 없으면 200 응답에 "Sorry" HTML이 옴 → PDF가 와야 할 자리에 HTML이 오면 실패로 기록 |
| Purple Book | 블록별 헤더 행, "License Type", "Month D, YYYY" 날짜, 보충 신청 행이 섞임 → 최초 허가일은 Original 행 기준, 인터체인저블 날짜 별도 저장 |
| Drugs@FDA URL | `#page=` 조각, `https://` 중복, 연도 디렉터리 누락, 공백·세미콜론, 유형 오기재(Letter인데 라벨 PDF) → 정규화·교정 |
| FDA 승인 패키지 목차 | 자바스크립트로 링크 생성(`pdfBaseName` + `pdfFiles` 플래그) → 플래그가 1인 문서만 수집 |
| 문서 공유 | FDA 리뷰 하나가 BLA 2개(예: 761285/761331)에 걸림 → `extra.registration_ids`에 모두 기록 |
| HC DPD | 브랜드 검색은 부분 일치("PYZCHIVA I.V."), 성분 검색은 `activeingredient?ingredientname=` → IV·SC 브랜드를 하나로 묶음 |
| HC SBD/RDS | DHPP `review-documents?search=` 로 자동 발견, 제목으로 제품 필터, 날짜는 페이지 본문에서 추출(템플릿별 패턴 5종) |
| FDA 가이던스 | 공식 JSON 목록(`search-for-guidance.json`)에서 상태·발행일·docket 자동 수집. 제목 키워드로만 필터(주제 태그만으로는 무관한 문서가 섞임) |
| EMA 중복 허가 | Eksunbi=SB17, Qoyvolma=CT-P43, Usrenty=BMAB1200 (평가보고서 본문의 개발 코드로 확인) → 레지스트리에 등록 |

## 4. 남은 것

| 항목 | 상태 |
|---|---|
| FDA 2015 "Scientific Considerations" 최종본 | FDA 현재 목록에 항목이 없음. 같은 docket의 2025-10 개정 초안만 있음 → 상태 확인 필요 |
| Upstelda (Amgen, EU 신청 철회) | 평가보고서가 없어 ABP 654 중복 여부 미확인 → 미등록 |
| HC Product Monograph | DPD API로는 링크를 얻을 수 없음. 필요하면 시드 URL로 추가 |
| ClinicalTrials.gov | 개발 코드 검색만으로는 일부 시험이 누락될 수 있음(BMAB1200 건강인 PK 본시험 등) |
| BAT2206 | holdout. 마지막에 `--product BAT2206 --include-holdout`으로 적재하고 수동 개입 시간을 측정 |
| 전처리 | 다음 단계. 입력은 `ra-ingest manifest`가 만드는 `data/manifest.jsonl` |
