"""Answer generation (ra_answer): evidence packing, segment IDs, the structured contract, the
checks in code, rendering, and the OpenAI payload. A scripted client stands in for the model,
so these run without an API key or the index."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from ra_answer import evidence as ev
from ra_answer.generate import ENUM_LIMIT, answer, check, messages, numbers, schema
from ra_answer.llm import LLMSettings, OpenAIChat, Tokens


def _chunk(cid, doc, parent, *, program="SB17", agency="ema", kind="narrative", speaker="regulator",
           needs_review=False, doc_type="assessment_report", pages=(12,), section=("2.6 Clinical aspects", "PK")):
    return SimpleNamespace(id=cid, index_document_id=doc * 10, document_id=doc, parent_key=parent, kind=kind,
                           agency=agency, program_id=program, doc_kind="guidance" if program is None else "product",
                           doc_type=doc_type, pages=list(pages), section_path=list(section), speaker=speaker,
                           needs_review=needs_review, table_caption=None, decision_date=None,
                           guidance_status="final" if program is None else None, guidance_effective_from=None,
                           guidance_effective_to=None)


def _hit(chunk, text, product, score=0.5, pages=None):
    return SimpleNamespace(chunk=chunk, score=score, parent_text=text, product=product,
                           parent_pages=list(pages or chunk.pages))


SB17_PK = ("The PK similarity study SB17-1101 enrolled healthy subjects.\n"
           "The 90% CI of the geometric mean ratio for AUCinf was 0.95 to 1.08, within 80.00-125.00%.")
SB17_TABLE = ("Table 3 PK parameters\n| Parameter | SB17 | EU-Stelara |\n| --- | --- | --- |\n"
              "| Cmax (ug/mL) | 14.1 | 14.4 |")
CTP43_PK = "CT-P43 study 3.1 met the equivalence margin of 80-125% for AUC and Cmax."
GUIDANCE = "A comparative efficacy study may not be needed when analytical and PK data suffice."


def _hits():
    return [_hit(_chunk(1, 1, "p1"), SB17_PK, "SB17", 0.9),
            _hit(_chunk(2, 1, "p1"), SB17_PK, "SB17", 0.7),            # same parent as hit 1
            _hit(_chunk(3, 1, "t1", kind="row"), SB17_TABLE, "SB17", 0.6),
            _hit(_chunk(4, 2, "p9", program="CT-P43", agency="fda", doc_type="review"), CTP43_PK, "CT-P43", 0.8),
            _hit(_chunk(5, 3, "g1", program=None, agency="fda", doc_type="guidance"), GUIDANCE, None, 0.4)]


class Scripted:
    """Counts tokens like OpenAIChat's estimate and replies with a fixed JSON."""
    model = "scripted"

    def __init__(self, reply: dict | None = None, input_tokens=100_000):
        self.reply, self.input_tokens = reply, input_tokens
        self.tokens, self.calls, self.sent = Tokens.__new__(Tokens), [], []
        self.tokens._enc, self.tokens.basis = None, "estimate"

    def count(self, text):
        return self.tokens.count(text)

    def count_messages(self, msgs):
        return sum(self.count(m["content"]) + 4 for m in msgs) + 8

    def __call__(self, msgs, json_schema=None):
        self.sent.append((msgs, json_schema))
        self.calls.append({"schema": json_schema["name"]})
        return {"text": json.dumps(self.reply), "prompt_tokens": 1000, "completion_tokens": 100}


def _ids(blocks):
    return {s.text: s.id for b in blocks for s in b.segments if s.id}


