# 청크·임베딩 메타데이터 전략

작성일: 2026-10-08 · 임베딩: bge-m3(dense + sparse) · LLM: gpt-4o-mini
코드 계약: `src/ra_ingest/metadata.py` (`build_document_metadata`, `ChunkMetadata`, `context_header`)

## 1. 원칙

1. **메타데이터는 두 층으로 나눕니다.**
   - 문서 단위 메타데이터는 적재 시점에 확정합니다. 한 문서의 모든 청크가 같은 값을 그대로 물려받습니다.
   - 청크 단위 메타데이터는 전처리 단계에서 채웁니다.
   - 이렇게 나누면 전처리를 다시 돌려도 문서 단위 정보는 바뀌지 않습니다.
2. **필터용 필드와 임베딩용 텍스트를 구분합니다.**
   - 기관, 제품, 날짜, 가이던스 상태처럼 **정확히 걸러야 하는 값**은 구조화된 필터로만 씁니다.
   - 임베딩 텍스트에는 짧은 컨텍스트 헤더만 붙입니다. 헤더가 길면 서로 다른 청크의 벡터가 비슷해집니다(바이오시밀러 문서는 이미 문장이 서로 비슷함).
3. **이름 변형은 키워드 검색 쪽에서 처리합니다.**
   - `name_variants`(개발 코드, 기관별 브랜드명, FDA 접미사 이름)는 bge-m3 sparse 벡터와 BM25가 맞추도록 둡니다.
   - dense 벡터에는 브랜드명 하나만 넣습니다.
4. **질문은 한국어, 문서는 영어입니다.**
   - 헤더와 필드 값은 원문 언어(영어)로 둡니다.
   - 한국어-영어 간 의미 연결은 bge-m3의 다국어 dense 벡터에 맡깁니다.
   - 약어·코드는 질의를 재작성할 때 영어로 바꿉니다.

## 2. 문서 단위 필드 (모든 청크가 상속)

| 그룹 | 필드 | 쓰임 |
|---|---|---|
| 식별 | `doc_id`, `doc_version_sha256`, `source_url`, `storage_path`, `title` | 원문 위치 인용, 버전 고정 |
| 출처 | `agency`, `jurisdiction`, `doc_kind`(product/guidance), `doc_type`, `native_doc_type` | 기관·문서 유형 필터 |
| 대상 | `ingredient`, `program_id`, `program_role`(reference/biosimilar), `brand_name`, `registration_key`, `name_variants`, `procedure_id` | 제품별 분해 검색, 대조약과 바이오시밀러 구분 |
| 시간 | `decision_date`, `doc_date`, `source_published_at`, `source_updated_at`, `retrieved_at` | "당시 기준" 질문, 답변 최신성 표시 |
| 가이던스 | `guidance_status`, `guidance_effective_from`/`to`, `guidance_reference_no`, `guidance_scope_products`, `guidance_scope_exceptions`, `guidance_partial_supersession`, `topics` | "현재 기준" 필터, 초안/최종 표시, 국소작용 예외 등 적용 범위 |
| 전처리 힌트 | `default_speaker`, `chunking`, `speaker_rule` | 문서 유형별 청킹 방식과 말한 주체 규칙 |

## 3. 청크 단위 필드 (전처리 단계에서 채움)

| 필드 | 설명 |
|---|---|
| `chunk_type` | text / table_row / table / footnote / list / heading |
| `section_path` | 섹션 트리 경로. 예: `["2.6 Clinical aspects", "2.6.2 Pharmacokinetics", "Discussion"]` |
| `section_role` | results / discussion / conclusion / background / label_section |
| `speaker` | applicant / applicant_data / regulator / label / unknown |
| `page_pdf`, `page_printed` | PDF 페이지와 문서에 인쇄된 페이지(둘 다 인용에 필요) |
| `table_id`, `table_caption`, `table_headers`, `footnotes` | 표의 행 청크마다 헤더와 각주를 반복해서 붙임 |
| `study_ids`, `populations` | 시험 ID와 분석집단. 수치 혼입을 막는 핵심 필드 |
| `topics` | 공통 쟁점 어휘(아래 5장) |
| `redacted` | 가림 처리된 부분 → "비공개" 상태 |
| `guidance_section_id`, `guidance_section_status` | 가이던스 문항 단위 상태(예: FDA Q&A I.8 철회) |

