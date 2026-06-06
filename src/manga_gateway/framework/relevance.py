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

Each candidate is scored over its MAIN *or* any ALTERNATE title (issue #139):
a query that matches only a series' native/alt title (e.g. the Korean name of an
English-display series) prunes exactly as a main-title match would. Callers pass
``keys=`` to supply ``[main, *alt_titles]`` per candidate; the legacy single-
title ``key=`` form is retained unchanged for backward compatibility.

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
    key: Callable[[T], str | None] | None = None,
    keys: Callable[[T], Sequence[str | None]] | None = None,
    cap: int,
) -> list[T]:
    """Narrow a relevance-sorted candidate list before the chapter fan-out.

    Returns a sub-list of the SAME items in the SAME (relevance-sorted) order.
    Each candidate is scored against its MAIN *or* any ALTERNATE title — its
    per-candidate score is ``max(_score(t, query) for t in titles)`` (#139), and
    a candidate counts as an exact match when ANY of its normalized titles equals
    the normalized query:

    * **Exact match** — when exactly ONE candidate matches exactly (via its main
      OR an alt title), return only that candidate (length 1).
    * **Dominant score** — when no single exact match applies but the top
      candidate's score clears :data:`_DOMINANT_MIN_SCORE` AND leads the
      next-best by more than :data:`_RELEVANCE_GAP_THRESHOLD`, return only the
      top candidate.
    * **Conservative fallback** — otherwise return ``candidates[:cap]``
      unchanged, byte-identical to the historic per-source slice (#126: fan out
      when in doubt).

    Empty and single-candidate lists pass through unchanged. A title that is
    ``None``/empty is dropped; a candidate with no usable titles scores zero and
    never wins any prune branch.

    Exactly ONE of ``key`` / ``keys`` must be supplied:

    * ``key`` — legacy single-title extractor (may return ``None``).
    * ``keys`` — multi-title extractor returning ``[main, *alt_titles]`` (entries
      may be ``None``/empty and are dropped).

    :param candidates: relevance-sorted candidate items (highest first).
    :param query: the user's raw search query.
    :param key: extracts a candidate's single title (mutually exclusive w/ keys).
    :param keys: extracts a candidate's title list (mutually exclusive w/ key).
    :param cap: the existing per-source candidate ceiling for the fan-out.
    :raises ValueError: if neither or both of ``key`` / ``keys`` are supplied.
    """
    if (key is None) == (keys is None):
        raise ValueError("prune_candidates requires exactly one of key= or keys=")

    if keys is not None:
        extract = keys
    else:
        # key is not None here (validated above); wrap the single title as a list.
        single = key

        def extract(item: T) -> Sequence[str | None]:
            return [single(item)]  # type: ignore[misc]

    if len(candidates) <= 1:
        return list(candidates)

    normalized_query = _normalize(query)
    if not normalized_query:
        return list(candidates[:cap])

    # Per-candidate normalized title list (None/empty dropped) — normalize each
    # title once and reuse it for both the exact and score branches.
    candidate_titles = [
        [norm for norm in (_normalize(t) for t in extract(item)) if norm]
        for item in candidates
    ]

    # Exact-match branch: a candidate is exact iff ANY of its titles == the
    # query. Prune ONLY when exactly one CANDIDATE is exact (two exact
    # candidates — whether via main or alt — is genuine ambiguity → fan out).
    exact_indices = [
        i
        for i, titles in enumerate(candidate_titles)
        if any(t == normalized_query for t in titles)
    ]
    if len(exact_indices) == 1:
        return [candidates[exact_indices[0]]]

    # Dominant-score branch: the relevance ordering is trusted, so the leader is
    # candidates[0]. Each candidate's score is the best over its titles.
    scores = [
        max((_score(t, normalized_query) for t in titles), default=0.0)
        for titles in candidate_titles
    ]
    top_score = scores[0]
    runner_up_score = max(scores[1:])
    if (
        top_score >= _DOMINANT_MIN_SCORE
        and top_score - runner_up_score > _RELEVANCE_GAP_THRESHOLD
    ):
        return [candidates[0]]

    return list(candidates[:cap])
