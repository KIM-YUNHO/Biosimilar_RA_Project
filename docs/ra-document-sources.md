# 바이오시밀러 RA 문서 소스 카탈로그 — 적재 파이프라인 참고

작성일: 2026-10-08 · 상태: 설계 참고 문서(구현 전)

**범위 전제**
- 같은 성분(우스테키누맙) 제품 5개: SB17, ABP 654, CT-P43, BMAB1200, BAT2206. 기관별 제품명과 승인 현황은 [진행 계획](project-plan.md) 1.1절 참고
- 기관: EMA, FDA, Health Canada(HC)
- 쟁점: 전 범위(PK·면역원성에 한정하지 않음)
- 가이던스를 독립된 축으로 둠

**표기**
- ✅ 공식 문서나 공개 자료에서 확인함
- ⚠️ 확인 필요. 작성 시점에 해당 사이트 접속이 막혀 원본을 직접 열어보지 못했음. 수집기를 처음 돌릴 때 실제 응답으로 검증할 것.

---

## 1. 적재 구조: LLM을 쓰지 않는 층부터 쌓는다

| 층 | 내용 | LLM 사용 | 비용 |
|---|---|---|---|
| **L0 사전 정보 테이블** | API·메타데이터에서 가져오는 제품별 기본 정보. 이름, 식별자, 허가일, 상태, 대조약, 제형, 허가 후 변경 이력, 문서 목록 | 없음 | 거의 없음 |
| **L1 문서 인덱스 + 원문 보관 + 파싱** | 원본 저장, 해시·날짜·상태 기록, 텍스트·표 추출. OCR은 텍스트층이 없는 페이지에만 적용 | 없음 | 로컬 연산 |
| **L2 검색 인덱스** | 섹션 경로·제품·기관·문서 날짜가 붙은 청크, 키워드 + 벡터 검색 | 임베딩만 | 낮음 |
| **L3 필드 추출** | 비교임상 수행 여부, 외삽 근거 같은 해석 항목 | 있음 | 질문이 요구한 칸만 추출 |

L3의 운영 원칙은 세 가지입니다.
- 미리 전부 추출하지 않습니다. 질문이 처음 요구한 (제품 × 항목) 칸만, 해당 섹션만 읽어서 추출합니다.
- 추출 결과는 원문 위치와 함께 캐시하고 검수 상태를 남깁니다. 같은 칸은 다시 추출하지 않습니다.
- 추출 스키마가 바뀌면 바뀐 필드만 다시 추출합니다.

**LLM 비용에 대한 판단 (추정)**
- 확인된 분량: EMA EPAR 3개가 418쪽, FDA Pyzchiva 통합 리뷰 152쪽 + 품질 리뷰 54쪽.
- 5개 제품 × 3개 기관에 라벨과 가이던스까지 더하면 수천 쪽 규모입니다. 1회 전체 통독은 수백만 토큰 정도로 추정합니다.
- 실제 비용을 키우는 요인은 세 가지입니다.
  1. 스캔 페이지를 비전 모델로 읽는 것
  2. 스키마를 바꿀 때마다 전체를 다시 추출하는 것
  3. 에이전트가 질문마다 긴 원문을 반복해서 읽는 것
- 위 L0~L3 구조는 이 세 가지를 각각 줄이기 위한 것입니다.

**스캔본 여부는 적재 단계에서 측정합니다**
- "FDA 문서는 스캔본이 많다"는 가설은 아직 검증되지 않았습니다.
- 그래서 파싱할 때 페이지마다 텍스트층이 있는지 검사하고(`pdffonts`, `pdftotext` 결과), 문서 인덱스에 `text_layer_ratio`와 `ocr_pages`를 기록합니다.
- OCR은 우선 로컬 도구(OCRmyPDF, Tesseract, Docling OCR 등)로 처리하고, 표 인식에 실패한 페이지만 비전 모델로 보냅니다.

---

## 2. 기관별 소스 목록

**우선순위**
- P0: 첫 데모에 필요
- P1: 데모 품질을 높임
- P2: 후속

**층 표기:** 각 표의 "층" 열은 그 소스가 1장의 어느 층(L0~L3)을 채우는지 나타냅니다.

### 2.1 EMA

