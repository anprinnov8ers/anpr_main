"""
Plate normalisation and OCR-noise-tolerant matching.

This is the differentiator of the whole platform. Everything else is plumbing.

Two ideas:
  1. Normalise  - strip junk, uppercase. This is what we STORE.
  2. Canonical  - collapse visually-confusable characters into one class.
                  This is what we COMPARE. Never stored, never shown.

A camera reading "MH12A81234" and another reading "MH12AB1234" are the same
vehicle. Exact matching drops the second stop and breaks the trajectory.
Canonical + edit distance keeps it.
"""

import re
from rapidfuzz.distance import Levenshtein

# Characters that OCR confuses on real Indian plates, collapsed to one form.
# Direction is arbitrary (letter -> digit) as long as it is applied to BOTH
# sides of every comparison.
CONFUSIONS = str.maketrans({
    "O": "0",
    "D": "0",
    "Q": "0",
    "I": "1",
    "L": "1",
    "Z": "2",
    "A": "4",
    "S": "5",
    "G": "6",
    "T": "7",
    "B": "8",
})

# Standard Indian format: MH 12 AB 1234
# Also tolerates 1-digit RTO codes and 0-3 series letters (BH-series, older plates).
PLATE_RE = re.compile(r"^[A-Z]{2}[0-9]{1,2}[A-Z]{0,3}[0-9]{4}$")

MIN_PLATE_LEN = 8
MAX_PLATE_LEN = 11


def normalise(raw: str) -> str:
    """Strip separators and whitespace, uppercase. What we store in plate_norm."""
    return re.sub(r"[^A-Za-z0-9]", "", raw or "").upper()


def canonical(s: str) -> str:
    """Collapse confusable characters. Comparison only - never display this."""
    return s.translate(CONFUSIONS)


def is_valid_indian_plate(s: str) -> bool:
    """True if the string matches the standard Indian registration format."""
    return bool(PLATE_RE.match(s))


def is_plausible(s: str) -> bool:
    """
    Looser gate used at ingestion. A read that fails the strict regex may still
    be a real plate with one bad character, so we keep it if the length is sane.
    Rejecting only obvious garbage here keeps recall high; the fuzzy layer
    cleans up afterwards.
    """
    return MIN_PLATE_LEN <= len(s) <= MAX_PLATE_LEN and s.isalnum()


def distance(a: str, b: str) -> int:
    """Edit distance between two plates in canonical space."""
    return Levenshtein.distance(canonical(a), canonical(b))


def fuzzy_match(query: str, candidate: str, max_dist: int = 2) -> bool:
    """
    Same vehicle?

    Length must match exactly. This is the constraint that keeps false positives
    low: we are correcting character SUBSTITUTIONS from OCR, not insertions or
    deletions. A plate that lost a character is a different failure mode and we
    would rather miss it than match the wrong car.
    """
    q, c = normalise(query), normalise(candidate)
    if len(q) != len(c):
        return False
    return distance(q, c) <= max_dist


def match_score(query: str, candidate: str) -> float:
    """0.0 - 1.0 confidence that two reads are the same plate. For ranking."""
    q, c = normalise(query), normalise(candidate)
    if not q or len(q) != len(c):
        return 0.0
    return 1.0 - (distance(q, c) / len(q))


if __name__ == "__main__":
    # Quick self-check. Run: python normalise.py
    cases = [
        ("MH12AB1234", "MH12A81234", True,  "B/8 misread - the demo case"),
        ("KA05MJ2345", "KA05MJ2345", True,  "identical"),
        ("KA05MJ2345", "KAO5MJ2345", True,  "0/O misread"),
        ("KA01AB1111", "KA01AB1112", True,  "one real digit off - within threshold"),
        ("KA01AB1111", "KA01AB2222", False, "four digits off - different car"),
        ("KA05MJ2345", "KA05MJ234",  False, "different length - rejected"),
        ("DL3CAB5678", "DL3CA85678", True,  "B/8 on a Delhi plate"),
    ]
    ok = True
    for q, c, expected, why in cases:
        got = fuzzy_match(q, c)
        flag = "PASS" if got == expected else "FAIL"
        if got != expected:
            ok = False
        print(f"{flag}  {q} vs {c}  ->  {got}  (score {match_score(q, c):.2f})  # {why}")
    print("\nAll good." if ok else "\nSomething is wrong - fix before building on this.")
