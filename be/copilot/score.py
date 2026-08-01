"""The determination arithmetic.

The model reports one status per criterion and nothing else. The number and the
band are computed here so the same record always yields the same determination,
and so no coverage judgement is ever carried by generated text.
"""

# Required criteria carry 80 of the 100 points, supporting criteria the other
# 20, matching four-at-twenty plus four-at-five when the policy yields the
# expected shape.
REQUIRED_POOL = 80.0
SUPPORTING_POOL = 20.0

QUALIFIES_FLOOR = 85
NOT_QUALIFIED_CEILING = 60

SCORE_RULE = (
    "Twenty points per required criterion met and five per supporting "
    "criterion, to one hundred. Any required criterion the record contradicts "
    "stops the request before submission. Any required criterion left "
    "undocumented routes the case to specialist review."
)


def _kind(criterion: dict) -> str:
    return str(criterion.get("kind") or "").strip().lower()


def _status(criterion: dict) -> str:
    return str(criterion.get("status") or "").strip().upper()


def score_criteria(criteria: list[dict]) -> tuple[int, str]:
    """Return `(score, band)` for one qualification result.

    An unrecognised `kind` is treated as required: mis-binning a real
    requirement into the cheap pool would let a case reach QUALIFIES on
    criteria nobody checked.

    Case is normalised first, because the model writes these two fields and
    "Supporting" is a known kind spelled differently, not an unknown one. Left
    exact, it would bin a supporting criterion as required, and a supporting
    criterion the record contradicts would then screen the patient out before
    submission, which is the one outcome reserved for required criteria.
    """
    supporting = [c for c in criteria if _kind(c) == "supporting"]
    required = [c for c in criteria if _kind(c) != "supporting"]

    # An empty pool surrenders its points to the other one. Without this a
    # policy that yields only required criteria could never reach 100, so an
    # all-met record would land in review on an arithmetic artefact.
    pools = [(group, pool) for group, pool in
             ((required, REQUIRED_POOL), (supporting, SUPPORTING_POOL)) if group]
    available = sum(pool for _, pool in pools)

    earned = 0.0
    for group, pool in pools:
        met = sum(1 for c in group if _status(c) == "MET")
        earned += (pool * 100 / available) * met / len(group)
    # int(x + 0.5), not round(): round() is half-to-even, so a criterion count
    # that lands on .5 would round down half the time. `earned` is never
    # negative, so truncation and floor agree.
    score = min(100, int(earned + 0.5))

    # No required criterion means nothing load-bearing was checked. Without
    # this the empty pool surrenders its points and `all()` over an empty list
    # is vacuously true, so an unchecked record would arrive at QUALIFIES and
    # arm the Submit gate.
    if not required:
        return score, "NEEDS_REVIEW"
    if any(_status(c) == "NOT_MET" for c in required):
        return score, "NOT_QUALIFIED"
    if score < NOT_QUALIFIED_CEILING:
        return score, "NOT_QUALIFIED"
    if score >= QUALIFIES_FLOOR and all(
            _status(c) == "MET" for c in required):
        return score, "QUALIFIES"
    return score, "NEEDS_REVIEW"
