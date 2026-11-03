"""Attach SchoolInfoProcessor facts to web results.

Conservative on purpose: a program only gets facts when its official URL is on the
catalog university's domain and its name matches. No match shows no facts.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from datetime import date
from typing import Any
from urllib.parse import urlsplit

import httpx

_log = logging.getLogger("acadadviser.catalog")

_DEGREE_LEVELS = ("ms", "phd", "meng", "ma", "mba", "mfa", "mph", "med", "edd", "psyd", "jd", "llm", "other")
_REFRESH_SECONDS = 3600
_RETRY_SECONDS = 60
_MIN_NAME_OVERLAP = 0.8
_NAME_NOISE = {
    "a", "an", "and", "at", "degree", "for", "graduate", "in", "m", "ma", "master", "masters",
    "mba", "meng", "ms", "msc", "of", "online", "phd", "program", "programme", "s", "science",
    "the", "university",
}


def registrable_domain(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return ".".join(host.split(".")[-2:])


def _degree(value: Any) -> str | None:
    text = re.sub(r"[^a-z]", "", str(value or "").lower())
    if not text:
        return None
    if text.startswith(("phd", "doctor")):
        return "phd"
    if text.startswith(("meng", "masterofengineering")):
        return "meng"
    if text.startswith("mba"):
        return "mba"
    if text.startswith(("ms", "master", "ma")):
        return "master"
    return None


def _tokens(name: Any) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", str(name or "").lower())) - _NAME_NOISE


def _overlap(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def extract_facts(
    profile: dict[str, Any], *, program_specific: bool = True, today: date | None = None
) -> dict[str, Any]:
    """Field-level rows (`:field:` ids) pool a whole subject, and their official-looking
    values can come from another degree's page, so they only yield community data.
    """
    today = today or date.today()
    admissions = profile.get("admissions") or {}
    details = profile.get("program_details") or {}
    facts: dict[str, Any] = {}

    gpa = (profile.get("observed_admission_data") or {}).get("gpa") or {}
    if gpa.get("median") and gpa.get("sample_flag") != "INSUFFICIENT_SAMPLE":
        facts["community_gpa"] = {"median": gpa["median"], "sample_size": gpa.get("sample_size")}
    if not program_specific:
        return facts

    duration = str((details.get("duration") or {}).get("raw_text") or "")
    # "typically 1-2 years" is a catalog-wide default, not a program fact.
    if duration and not duration.lower().startswith("typically"):
        facts["duration"] = duration
    tuition = (profile.get("cost") or {}).get("tuition") or {}
    if tuition.get("amount"):
        facts["tuition"] = {
            "amount": tuition["amount"],
            "currency": tuition.get("currency") or "USD",
            "basis": tuition.get("basis"),
        }
    minimum_gpa = (admissions.get("academic_requirements") or {}).get("minimum_gpa")
    if minimum_gpa:
        facts["minimum_gpa"] = minimum_gpa
    gre_policy = ((admissions.get("tests") or {}).get("gre") or {}).get("policy")
    # not_considered is a weak crawler signal; SIP itself lets any other source override it.
    if gre_policy and gre_policy not in {"unknown", "not_considered"}:
        facts["gre_policy"] = gre_policy
    english = (admissions.get("english_proficiency") or {}).get("minimum_scores") or {}
    if english:
        facts["english_minimums"] = english
    deadlines = sorted(
        row["date"]
        for row in admissions.get("deadlines") or []
        if row.get("date") and str(row["date"]) >= today.isoformat()
    )
    if deadlines:
        facts["next_deadline"] = deadlines[0]
    return facts


class Catalog:
    def __init__(self, base_url: str, *, transport: httpx.BaseTransport | None = None):
        self._base_url = base_url.rstrip("/")
        self._transport = transport
        self._lock = threading.Lock()
        self._by_domain: dict[str, list[dict[str, Any]]] = {}
        self._next_load = 0.0

    def _load(self) -> None:
        by_domain: dict[str, list[dict[str, Any]]] = {}
        with httpx.Client(timeout=60.0, transport=self._transport) as client:
            for level in _DEGREE_LEVELS:
                response = client.get(
                    f"{self._base_url}/v1/programs", params={"degree_level": level, "limit": 2000}
                )
                response.raise_for_status()
                for row in response.json():
                    profile = row.get("profile") or {}
                    identity = profile.get("identity") or {}
                    display = _tokens(row.get("display_name"))
                    short = _tokens(identity.get("program_name_short"))
                    by_domain.setdefault(row.get("domain") or "", []).append(
                        {
                            "program_id": row.get("program_id"),
                            "degree": _degree(row.get("degree_level")),
                            # Web results often combine them: "Data Science (MSDS)".
                            "names": [display, short, display | short],
                            "profile": {
                                key: profile.get(key)
                                for key in ("admissions", "program_details", "cost", "observed_admission_data")
                            },
                        }
                    )
        self._by_domain = by_domain

    def _refresh(self) -> None:
        with self._lock:
            if time.monotonic() < self._next_load:
                return
            try:
                self._load()
                self._next_load = time.monotonic() + _REFRESH_SECONDS
            except (httpx.HTTPError, ValueError) as exc:
                self._next_load = time.monotonic() + _RETRY_SECONDS
                _log.warning("catalog load failed: %s", type(exc).__name__)

    def match(self, item: dict[str, Any]) -> dict[str, Any] | None:
        candidates = self._by_domain.get(registrable_domain(str(item.get("official_url") or "")))
        if not candidates:
            return None
        wanted = _tokens(item.get("display_name"))
        degree = _degree(item.get("degree_level")) or _degree(item.get("display_name"))
        scored = []
        for row in candidates:
            if degree and row["degree"] and degree != row["degree"]:
                continue
            score = max(_overlap(wanted, names) for names in row["names"])
            if score >= _MIN_NAME_OVERLAP:
                scored.append((score, row))
        if not scored:
            return None
        scored.sort(key=lambda pair: pair[0], reverse=True)
        if len(scored) > 1 and scored[0][0] == scored[1][0]:
            return None
        return scored[0][1]

    def enrich(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        self._refresh()
        enriched = []
        for item in items:
            row = self.match(item)
            facts = (
                extract_facts(row["profile"], program_specific=":field:" not in str(row["program_id"]))
                if row else {}
            )
            enriched.append({**item, "catalog": facts or None})
        return enriched
