"""Map free-text commodity descriptions to a class, and look up class facts."""

from __future__ import annotations

import math
import re
from typing import Any

from . import config


def classes() -> dict[str, dict[str, Any]]:
    return config.commodities()["classes"]


def vague_phrases() -> list[str]:
    return [p.lower() for p in config.commodities()["vague"]["phrases"]]


def is_vague(text: str) -> bool:
    t = (text or "").strip().lower()
    return any(t == p or t.startswith(p) for p in vague_phrases())


def _keyword_re(keyword: str) -> re.Pattern[str]:
    """Whole word (or phrase), plural allowed; a trailing * means 'word starting with'."""
    k = keyword.lower().strip()
    if k.endswith("*"):
        return re.compile(r"\b" + re.escape(k[:-1]))
    return re.compile(r"\b" + re.escape(k) + r"(?:s|es)?\b")


def classify(text: str) -> str:
    """Pick a class from the description. Vague text falls through to general cargo."""
    t = (text or "").lower()
    if is_vague(t):
        return config.commodities()["vague"]["class"]
    for name, spec in classes().items():
        if any(_keyword_re(k).search(t) for k in spec.get("keywords") or []):
            return name
    return "general_cargo"


def info(cls: str) -> dict[str, Any]:
    return classes().get(cls) or classes()["general_cargo"]


def label(cls: str) -> str:
    return info(cls)["label"]


def typical_value_per_kg(cls: str) -> float:
    lo, hi = info(cls)["value_per_kg"]
    return math.sqrt(lo * hi)  # geometric mean of the plausible range


def is_high_value(cls: str, text: str = "") -> bool:
    return bool(info(cls).get("high_value"))


def payment_label(method: str) -> str:
    return method.replace("_", " ").capitalize()


def payment_phrase(method: str) -> str:
    """How the payment reads after the word 'paid': 'in cash', 'by a third party'."""
    return config.commodities()["payment_methods"].get(method, "by " + method.replace("_", " "))