def test_blocks_one_per_parent_and_segments_keep_source():
    blocks = ev.number(ev.from_hits(_hits(), {1: "Pyzchiva EPAR"}))
    assert [b.chunk_ids for b in blocks] == [[1, 2], [3], [4], [5]]
    assert blocks[0].title == "Pyzchiva EPAR" and blocks[0].score == 0.9
    assert blocks[1].kind == "table"
    for b in blocks:
        for s in b.segments:
            assert b.text[s.start:s.end] == s.text
    rule = [s for s in blocks[1].segments if s.text.startswith("| ---")]
    assert rule and rule[0].id is None  # table rule: readable, not citable
    ids = [s.id for b in blocks for s in b.segments if s.id]
    assert ids == [f"S{i}" for i in range(1, len(ids) + 1)]


def test_pack_round_robin_and_whole_blocks():
    blocks = ev.from_hits(_hits())
    order = []
    admitted, omitted = ev.pack(blocks, lambda cand: order.append(cand[-1].chunk_ids[0]) or True)
    assert order == [1, 4, 5, 3]  # first block of each product before a second SB17 block
    assert [b.id for b in admitted] == ["E1", "E2", "E3", "E4"] and not omitted
    limited, dropped = ev.pack(ev.from_hits(_hits()), lambda cand: sum(len(b.text) for b in cand) <= 250)
    assert all(b.text in (SB17_PK, SB17_TABLE, CTP43_PK, GUIDANCE) for b in limited)  # never clipped
    assert dropped and len(limited) + len(dropped) == 4


def test_schema_is_strict_and_enumerates_segments():
    blocks = ev.number(ev.from_hits(_hits()))
    s = schema(blocks)
    claim = s["schema"]["properties"]["claims"]["items"]
    assert list(claim["properties"])[0] == "citation_span_ids"
    assert claim["additionalProperties"] is False and set(claim["required"]) == set(claim["properties"])
    assert claim["properties"]["citation_span_ids"]["items"]["enum"] == [
        x.id for b in blocks for x in b.segments if x.id]
    assert claim["properties"]["products"]["items"]["enum"] == ["CT-P43", "SB17"]
    big = schema(blocks, enum_limit=2)
    assert big["schema"]["properties"]["claims"]["items"]["properties"]["citation_span_ids"]["items"] == {
        "type": "string", "pattern": "^S[0-9]+$"}
    guidance_only = schema(ev.number(ev.from_hits(_hits()[4:])))
    assert guidance_only["schema"]["properties"]["claims"]["items"]["properties"]["products"]["maxItems"] == 0
    assert ENUM_LIMIT == 500


def test_messages_carry_segments_and_document_fields():
    blocks = ev.number(ev.from_hits(_hits(), {1: "Pyzchiva EPAR"}))
    data = json.loads(messages("SB17 PK?", blocks, {"products": ["SB17"]})[1]["content"])
    assert data["documents_not_source_text"]["D1"]["title"] == "Pyzchiva EPAR"
    first = data["sources"][0]
    assert first["id"] == "E1" and first["document"] == "D1" and first["speakers"] == ["regulator"]
    assert first["reading_segments"][0] == ["S1", SB17_PK.split("\n")[0]]


def test_numbers_as_printed():
    assert numbers("90% CI 0.95–1.08, 1,000 subjects") == {"90", "0.95", "1.08", "1000"}
    assert "17" not in numbers("SB17")  # a code, not a value


def _claim(text, ids, products=("SB17",), kind="source_fact"):
    return {"citation_span_ids": ids, "text": text, "kind": kind, "products": list(products)}


