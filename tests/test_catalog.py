from __future__ import annotations

from datetime import date

import httpx

from acadadviser.catalog import Catalog, extract_facts, registrable_domain

PROFILE = {
    "identity": {"program_name_short": "MCDS"},
    "admissions": {
        "tests": {"gre": {"policy": "required"}},
        "english_proficiency": {"minimum_scores": {"toefl": 100}},
        "academic_requirements": {"minimum_gpa": 3.0},
        "deadlines": [{"date": "2020-01-01"}, {"date": "2026-12-11"}, {"date": "2027-01-15"}],
    },
    "program_details": {"duration": {"raw_text": "16 months"}},
    "cost": {"tuition": {"amount": 60300.0, "currency": "USD", "basis": "unknown"}},
    "observed_admission_data": {"gpa": {"median": 3.95, "sample_size": 100, "sample_flag": "SOURCE_REPORTED_MEDIAN"}},
}
ROWS = [
    {"program_id": "cmu:ms:mcds", "domain": "cmu.edu", "degree_level": "ms",
     "display_name": "Master of Computational Data Science", "profile": PROFILE},
    {"program_id": "cmu:phd:ml", "domain": "cmu.edu", "degree_level": "phd",
     "display_name": "Machine Learning", "profile": PROFILE},
    {"program_id": "cmu:ms:ml", "domain": "cmu.edu", "degree_level": "ms",
     "display_name": "Machine Learning", "profile": {}},
]


def catalog(status=200):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if status != 200:
            return httpx.Response(status)
        level = request.url.params["degree_level"]
        return httpx.Response(200, json=[r for r in ROWS if r["degree_level"] == level])

    return Catalog("http://sip", transport=httpx.MockTransport(handler)), calls


def item(**fields):
    return {"official_url": "https://mcds.cs.cmu.edu/", "display_name": "", "degree_level": "ms", **fields}


def test_registrable_domain():
    assert registrable_domain("https://mcds.cs.cmu.edu/apply") == "cmu.edu"
    assert registrable_domain("not a url") == ""


def test_facts_are_extracted_and_past_deadlines_dropped():
    facts = extract_facts(PROFILE, today=date(2026, 11, 2))
    assert facts["duration"] == "16 months"
    assert facts["next_deadline"] == "2026-12-11"
    assert facts["gre_policy"] == "required"
    assert facts["tuition"]["amount"] == 60300.0
    assert facts["community_gpa"] == {"median": 3.95, "sample_size": 100}


def test_empty_and_small_sample_values_stay_absent():
    facts = extract_facts({
        "admissions": {"tests": {"gre": {"policy": "unknown"}}},
        "observed_admission_data": {"gpa": {"median": 3.9, "sample_flag": "INSUFFICIENT_SAMPLE"}},
    })
    assert facts == {}


def test_matches_same_domain_and_name():
    source, _ = catalog()
    row = source.enrich([item(display_name="Master of Computational Data Science (MCDS)")])[0]
    assert row["catalog"]["duration"] == "16 months"


def test_short_name_matches():
    source, _ = catalog()
    assert source.enrich([item(display_name="MCDS")])[0]["catalog"]


def test_same_name_on_another_domain_does_not_match():
    source, _ = catalog()
    row = source.enrich([item(official_url="https://other.edu/x", display_name="Computational Data Science")])[0]
    assert row["catalog"] is None


def test_degree_conflict_does_not_match():
    source, _ = catalog()
    row = source.enrich([item(display_name="Master of Computational Data Science", degree_level="PhD")])[0]
    assert row["catalog"] is None


def test_loose_name_does_not_match():
    source, _ = catalog()
    assert source.enrich([item(display_name="Data Science")])[0]["catalog"] is None


def test_catalog_outage_returns_items_without_facts():
    source, _ = catalog(status=503)
    assert source.enrich([item(display_name="MCDS")])[0]["catalog"] is None


def test_catalog_is_cached_between_lists():
    source, calls = catalog()
    source.enrich([item(display_name="MCDS")])
    count = len(calls)
    source.enrich([item(display_name="MCDS")])
    assert len(calls) == count


def test_field_level_rows_only_yield_community_data():
    facts = extract_facts(PROFILE, program_specific=False)
    assert facts == {"community_gpa": {"median": 3.95, "sample_size": 100}}


def test_field_level_row_is_detected_from_its_id():
    row = {"program_id": "nyu:ms:field:data_science", "domain": "nyu.edu", "degree_level": "ms",
           "display_name": "Data Science Analytics", "profile": PROFILE}
    source = Catalog("http://sip", transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=[row] if request.url.params["degree_level"] == "ms" else [])
    ))
    enriched = source.enrich([{"official_url": "https://cds.nyu.edu/", "display_name": "Data Science Analytics"}])
    assert set(enriched[0]["catalog"]) == {"community_gpa"}


def test_boilerplate_duration_and_weak_gre_are_hidden():
    facts = extract_facts({
        "program_details": {"duration": {"raw_text": "typically 1-2 years"}},
        "admissions": {"tests": {"gre": {"policy": "not_considered"}}},
    })
    assert facts == {}
