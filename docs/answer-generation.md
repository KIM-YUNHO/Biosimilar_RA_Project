# 답변 생성 (`ra-answer`): 1차 구현

작성일: 2026-10-13

검색(`ra-index search`, `ra_chunk.retrieve`) 결과를 받아 LLM 호출 한 번으로 인용 달린 답변을 만드는 단계입니다. 참고 코드(`biosimilar_collection/rag`의 `ra_pipeline`, `ra_answer`, `ra_spans`, `ra_models`, `openai_chat`)에서 생성 쪽 구조만 가져왔고, 검토 호출과 질문별 규칙은 넣지 않았습니다.

**현재 상태**
- 단위 테스트 16개(`tests/test_answer.py`)는 가짜 클라이언트로 통과합니다.
- 실제 OpenAI 호출과 실제 색인 위 실행은 아직 하지 않았습니다. 막힌 이유는 세 가지입니다.
  - API 키를 설정하지 않았습니다.
  - `.venv`에 torch, openai, tiktoken이 없습니다.
  - `data/ra.db`가 없습니다.

## 1. 흐름

```
Retriever.retrieve(question)                         ← 기존 (규칙 라우팅 → 용어집 → 혼합 검색 → 선택적 재순위)
  ↓ hits (청크 + parent 텍스트 + 라우팅된 제품)
① 근거 블록     같은 parent는 한 블록으로 합침 → E1..En
                 문서 필드: 기관, 제품, 문서 유형, 제목, 쪽, 절 경로, 화자, needs_review
② 배치          제품별로 번갈아(round-robin) 한 블록씩, 통째로만 넣음
                 프롬프트+스키마가 RA_LLM_INPUT_TOKENS를 넘으면 그 블록은 빼고 omitted에 기록
③ 인용 구간      블록을 줄 단위로 나눠 S1..Sn
                 줄은 절의 문단 또는 마크다운 표의 행. 표 구분선(| --- |)과 빈 줄은 읽기용이라 인용할 수 없음
④ 생성 (1회)    strict JSON schema
                 claims[{citation_span_ids, text, kind, products}] (최대 8개), gaps
⑤ 코드 검증     아래 표
⑥ 출력          통과한 주장만 answer_ko에 표시 ([E#] 인용, 근거 목록, 추가 확인 필요 요청)
                 보고서 JSON에 근거·주장·인용문·제외 사유·사용량 저장
```

**코드 검증 (⑤)**

| 검사 | 제외 사유 |
|---|---|
| 형식(text, kind, products, ID 목록) | `invalid_claim_shape` |
| 인용 ID가 실제로 보낸 구간인지 | `unknown_span_id` |
| 선언한 제품이 인용 근거의 제품인지 | `product_not_in_cited_sources` |
| 제품 근거를 인용했는데 제품을 비워 두었는지 | `product_scope_missing` |
| 본문에 나온 바이오시밀러 이름이 products에 있는지 | `named_product_not_declared` |
| 주장의 숫자가 인용문에 모두 있는지 | `number_not_in_cited_sources` (`missing_numbers` 함께 기록) |
| 인용 블록이 파싱 검토 대상인지 | 제외하지 않고 `needs_review` 표시 |

- 인용문은 모델이 쓰지 않습니다. 코드가 원문에서 잘라 붙이므로 원문에 없는 인용은 생길 수 없습니다.
- kind 값별 표시: `interpretation`은 "해석:", `applicant_position`은 "(신청자)", `regulator_position`은 "(기관)"을 앞에 붙입니다.
- 상태값
  - `answered`: 통과한 주장이 있고 gaps와 제외가 없음
  - `partial`: 통과한 주장이 있지만 gaps나 제외가 있음
  - `not_found`: 통과한 주장이 없음
  - `error`: 호출 실패 또는 응답 파싱 실패
  - `dry_run`: 모델을 호출하지 않음

## 2. 사용법

```bash
uv pip install -p .venv -e ".[rag,llm,postgres]"     # torch는 prep extra(docling)와 함께 오거나 따로 설치
cp .env.example .env                                  # OPENAI_API_KEY 입력 (.env는 git 제외)

.venv/Scripts/ra-answer "SB17의 PK 시험 동등성 한계는?" --dry-run   # 호출 없이 근거와 토큰 수 확인
.venv/Scripts/ra-answer "SB17의 PK 시험 동등성 한계는?" --rerank
.venv/Scripts/ra-answer "SB17과 CT-P43의 면역원성 비교" --k 8 --out data/answers/q1.json
```