def test_check_accepts_supported_and_rejects_with_reasons():
    blocks = ev.number(ev.from_hits(_hits()))
    i = _ids(blocks)
    pk, ct, gd = i[SB17_PK.split("\n")[1]], i[CTP43_PK], i[GUIDANCE]
    names = {"SB17": ["SB17", "Pyzchiva"], "CT-P43": ["CT-P43", "Steqeyma"]}
    raw = {"claims": [
        _claim("SB17의 AUCinf 기하평균비 90% CI는 0.95~1.08로 80.00-125.00% 범위 안이었다.", [pk]),
        _claim("SB17의 AUCinf 90% CI는 0.93~1.08이었다.", [pk]),                       # 0.93 not in source
        _claim("CT-P43은 동등성 기준 80-125%를 충족했다.", [ct], products=["SB17"]),     # wrong product
        _claim("SB17과 CT-P43 모두 기준을 충족했다.", [pk]),                            # CT-P43 undeclared
        _claim("CT-P43 결과", ["S999"], products=["CT-P43"]),                           # unknown ID
        _claim("비교 효능 시험이 필요 없을 수 있다.", [gd], products=[], kind="regulator_position"),
        _claim("SB17 결과", [pk], products=[]),                                         # product scope missing
    ], "gaps": ["면역원성 결과", " "]}
    accepted, rejected, gaps = check(raw, blocks, names)
    assert [c["id"] for c in accepted] == ["C1", "C6"]
    assert accepted[0]["sources"][0]["quote"] == SB17_PK.split("\n")[1]
    assert accepted[0]["sources"][0]["block"] == "E1"
    assert {c["id"]: c["reason"] for c in rejected} == {
        "C2": "number_not_in_cited_sources", "C3": "product_not_in_cited_sources",
        "C4": "named_product_not_declared", "C5": "unknown_span_id", "C7": "product_scope_missing"}
    assert rejected[0]["missing_numbers"] == ["0.93"]
    assert gaps == ["면역원성 결과"]


def test_check_rejects_bad_shape():
    with pytest.raises(ValueError):
        check({"claims": "x"}, [])
    accepted, rejected, _ = check({"claims": [{"text": "x"}], "gaps": []}, [])
    assert not accepted and rejected[0]["reason"] == "invalid_claim_shape"


def test_answer_renders_checked_claims_only():
    blocks, _ = ev.pack(ev.from_hits(_hits()), lambda cand: True)  # the numbering answer() sends
    i = _ids(blocks)
    reply = {"claims": [
        _claim("SB17 시험 SB17-1101은 건강한 피험자를 대상으로 했다.", [i[SB17_PK.split("\n")[0]]]),
        _claim("Cmax는 SB17 14.1, EU-Stelara 14.4 ug/mL였다.", [i["| Cmax (ug/mL) | 14.1 | 14.4 |"],
                                                              i["| Parameter | SB17 | EU-Stelara |"]]),
        _claim("SB17은 99명이 참여했다.", [i[SB17_PK.split("\n")[0]]])], "gaps": []}
    client = Scripted(reply)
    r = answer("SB17의 PK 결과는?", _hits(), client, titles={1: "Pyzchiva EPAR"},
               names={"SB17": ["SB17"], "CT-P43": ["CT-P43"]})
    assert len(client.sent) == 1 and client.sent[0][1]["name"] == "ra_grounded_answer"
    assert r["status"] == "partial"  # one claim rejected
    assert [c["id"] for c in r["claims"]] == ["C1", "C2"] and r["rejected"][0]["reason"] == "number_not_in_cited_sources"
    assert "99명" not in r["answer_ko"]
    assert "- SB17 시험 SB17-1101은 건강한 피험자를 대상으로 했다. [E1]" in r["answer_ko"]
    assert '[E1] EMA · SB17 · assessment_report · "Pyzchiva EPAR" · p.12' in r["answer_ko"]
    assert r["usage"] == {"prompt_tokens": 1000, "completion_tokens": 100}
    assert "[E4] EMA · SB17 · assessment_report" in r["answer_ko"]  # the table block, cited by C2
    assert len(r["evidence"]) == 4 and r["evidence"][0]["text"] == SB17_PK


def test_answer_without_hits_or_in_dry_run_calls_no_model():
    client = Scripted({"claims": [], "gaps": []})
    assert answer("질문", [], client)["status"] == "not_found"
    r = answer("SB17?", _hits(), client, dry_run=True)
    assert r["status"] == "dry_run" and r["estimated_prompt_tokens"] > 0 and r["schema"]["name"]
    assert not client.sent


