"""ClinicalTrials.gov API v2: development code -> registered trials (NCT ID, phase,
conditions, primary outcomes). Registration data, not regulator judgement."""
from __future__ import annotations

from typing import Iterable

from ..http import FetchError, Fetcher
from ..models import TrialRecord
from ..registry import Program
from ..util import norm_name

API = "https://clinicaltrials.gov/api/v2/studies"


def trials_for_program(fetcher: Fetcher, program: Program, report) -> Iterable[TrialRecord]:
    seen: set[str] = set()
    for code in program.codes or [program.id]:
        try:
            data = fetcher.get(API, params={"query.term": code, "pageSize": 50}).json()
        except FetchError as e:
            report("error", "source_failed", f"ClinicalTrials.gov 검색 실패({code}): {e.reason}",
                   agency="ctgov", program_id=program.id)
            return
        for st in data.get("studies", []):
            ps = st.get("protocolSection", {})
            ident = ps.get("identificationModule", {})
            nct = ident.get("nctId")
            if not nct or nct in seen:
                continue
            text = " ".join([ident.get("briefTitle", ""), ident.get("officialTitle", ""),
                             " ".join(o.get("id", "") for o in ident.get("secondaryIdInfos", [])),
                             (ident.get("orgStudyIdInfo") or {}).get("id", "")])
            # the free-text search is broad; keep only studies that actually mention a program code
            if not any(norm_name(c) in norm_name(text) for c in (program.codes or [program.id])):
                continue
            seen.add(nct)
            yield TrialRecord(
                nct_id=nct, program_id=program.id, title=ident.get("briefTitle"),
                phase=",".join(ps.get("designModule", {}).get("phases", [])) or None,
                conditions=ps.get("conditionsModule", {}).get("conditions", []),
                primary_outcomes=[o.get("measure", "") for o in
                                  ps.get("outcomesModule", {}).get("primaryOutcomes", [])],
                sponsor=ps.get("sponsorCollaboratorsModule", {}).get("leadSponsor", {}).get("name"),
                raw={"org_study_id": (ident.get("orgStudyIdInfo") or {}).get("id")},
            )
