"""Shared candidate-relevance prune (issue #126).

Title-only sources (mangadot, mangaball, comix) run an expensive per-candidate
chapter fan-out on the full ``_DEFAULT_*_CANDIDATES`` list even when only ONE
candidate is the series the user asked for. For comix each wasted candidate is a
7-18s browser navigation — the single biggest contributor to brushing the 30s
fan-out timeout (#101).

:func:`prune_candidates` is the ONE shared gate that narrows the candidate list
*before* the fan-out. It is intentionally conservative — issue #126's rule is
"when in doubt, fan out": a missed match (returning fewer candidates than needed
and dropping the series the user wanted) is worse than a few wasted lookups. So
the helper only narrows to a single candidate when the query is *unambiguous*
(an exact normalized-title match, or a clearly dominant top score); otherwise it
falls back to the historic ``candidates[:cap]`` behavior byte-for-byte.

All three source candidate lists are ALREADY relevance-sorted upstream, so the
ordering is trusted and the scoring here is cheap (stdlib only — no new
dependency).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from difflib import SequenceMatcher

# --- Tunable knobs (NOT inline magic numbers) --------------------------------
#
# The gate is deliberately conservative (#126: "fan out when in doubt"). A
# missed match (dropping the series the user wanted) is worse than a few wasted
# per-candidate lookups, so both thresholds err toward fanning out.

#: Minimum lead the top candidate's score must hold over the next-best score for
#: the gate to treat the query as unambiguous and prune to that single
#: candidate. Scores are similarity ratios in ``[0.0, 1.0]``; a 0.40 gap means
#: the runner-up is substantially less similar to the query than the leader.
#: Raising it makes the gate MORE conservative (fans out more often); lowering
#: it prunes more aggressively. Tune here, never at a call site.
_RELEVANCE_GAP_THRESHOLD = 0.40

#: Floor the top candidate's own score must clear before the dominant-gap branch
#: may fire. Guards against pruning to a single weak match merely because every
#: other candidate is even weaker (e.g. a typo query where nothing really
#: matches — better to fan out than commit to a poor leader).
_DOMINANT_MIN_SCORE = 0.60

_PUNCT_RE = re.compile(r"[^\w\s]", flags=re.UNICODE)
_WS_RE = re.compile(r"\s+")


def _normalize(title: str | None) -> str:
    """Casefold + strip most punctuation + collapse internal whitespace.

    ``"The Forgotten Field!"`` and ``"the forgotten   field"`` both normalize to
    ``"the forgotten field"``. An empty/``None`` title normalizes to ``""`` so it
    can never win the exact-match branch.
    """
    if not title:
        return ""
    stripped = _PUNCT_RE.sub(" ", title)
    return _WS_RE.sub(" ", stripped.casefold()).strip()


def _score(normalized_title: str, normalized_query: str) -> float:
    """Similarity of a normalized title to the normalized query in ``[0, 1]``.

    Ordering mirrors issue #126: normalized-exact (1.0) > prefix/containment >
    token-set overlap > fuzzy ratio. An empty title scores 0.0 and never wins.
    """
    if not normalized_title or not normalized_query:
        return 0.0
    if normalized_title == normalized_query:
        return 1.0
    if normalized_title.startswith(normalized_query) or normalized_query.startswith(
        normalized_title
    ):
        return 0.90
    title_tokens = set(normalized_title.split())
    query_tokens = set(normalized_query.split())
    if title_tokens and query_tokens:
        union = title_tokens | query_tokens
        token_set_ratio = len(title_tokens & query_tokens) / len(union)
    else:
        token_set_ratio = 0.0
    fuzzy_ratio = SequenceMatcher(None, normalized_title, normalized_query).ratio()
    return max(token_set_ratio, fuzzy_ratio)


def prune_candidates[T](
    candidates: Sequence[T],
    query: str,
    *,
    key: Callable[[T], str | None],
    cap: int,
) -> list[T]:
    """Narrow a relevance-sorted candidate list before the chapter fan-out.

    Returns a sub-list of the SAME items in the SAME (relevance-sorted) order:

    * **Exact match** — when exactly ONE candidate's normalized title equals the
      normalized query, return only that candidate (length 1).
    * **Dominant score** — when no single exact match applies but the top
      candidate's score clears :data:`_DOMINANT_MIN_SCORE` AND leads the
      next-best by more than :data:`_RELEVANCE_GAP_THRESHOLD`, return only the
      top candidate.
    * **Conservative fallback** — otherwise return ``candidates[:cap]``
      unchanged, byte-identical to the historic per-source slice (#126: fan out
      when in doubt).

    Empty and single-candidate lists pass through unchanged. A candidate whose
    ``key(item)`` is ``None``/empty scores zero and never wins any prune branch.

    :param candidates: relevance-sorted candidate items (highest first).
    :param query: the user's raw search query.
    :param key: extracts a candidate's title (may return ``None``).
    :param cap: the existing per-source candidate ceiling for the fan-out.
    """
    if len(candidates) <= 1:
        return list(candidates)

    normalized_query = _normalize(query)
    if not normalized_query:
        return list(candidates[:cap])

    normalized_titles = [_normalize(key(item)) for item in candidates]

    # Exact-match branch: prune ONLY when exactly one candidate matches exactly
    # (two exact matches is genuine ambiguity — fall through to fan out).
    exact_indices = [
        i for i, title in enumerate(normalized_titles) if title == normalized_query
    ]
    if len(exact_indices) == 1:
        return [candidates[exact_indices[0]]]

    # Dominant-score branch: the relevance ordering is trusted, so the leader is
    # candidates[0]. Compare its score against the best of the remainder.
    scores = [_score(title, normalized_query) for title in normalized_titles]
    top_score = scores[0]
    runner_up_score = max(scores[1:])
    if (
        top_score >= _DOMINANT_MIN_SCORE
        and top_score - runner_up_score > _RELEVANCE_GAP_THRESHOLD
    ):
        return [candidates[0]]

    return list(candidates[:cap])