| 환경변수 | 기본값 | 설명 |
|---|---|---|
| `OPENAI_API_KEY` | (없음) | 모델 호출에 필요. `--dry-run`에는 필요 없음 |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | |
| `RA_LLM_MODEL` | `gpt-5.6-luna` | |
| `RA_LLM_REASONING_EFFORT` | `none` | `reasoning_effort`로 보냄. 비우면 보내지 않음(이 파라미터가 없는 모델용) |
| `RA_LLM_MAX_OUTPUT` | `4000` | `max_completion_tokens` |
| `RA_LLM_INPUT_TOKENS` | `24000` | 프롬프트 예산. 근거는 이 안에서 블록 단위로 들어감 |
| `RA_LLM_TIMEOUT` / `RA_LLM_RETRIES` | `120` / `2` | |

- 검색 옵션(`--k`, `--mode`, `--rerank`, `--depth`, `--no-glossary`, `--device`)은 `ra-index search`와 같습니다.
- 보고서 JSON의 기본 위치는 `data/answers/<시각>.json`입니다. 담기는 항목:
  - `evidence`: 블록 전문
  - `omitted`
  - `claims`: 인용문 포함
  - `rejected`: 제외 사유 포함
  - `gaps`, `raw_reply`, `calls`(모델 스냅샷, finish_reason, 토큰), `usage`, `routed`, `retrieval`

코드에서 호출할 때:

```python
from ra_answer.generate import answer
from ra_answer.llm import OpenAIChat
report = answer(question, retriever.retrieve(question, k=10), OpenAIChat(), titles=titles, names=names)
```

## 3. 참고 코드 대비 수정사항

| 항목 | 참고 코드 | 이번 구현 | 이유 |
|---|---|---|---|
| LLM 호출 수 | 3회(질문 이해, 생성, 검토) | 1회(생성) | 1차는 생성만 확인. 검토는 6장 미반영 |
| 질문 이해 | LLM이 조사 과제 1~4개로 분해(`ra_tasks`) | 기존 규칙 라우터 결과를 프롬프트 `routed`로 전달 | 이 프로젝트는 라우팅·용어집·검색 묶음이 이미 규칙으로 정해져 있음 |
| 근거 단위 | 청크와 가장 가까운 제목 블록을 따로 넣음(`ra_retrieval.packet`) | parent 블록(절 전체 또는 표 전체), 같은 parent는 합침 | parent가 이미 절 제목 경로와 문맥을 담고 있음 |
| 근거 배치 | 그룹별 round-robin, 블록 통째로, 같은 시험 문맥 확장(그룹당 1,200토큰) | round-robin과 통째로만 유지. 확장 없음 | 확장은 6장 미반영 |
| 인용 구간 | 문단과 HTML `<table>` 셀 단위(`ra_spans`) | 줄 단위(문단, 마크다운 표 행) | 이 프로젝트의 표는 마크다운(`chunker.table_markdown`) |
| 응답 계약 | 원질문 문장(R1..)이나 질문 의무 항목별 `request_results`/`requirement_results`, claim에 `task_ids`, `claim_basis` | 단일 `claims` + `gaps`. `citation_span_ids`를 맨 앞에 두는 방식은 유지 | 과제 분해가 없으므로 단순화 |
| span ID 제약 | 항상 enum | 500개 이하면 enum, 넘으면 `pattern: ^S[0-9]+$`, 어느 쪽이든 코드에서 재검사 | OpenAI structured outputs의 enum 개수 한도 |
| 숫자 검사 | span 모드에서는 끄고 유형별 수량 검사(`ra_quantities`) 사용 | span 모드에서도 숫자 집합 포함 검사 | 유형별 검사를 옮기지 않았으므로 최소 안전장치. 7장의 오탐 참고 |
| 제품 검사 | 선언 제품 ⊆ 인용 제품, 언급 제품 ⊆ 선언 제품 | 같음. 언급 검사는 바이오시밀러 이름만(원제품 Stelara는 대조약으로 자주 나와서 제외) | |
| 화자 | 문서 역할로만 구분 | 파싱 단계의 `speaker`(regulator / applicant / applicant_data)를 근거에 표시해 kind 판단을 도움 | 이 프로젝트에 있는 필드 |
| 검토 필요 원문 | figure 설명 등 일부만 차단 | `needs_review` 블록을 표시(제외하지 않음) | |
| 프롬프트 | ustekinumab와 평가 문항 전용 규칙 다수(QTPP, 브리징, Part, 선정 기준 등) | 일반 RA 규칙만 남김 | 질문 세트 과적합 제거 |
| 모델 설정 | 코드에 하드코딩(`gpt-4o-mini`, `gpt-5.6-luna` 허용 목록), 입력 16,384 고정 | 환경변수. 입력 예산 기본 24,000 | |
| luna 파라미터 | `max_completion_tokens`, `reasoning_effort='none'`, temperature/seed 미전송 | 같음 | 참고 코드 기준. 7장 참고 |
| 토큰 계산 | `tiktoken.encoding_for_model(model)` | `tiktoken.get_encoding("o200k_base")`, 없으면 추정(한글 1자 1토큰) | 새 모델명이 tiktoken 매핑에 없을 수 있음 |
| 호출 기록 | 일일 호출 장부, 요청·응답 파일 캐시, 동일 요청 재호출 금지 | 보고서 JSON의 `calls`만 | 6장 미반영 |