| ID | 소스 | 층 | 접근·형식 | 키 | 날짜 필드 | 주의 | 우선 |
|---|---|---|---|---|---|---|---|
| E1 | 의약품 데이터(JSON / Excel) | L0 | JSON 다운로드. 하루 2회 갱신(06:00·18:00 암스테르담 시각). Excel도 같은 내용 ✅ | `ema_product_number` | 허가일, `first_published`, `last_updated`, `revision_number` ✅ | 필터 `category=Human`, `biosimilar=Yes`, `medicine_status=Authorised`. 노션 집계(2026-10-01) 기준 바이오시밀러 151개, 우스테키누맙 11개 ✅ | P0 |
| E2 | EPAR 문서 목록(JSON) | L1 문서 발견 | JSON. 문서명, 제품명, 제품번호, 문서 유형, 참조번호, 게시·개정일, URL ✅ | 제품번호 + 문서 유형 | `first_published`, `last_updated` | 문서 유형 `assessment-report`, `scientific-discussion` 등 ✅ | P0 |
| E3 | 공개 평가보고서(최초 허가) | L1·L2·L3 | 텍스트 PDF. 117~168쪽 ✅ | 제품번호 | 문서 내 CHMP 의견일 | `Discussion`·`Conclusions` 소절을 기관 판단으로 1차 태깅. 가림 처리 표시가 있으면 "비공개" 상태로 기록 | P0 |
| E4 | Procedural steps taken and scientific information after authorisation | L0(규칙 파싱) | PDF 표 | 절차번호(EMEA/H/C/xxxx/II/xxxx 형식) | 절차별 결정일 | 허가 후 변경 이력을 시간 순서로 정리한 문서. **같은 URL에 새 버전이 덮어써짐** ⚠️ | P1 |
| E5 | 변경 평가보고서(적응증 추가 등, 공개된 경우) | L1·L2 | PDF | 절차번호 | 결정일 | 모든 변경에 공개 보고서가 있는 것은 아님 | P2 |
| E6 | 제품 정보(SmPC, Annex) | L1·L2 | PDF(다국어) | 제품번호 | 문서 내 개정일 | 영어판만 수집. 같은 URL 덮어쓰기 ⚠️ → (URL, 해시)로 버전 보존 | P1 |
| E7 | 과학 가이드라인 | 가이던스 | 웹페이지 + PDF | 참조번호(예: EMEA/CHMP/BMWP/42832/2005 Rev1) | 표지의 채택일, "Date for coming into effect" ✅ | 문서 안에 대체 관계가 명시됨(예: Rev1이 2005판을 대체) ✅ | P0 |

### 2.2 FDA

| ID | 소스 | 층 | 접근·형식 | 키 | 날짜 필드 | 주의 | 우선 |
|---|---|---|---|---|---|---|---|
| F1 | Purple Book 월별 CSV/XLSX | L0 | 월별 파일 ✅ | BLA 번호 + 제품번호 | 승인일, 최초 허가일, 독점기간 만료일, 파일 기준월 | 상단은 변경분(N/R/U), 하단은 전체 목록 → 분리해서 파싱 ✅. 허가 유형은 351(k) Biosimilar / Interchangeable ✅. 열 이름은 실제 헤더로 확인 ⚠️ | P0 |
| F2 | Drugs@FDA 데이터 파일(ZIP, 탭 구분 12개 테이블, 평일 매일 갱신) | L0 + L1 문서 발견 | ZIP ✅ | `ApplNo` + `SubmissionType` + `SubmissionNo` | `ApplicationDocsDate`, `SubmissionStatusDate` ✅ | ApplicationDocs에 문서 유형·제목·URL·날짜가 있고, Submissions에 보충 신청 이력이 있음 ✅. CDER 치료용 생물의약품 BLA 포함, CBER 제외 ✅ | P0 |
| F3 | 승인 패키지(목차 페이지 + 승인서·라벨·통합 리뷰·품질 리뷰·기타 리뷰·행정 서신) | L1·L2·L3 | PDF | BLA + 신청 구분(Orig1s000, s001…) | 게시 시점이 승인보다 늦을 수 있음 | (b)(4) 가림 처리 → "비공개"로 기록. 스캔본 비율은 1장 방식으로 측정. 파일 경로의 연도 ≠ 승인일(노션 기록) | P0 |
| F4 | 라벨: DailyMed SPL | L1·L2 + 라벨 시간축 | 웹서비스 v2(XML/JSON). `/spls/{setid}/history`로 버전 이력 조회 ⚠️ | SetID + 버전 | 버전별 유효 시점 | 한 제품에 유통사별 SetID가 여러 개(Pyzchiva: Cordavis, Sandoz — 노션 기록) | P1 |
| F5 | FDA 가이던스 | 가이던스 | 검색 페이지(HTML) + PDF. 열: Issue Date, Topic, Guidance Status, Open for Comment, Comment Closing Date on Draft, Docket Number ✅. 일괄 내보내기 기능은 확인 못 함 ⚠️ | Docket 번호 | 발행일, 상태(초안/최종) | 문서 일부 문항만 개정되는 경우가 있음(예: Q&A Rev 4가 2021 최종본의 I.8, I.10, I.19만 철회) → 섹션 단위 상태 필요 | P0 |
| F6 | openFDA drugsfda API | F2 보완 | JSON API ✅ | `application_number` | — | 문서 URL 필드 이름은 직접 확인 필요 ⚠️ | P2 |
| F7 | Federal Register API | 가이던스 날짜·상태 교차 확인 | JSON API | Docket 번호 | 공고일, 의견 마감일 | 가이던스 발행 공고(notice of availability)로 날짜를 검증 | P2 |