## 4. 문서 유형별 전략

| 문서 유형 | 청킹 | 말한 주체 규칙 | 특히 중요한 청크 필드 |
|---|---|---|---|
| EMA 평가보고서 (`assessment_report`) | 섹션 트리 + 표 행 단위 | `Discussion`·`Conclusions` 소절 → regulator, 결과 서술 → applicant_data | `section_path`, `study_ids`, `populations`, `table_headers`, `footnotes` |
| FDA 리뷰 (`review`) | 섹션 트리 + 표 행 단위. 스캔 페이지는 OCR 후 같은 방식 | "Applicant's Position" → applicant, "FDA's Assessment" → regulator(라벨이 있는 양식만) | 위와 같음 + `page_printed`, `redacted`((b)(4)) |
| HC SBD (`sbd`) | HTML 섹션 + 표 행 | 문서 전체 regulator | `section_path`. 허가 후 활동표(PAAT)는 행마다 날짜 포함 |
| EMA 허가 후 절차 (`procedural_steps`) | 표 행 1개 = 절차 1건 | regulator | 행의 절차번호·결정일 → 청크 메타데이터로 올림 |
| 라벨 (`product_information`, `label`, `product_monograph`) | 라벨 섹션(적응증, 용법, 이상반응…) | label | `section_path`. 라벨 버전의 `valid_from`/`valid_to` |
| 가이던스 (`guidance`) | 번호가 붙은 절·문항 단위 | regulator | `guidance_section_id`, `guidance_section_status`, `topics` |
| FDA 목차 페이지 (`review_index`) | 청킹하지 않음(하위 PDF 연결용) | — | — |

## 5. 공통 쟁점 어휘 (`topics`)

`analytical`, `pk`, `comparator_bridging`, `ces`, `immunogenicity`, `extrapolation`, `interchangeability`, `labeling`, `meetings`, `general`

- 가이던스 쪽은 적재 시점에 `config/guidances.yaml`에서 붙입니다.
- 선례 문서 쪽은 전처리 단계에서 섹션 경로와 키워드 규칙으로 붙입니다.
- 이 어휘가 같아야 "이 쟁점에 대해 가이던스는 시점별로 뭐라 하고, 선례는 어땠나"를 한 번에 조회할 수 있습니다.

## 6. 컨텍스트 헤더 (임베딩 텍스트 앞에 붙임)

```
[EU | EMA | assessment_report | Pyzchiva (SB17, ustekinumab) | 2024-04-22 | 2.6.2 Pharmacokinetics > Discussion | study SB17-1001 | regulator]
[US | FDA | guidance | Q&As on Biosimilar Development (Revision 4) | status=draft | section I.8 (withdrawn_from_final) | 2026-03-09]
```

- 헤더에는 기관, 문서 유형, 제품(코드·성분), 날짜, 마지막 두 단계의 섹션 경로, 시험 ID, 말한 주체만 넣습니다.
- 가이던스 상태는 헤더에도 넣고 필터에도 둡니다. 초안이 최종본처럼 인용되는 것을 막기 위해서입니다.

## 7. 검색 시 필터 사용 예

| 질문 유형 | 필터 |
|---|---|
| 제품 비교 | `program_id IN (...)`로 제품별로 따로 검색 → 결과 병합(교차 오염 방지) |
| 당시 기준 | `doc_kind=product AND program_id=X` + 가이던스는 `effective_from <= decision_date` |
| 현재 기준 | `doc_kind=guidance AND jurisdiction=Y AND (effective_to IS NULL)` + `guidance_status` 표시 |
| 기관 판단만 | `speaker=regulator` |
| 수치 질문 | `chunk_type IN (table_row, footnote)` + `study_ids`, `populations` 일치 |