### 기존 코드 변경

- `src/ra_chunk/retrieve.py`: `Retrieved`에 `parent_pages`(parent 텍스트의 쪽 번호) 필드를 추가했습니다. 기본값이 있어서 기존 호출과 시험은 그대로 통과합니다.
- `pyproject.toml`: `ra_answer` 패키지, `ra-answer` 스크립트, `llm` extra(`openai`, `tiktoken`)를 추가했습니다.
- `.gitignore`: `.env`를 추가했습니다.
- `.env.example`: 새 파일입니다.

## 4. 미반영 부분

우선순위는 추천 순서입니다.

| # | 항목 | 참고 코드 | 내용 | 비고 |
|---|---|---|---|---|
| 1 | **검토 호출** | `ra_review`, `ra_answer.review_*` | 각 주장을 인용 구간만 보고 supported / uncertain / unsupported로 판정. 주장을 절 단위로 나누고, 인용 보충은 보여준 구간 안에서만 허용 | 2회째 호출. 숫자는 맞지만 의미가 틀린 주장(대상군, 시점, 신청자·기관 혼동)을 거르는 장치 |
| 2 | 요청 항목별 응답 구조 | `ra_models.request_items`, `RequestedDraft` | 질문을 문장 단위(R1..)로 나눠 항목마다 claims 또는 gaps를 강제. 뒤쪽 요청이 빠지는 것을 막음 | 일반 규칙이라 옮기기 쉬움 |
| 3 | 평가 | `eval_*` | 정답 세트 대비 답변 채점 | `eval/retrieval/questions.yaml`(검색 20문항)에 기대 답 추가 필요 |
| 4 | 호출 장부와 응답 캐시 | `openai_chat.EvaluationAPIChat` | 요청 해시별 요청·응답 저장, 일일 호출 상한, 실패 요청 자동 재호출 금지, 키 마스킹 | 비용 관리와 재현성 |
| 5 | 같은 시험 문맥 확장 | `ra_parent_context`, `ra_retrieval.context` | 검색된 블록과 같은 시험의 이웃 절을 그룹당 1,200토큰까지 추가 | parent 절로 부족할 때 |
| 6 | 누락 항목 추가 검색 1회 | `ra_research.extend` | 생성 전에 요청 항목별 키워드로 빠진 근거 후보를 한 번 더 검색(LLM 호출 없음) | 키워드 표가 ustekinumab용이라 일반화 필요 |
| 7 | 유형별 수치 검증 | `ra_quantities`, `printed_pk` | 단위, 통계량 종류(평균/SD/중앙값/기하평균), 군 연결 검사 | 현재 숫자 집합 검사를 대체 |
| 8 | 인용 보완 | `ra_citation_completion` | 시험 ID, 대조약, 투여 경로 등의 인용이 빠지면 근거 안에서 자동 추가 | ustekinumab 전용 규칙이 많음 |
| 9 | 주장 분리와 중복 제거 | `ra_assertions` | 여러 사실을 묶은 주장 분리, 같은 내용 병합 | |
| 10 | 시험 ID 충돌 검사 | `ra_requirements.study_conflict` | 주장의 시험 ID와 인용 근거의 시험이 다르면 제외 | 이 프로젝트 청크에 `study_ids`가 있어 옮기기 쉬움 |
| 11 | "부재" 주장 차단 | `claim_basis = absence_of_evidence` 제외 | 일부 검색만으로 "문서에 없다"고 단정하는 주장 제외 | 지금은 프롬프트로만 금지 |
| 12 | 가이던스 "현재 유효" 표현 차단 | `literal_check`의 정규식 | 저장된 상태만으로 현재 유효하다고 쓰는 주장 제외 | 지금은 프롬프트로만 금지 |
| 13 | 질문 의무 항목 | `ra_obligations` | 질문 유형(용량·경로, 대조약, 설계, 브리징 등)별 필수 항목과 탐색 단서 | ustekinumab과 평가 문항에 맞춤. 그대로 옮기지 않는 것이 좋음 |
| 14 | 범위 충돌 처리 | `request_scope` | UI 필터(제품, 기관, 문서)와 질문 범위가 충돌할 때 안내 | UI가 생기면 |
| 15 | 검토 메모 | `ra_memo` | 통과한 주장으로 쟁점 메모 생성(추가 호출 없음) | |
| 16 | 서비스 계층 | `rag_service`, `RAPipeline.ask` | 진행 이벤트, 요청 잠금, 준비(warmup) | |