### 2.3 Health Canada

| ID | 소스 | 층 | 접근·형식 | 키 | 날짜 필드 | 주의 | 우선 |
|---|---|---|---|---|---|---|---|
| H1 | Drug Product Database(DPD) | L0 | REST API(`health-products.canada.ca/api/drug/…`: drugproduct, activeingredient, company 등, JSON/XML) + 전체 추출 파일 ✅ | DIN, `drug_code` | 상태 날짜 | 바이오시밀러 플래그가 있는지 확인 필요 ⚠️ | P0 |
| H2 | Notice of Compliance(NOC) 데이터 추출 | L0 | 쉼표 구분·따옴표 텍스트 파일(main, brand, DIN product, ingredient, form, route) ✅ | NOC 번호 | `NOC_DATE`(허가일), `NOC_LAST_UPDATE` ✅ | 제출 유형·클래스(NDS/SNDS) 포함 ✅. 갱신 주기는 Read Me에 주간, DB 페이지에 매일로 서로 다르게 적혀 있음 ⚠️ | P0 |
| H3 | Summary Basis of Decision(SBD) | L1·L2·L3 | HTML(`dhpp.hpfb-dgpsa.ca/review-documents/resource/SBD…`). API 없음 ✅ | SBD ID | "Date SBD issued", NOC 날짜 ✅ | HTML이라 OCR이 필요 없음. 허가 후 활동표(PAAT)가 시간축을 제공 ✅. EPAR보다 짧음 → 기관 간 비교 시 "공개자료에 없음" 비율이 다름 | P0 |
| H4 | Regulatory Decision Summary(RDS) | L1·L2 | HTML(`…/resource/RDS…`) ✅ | RDS ID | 결정일 | 보충 신청 결정(예: Pyzchiva 2025-11 바이알·소아 적응증 보충) | P1 |
| H5 | Product Monograph | L1·L2 | PDF(DPD에서 링크) ⚠️ | DIN | 문서 내 개정일 ⚠️ | 라벨에 해당하는 문서 | P1 |
| H6 | 임상정보 공개 포털(PRCI) | L1 | PDF(대용량 임상시험보고서 등) | — | — | 바이오시밀러가 포함되는지 불확실 ⚠️ → 제품별로 존재 여부만 먼저 확인 | P2 |
| H7 | HC 바이오시밀러 가이던스 + Summary of changes 페이지 | 가이던스 | HTML(canada.ca) ✅ | — | 2016 개정 → 2025 초안 → 2026-05-19 최종 ✅. 시행일은 확인 필요 ⚠️ | HTML이라 섹션 단위로 다루기 쉬움 | P0 |

### 2.4 기관 공통

| ID | 소스 | 층 | 접근·형식 | 용도 | 우선 |
|---|---|---|---|---|---|
| X1 | ClinicalTrials.gov API v2 | L0 | JSON ✅ | 개발 코드 ↔ NCT ID ↔ 시험 단계·설계·적응증·주요 평가변수. 예: SB17-3001 = NCT04967508(노션 기록) | P1 |