def test_answer_budget_omits_whole_blocks():
    client = Scripted({"claims": [], "gaps": []}, input_tokens=1)
    r = answer("SB17?", _hits(), client)
    assert r["status"] == "not_found" and len(r["omitted"]) == 4 and not client.sent


def test_answer_reports_model_error():
    class Broken(Scripted):
        def __call__(self, msgs, json_schema=None):
            raise RuntimeError("incomplete reply (finish_reason=length)")
    r = answer("SB17?", _hits(), Broken())
    assert r["status"] == "error" and "length" in r["error"]


def test_needs_review_is_marked():
    hits = [_hit(_chunk(1, 1, "p1", needs_review=True), SB17_PK, "SB17")]
    blocks = ev.number(ev.from_hits(hits))
    sid = _ids(blocks)[SB17_PK.split("\n")[0]]
    r = answer("SB17?", hits, Scripted({"claims": [_claim("SB17 시험은 건강한 피험자 대상이었다.", [sid])],
                                        "gaps": []}))
    assert r["claims"][0]["needs_review"] and "(검토 필요 원문)" in r["answer_ko"]


def _completion(content="{}", finish="stop", refusal=None):
    msg = SimpleNamespace(content=content, refusal=refusal)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason=finish)], model="gpt-5.6-luna-snap",
                           usage=SimpleNamespace(prompt_tokens=10, completion_tokens=2), system_fingerprint="fp")


class FakeSDK:
    def __init__(self, reply):
        self.payloads, self.reply = [], reply
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **payload):
        self.payloads.append(payload)
        return self.reply


def test_openai_payload_and_reply():
    sdk = FakeSDK(_completion('{"claims":[],"gaps":[]}'))
    chat = OpenAIChat(LLMSettings(api_key="k"), sdk=sdk)
    out = chat([{"role": "user", "content": "x"}], {"name": "n", "schema": {"type": "object"}})
    p = sdk.payloads[0]
    assert p["model"] == "gpt-5.6-luna" and p["reasoning_effort"] == "none" and p["max_completion_tokens"] == 4000
    assert p["response_format"] == {"type": "json_schema",
                                    "json_schema": {"name": "n", "schema": {"type": "object"}, "strict": True}}
    assert "temperature" not in p
    assert out["text"] == '{"claims":[],"gaps":[]}' and out["model_reported"] == "gpt-5.6-luna-snap"
    assert chat.calls[0]["status"] == "completed" and chat.calls[0]["prompt_tokens"] == 10
    plain = FakeSDK(_completion("hi"))
    OpenAIChat(LLMSettings(api_key="k", model="gpt-4o-mini", reasoning_effort=None), sdk=plain)(
        [{"role": "user", "content": "x"}])
    assert "reasoning_effort" not in plain.payloads[0] and "response_format" not in plain.payloads[0]


@pytest.mark.parametrize("reply,msg", [(_completion("{", finish="length"), "RA_LLM_MAX_OUTPUT"),
                                       (_completion(None, refusal="no"), "refused")])
def test_openai_incomplete_or_refused(reply, msg):
    chat = OpenAIChat(LLMSettings(api_key="k"), sdk=FakeSDK(reply))
    with pytest.raises(RuntimeError, match=msg):
        chat([{"role": "user", "content": "x"}])


def test_settings_from_env(monkeypatch, tmp_path):
    monkeypatch.setattr("ra_answer.llm.load_env", lambda: None)
    for k in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "RA_LLM_MODEL", "RA_LLM_MAX_OUTPUT", "RA_LLM_INPUT_TOKENS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("RA_LLM_REASONING_EFFORT", "")
    monkeypatch.setenv("RA_LLM_INPUT_TOKENS", "50000")
    s = LLMSettings.from_env()
    assert s.model == "gpt-5.6-luna" and s.reasoning_effort is None and s.input_tokens == 50000
    with pytest.raises(SystemExit, match="OPENAI_API_KEY"):
        OpenAIChat(s).sdk