## 5. 알려진 한계와 확인할 점

**숫자 검사 오탐.** 주장의 숫자가 인용문에 글자 그대로 없으면 제외합니다. 다음 경우 맞는 주장도 빠질 수 있습니다.
- 표기 차이: "80~125%"와 원문 "80.00-125.00%", 원문 "four"와 주장 "4"
- 단계 표기: "1상"과 원문 "Phase I"
- 한국어 날짜: "2024년 4월"과 원문 "April 2024"
- 수 세기: "두 제품", "3개 시험"처럼 원문에 없는 개수

프롬프트에 "숫자를 그대로 복사하라"를 넣었지만, 실제 답변에서 제외 비율을 보고 규칙을 조정해야 합니다(4장 7번 대체). 제외된 주장과 `missing_numbers`는 보고서에 남습니다.

**luna 파라미터는 확인하지 않았습니다.** 참고 코드가 `gpt-5.6-luna`에 보내는 방식(`max_completion_tokens`, `reasoning_effort='none'`, temperature/seed 없음)을 그대로 따랐습니다. 첫 실제 호출에서 다음을 확인해야 합니다.
- strict schema의 `minItems`, `maxItems`, `pattern`, 큰 enum 지원 여부
- 컨텍스트 길이에 맞춘 `RA_LLM_INPUT_TOKENS` 값

**그 밖의 한계**
- **인용 범위:** 표는 행 단위로 인용하고 셀 단위는 지원하지 않습니다. 대신 프롬프트에 머리글 행과 캡션을 함께 고르라고 지시합니다. 긴 문단은 한 줄 전체가 인용 단위입니다.
- **parent 크기:** 절 전체가 근거라서 parent가 크면(최대 약 2,000토큰, `ChunkConfig.parent_max`) 예산 안에 들어가는 블록 수가 줄어듭니다. 이렇게 빠진 블록은 `omitted`에 남습니다.
- **tiktoken:** 첫 사용 때 인코딩 파일을 내려받습니다. 받을 수 없으면 추정값을 쓰고, 보고서의 `token_count_basis`에 그 사실이 남습니다.
- **원제품 이름:** Stelara 같은 원제품 이름은 언급 검사에서 빠집니다. 원제품 자체에 대한 주장은 선언 제품 ⊆ 인용 제품 검사만 받습니다.

## 6. 다음 단계

1. 환경을 준비합니다(torch·openai·tiktoken 설치, DB 지정과 색인 import, `.env`). 그다음 `--dry-run`으로 근거 구성과 토큰 수를 확인합니다.
2. `eval/retrieval/questions.yaml`의 20문항으로 실제 답변을 뽑아 제외 사유 분포(특히 숫자 오탐)와 gaps를 봅니다.
3. 4장 1번(검토 호출)과 2번(요청 항목별 응답 구조)을 붙입니다.