**X1을 쓰면 LLM 없이 미리 채울 수 있는 칸이 생깁니다**
- 채울 수 있는 칸: "3상 비교임상이 등록됐는가", "어떤 적응증·평가변수로 했는가"
- 단, 등록 정보는 기관의 판단이 아닙니다. 이 칸에는 "출처: 등록 정보"라고 표시하고, 심사 문서 근거와 구분합니다.

---

## 3. 가이던스 수집 시작 목록

아래는 출발점입니다. 수집할 때마다 상태(초안/최종/철회/대체)를 다시 확인하고 `status_checked_at`을 기록합니다.

| 관할 | 문서 | 알려진 상태·날짜 |
|---|---|---|
| EMA | Guideline on similar biological medicinal products(총괄), CHMP/437/04 Rev 1 | 최종 ⚠️ |
| EMA | …biotechnology-derived proteins: quality issues, EMA/CHMP/BWP/247713/2012 | 최종 ⚠️ |
| EMA | …biotechnology-derived proteins: non-clinical and clinical issues, EMEA/CHMP/BMWP/42832/2005 Rev1 | 2014-12 채택, 2015-07-01 시행. 2005판을 대체 ✅ |
| EMA | Similar biological medicinal products containing monoclonal antibodies: non-clinical and clinical issues | 최종 ⚠️ |
| EMA | Reflection paper on a tailored clinical approach in biosimilar development | 2025-04 초안 → 2026 최종 ✅ |
| EMA | 총괄 가이드라인 개정 concept paper | 개정 진행 중 ⚠️ |
| EMA/HMA | 바이오시밀러 상호교환성 과학적 근거 성명 | 2022 ⚠️ |
| FDA | Scientific Considerations in Demonstrating Biosimilarity to a Reference Product | 2015 최종 ✅ |
| FDA | 같은 문서의 Updated Recommendations for Assessing the Need for Comparative Efficacy Studies | 2025-10 초안 ✅. 국소작용 제품 예외 조항 있음 ✅ |
| FDA | Development of Therapeutic Protein Biosimilars: Comparative Analytical Assessment and Other Quality-Related Considerations | 2025-09 최종(노션 기록) |
| FDA | Clinical Pharmacology Data to Support a Demonstration of Biosimilarity | 2016-12 최종 ✅ |
| FDA | Considerations in Demonstrating Interchangeability With a Reference Product | 2019 최종 → 2024-06 초안 개정 ✅ |
| FDA | Questions and Answers on Biosimilar Development and the BPCI Act + New and Revised Draft Q&As (Revision 4) | 2021 최종 + 2026-03 초안. I.8, I.10, I.19 철회 후 초안으로 재발행 ✅ |
| FDA | Formal Meetings Between the FDA and Sponsors or Applicants of BsUFA Products | 2025-07 최종(노션 기록) |
| FDA | Labeling for Biosimilar and Interchangeable Biosimilar Products | 최종 ⚠️ |
| HC | Guidance: Information and submission requirements for biosimilar biologic drugs | 2016 → 2025 초안 → 2026-05-19 최종 ✅ |

---

## 4. L0 사전 정보 테이블 설계

허가는 기관별로 따로 존재합니다. 그래서 **개발 프로그램(제품) 테이블**과 **기관별 허가 테이블**로 나눕니다. 5개 제품 × 3개 기관이면 허가 행은 최대 15개라서, 프로그램 간 대응은 처음에 사람이 확인해서 고정합니다.

### 4.1 `program` (개발 프로그램 단위)

| 필드 | 예 | 출처 |
|---|---|---|
| `program_id`, `dev_code` | SB17 | 수동(초기 확인) |
| `inn` | ustekinumab | E1 / F1 / H1 |
| `developer` | Samsung Bioepis | 수동 |
| `reference_product` | Stelara | F1(대조약 열), E3 |
| `trials[]` | NCT04967508 (SB17-3001, 3상, 판상건선) | X1 |

### 4.2 `registration` (기관별 허가 단위)

| 필드 | EMA | FDA | HC |
|---|---|---|---|
| 브랜드명 | E1 | F1 | H1 |
| 식별자 | EMA 제품번호 | BLA + 제품번호 | DIN(복수), NOC 번호 |
| 허가 보유자 / 유통사 | E1 | F1, F4 | H1 |
| 허가 유형 | biosimilar 플래그 | 351(k) Biosimilar / Interchangeable | H2 제출 유형·클래스 |
| 최초 허가일 | 허가일 | 승인일 | `NOC_DATE` |
| 상태 | `medicine_status` | 시판 상태 | DPD 상태 |
| 제형·함량·경로 | E6(파싱) ⚠️ | F1 | H1 |
| 허가 후 변경 이력 | E4(규칙 파싱) | F2 Submissions | H2(SNDS), H3 PAAT, H4 |
| 문서 목록·파싱 상태 | E2 | F2 ApplicationDocs | H3, H4, H5 |
| 정보 기준일 | 파일 스냅샷 시각 | 파일 기준월 | 추출 파일 날짜 |

L0만으로 LLM 없이 답할 수 있는 질문:
- "기관별 이름·허가일·제형은?"
- "인터체인저블 지정 여부는?"
- "허가 후 변경은 몇 건이고 언제였나?"
- "어떤 문서가 있고 분석할 수 있는 상태인가?"

L0만으로 답할 수 없는 질문: 비교임상이 필요했는지에 대한 기관의 판단, 외삽 근거. 이런 질문은 L3에서 해당 섹션만 추출해서 답합니다.

---

## 5. 문서 인덱스 필드 (L1)

**공통 필드**
- `agency`, `doc_type`, `title`, `language`, `source_url`, `file_sha256`
- 날짜: `retrieved_at`(수집일), `source_published_at`, `source_updated_at`, `doc_date`
- 버전: `version_label`, `supersedes`, `superseded_by`
- 처리: `parser_version`, `page_count`, `parse_status`, `text_layer_ratio`, `ocr_pages`

**심사자료·라벨에 추가**
- `product_ids`, `procedure_id`, `procedure_type`(최초 / 변경 / 적응증 추가 / 제형 추가)
- `decision_date`, `covers`(이 문서가 다루는 적응증·제형), `redaction`
- 라벨은 `valid_from`, `valid_to`

**가이던스에 추가**
- `jurisdiction`, `issuer`, `reference_no`
- `status`, `status_checked_at`
- `published_at`, `consultation_end`, `adopted_at`, `effective_from`, `effective_to`
- `scope_products`, `scope_topics`
- 섹션 단위 `status`, `replaced_by`

**질문 유형별로 쓰는 날짜**

| 질문 유형 | 쓰는 필드 |
|---|---|
| "당시 기준" | 선례의 `decision_date` + 그 시점에 시행 중이던 가이던스 |
| "현재 기준" | 기준일을 오늘로 두고 `effective_from`/`effective_to`·`status`로 필터. 초안이면 초안이라고 표시 |
| 답변의 최신성 표시 | `retrieved_at`, `status_checked_at` |

---

## 6. 파이프라인 공통 주의사항

1. **같은 URL 덮어쓰기:** EMA 제품 정보·허가 후 절차 문서는 같은 URL에 새 버전이 올라옵니다 ⚠️. URL이 아니라 (URL, sha256) 쌍으로 버전을 관리합니다.
2. **날짜 혼동:**
   - 결정일, 문서 날짜, 게시·개정일, 수집일, 시행일, 상태 확인일은 모두 별도 필드입니다.
   - FDA 파일 경로에 있는 연도는 승인일이 아닙니다.
3. **이름 불일치:**
   - 같은 프로그램이라도 기관마다 이름이 다를 수 있습니다(예: ABP 654 = Wezlana(US) / Wezenla(EU)).
   - EMA에서는 한 프로그램에 제품번호가 여러 개 붙을 수 있습니다(중복 허가).
   - 이름이 아니라 공식 식별자와 개발 코드로 연결합니다.
4. **부분 개정:** 가이던스는 문서 전체가 아니라 일부 문항만 철회되거나 대체될 수 있으므로 섹션 단위 상태가 필요합니다.
5. **비공개 표시:** FDA의 (b)(4), EMA의 가림 처리는 "비공개" 상태로 기록하고, "찾지 못함"과 구분합니다.
6. **말한 주체 태깅(결정됨):** 문서 구조로 1차 태깅합니다.
   - EMA: `Discussion`·`Conclusions` 소절 → 기관 판단
   - FDA: 신청자 입장과 FDA 평가 라벨이 있으면 그 라벨을 사용
   - HC SBD: 문서 전체를 HC 시각의 요약으로 봄
   - 위 규칙으로 정해지지 않는 애매한 문장만 LLM으로 판별합니다.
7. **수집 예절:** 기관 사이트의 호출 정책을 따르고, 원본은 로컬에 캐시해서 재수집을 최소화합니다.
