from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Iterable, Sequence, TypeVar

T = TypeVar("T")

_STOPWORDS: frozenset[str] = frozenset(
    {
        "di",
        "del",
        "dello",
        "della",
        "dei",
        "degli",
        "delle",
        "da",
        "dal",
        "in",
        "il",
        "lo",
        "la",
        "le",
        "gli",
        "un",
        "uno",
        "una",
        "ed",
        "the",
        "of",
        "and",
        "to",
        "on",
        "at",
        "by",
        "for",
        "no",
        "ni",
        "wa",
        "ga",
        "wo",
    }
)


def normalize_search_text(text: str) -> str:
    """
    Normalizza una stringa per il confronto di ricerca:
    rimuove accenti, converte in minuscolo, sostituisce punteggiatura con spazi
    e compatta gli spazi multipli.
    """
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    without_marks = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    cleaned = re.sub(r"[^a-z0-9]+", " ", without_marks.lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def generate_fallback_queries(query: str, max_queries: int = 6) -> list[str]:
    """
    Genera sotto-query di fallback (bigrammi adiacenti, parole chiave significative
    e prefissi per singole parole lunghe) per recuperare risultati anche in presenza
    di refusi (es. 'jojo steal ball run' -> 'ball run', 'jojo') o punteggiatura/ordine diversi.
    """
    norm = normalize_search_text(query)
    if not norm:
        return []

    tokens = [tok for tok in norm.split() if len(tok) >= 2]
    if not tokens:
        return []

    candidates: list[str] = []

    def _add(q: str) -> None:
        q = q.strip()
        if len(q) >= 3 and q != norm and q not in candidates:
            candidates.append(q)

    sig_tokens = [tok for tok in tokens if tok not in _STOPWORDS and len(tok) >= 3]
    if not sig_tokens:
        sig_tokens = [tok for tok in tokens if len(tok) >= 3] or tokens

    if len(tokens) >= 2:
        bigrams = [f"{tokens[i]} {tokens[i + 1]}" for i in range(len(tokens) - 1)]
        # Priorità all'ultimo bigramma (es. "ball run") e al primo bigramma (es. "steel ball")
        _add(bigrams[-1])
        _add(bigrams[0])
        # Prima parola chiave (spesso il nome del franchise, es. "jojo")
        if sig_tokens:
            _add(sig_tokens[0])
        for bg in bigrams[1:-1]:
            _add(bg)
        # Parole chiave rimanenti ordinate per lunghezza decrescente (più distintive)
        for tok in sorted(sig_tokens, key=len, reverse=True):
            _add(tok)
    else:
        word = sig_tokens[0]
        if len(word) >= 5:
            for cut in (len(word) - 1, len(word) - 2, 4, 3):
                if 3 <= cut < len(word):
                    _add(word[:cut])

    return candidates[:max_queries]


def _score_single_title(
    query_norm: str,
    query_compact: str,
    query_tokens: list[str],
    candidate_title: str,
) -> float:
    cand_norm = normalize_search_text(candidate_title)
    if not cand_norm:
        return 0.0

    if query_norm == cand_norm:
        return 1.0

    cand_compact = cand_norm.replace(" ", "")
    if len(query_compact) >= 4 and query_compact == cand_compact:
        return 0.99

    if query_norm in cand_norm:
        return 0.96 + 0.03 * (len(query_norm) / len(cand_norm))

    if len(query_compact) >= 5 and query_compact in cand_compact:
        return 0.94 + 0.03 * (len(query_compact) / len(cand_compact))

    cand_tokens = cand_norm.split()
    if not cand_tokens or not query_tokens:
        return 0.0

    cand_compounds = [
        cand_tokens[i] + cand_tokens[i + 1] for i in range(len(cand_tokens) - 1)
    ]
    pool = cand_tokens + cand_compounds

    token_scores: list[float] = []
    for qt in query_tokens:
        best_tok = 0.0
        for ct in pool:
            if qt == ct:
                best_tok = 1.0
                break
            ratio = SequenceMatcher(None, qt, ct).ratio()
            if len(qt) >= 4 and ct.startswith(qt):
                ratio = max(ratio, 0.88)
            if ratio > best_tok:
                best_tok = ratio
        token_scores.append(best_tok)

    avg_token_score = sum(token_scores) / len(token_scores)
    seq_ratio = SequenceMatcher(None, query_norm, cand_norm).ratio()
    compact_ratio = SequenceMatcher(None, query_compact, cand_compact).ratio()

    if len(query_tokens) == 1:
        return max(
            avg_token_score * 0.85 + max(seq_ratio, compact_ratio) * 0.15,
            compact_ratio,
        )

    matched_tokens = sum(1 for s in token_scores if s >= 0.72)
    coverage = matched_tokens / len(token_scores)
    if coverage < 0.5 and compact_ratio < 0.78:
        return 0.0

    return (
        0.65 * avg_token_score
        + 0.25 * coverage
        + 0.10 * max(seq_ratio, compact_ratio)
    )


def score_query_against_titles(query: str, titles: Iterable[str | None]) -> float:
    """
    Restituisce il punteggio massimo di similarità [0.0, 1.0] tra `query`
    e tutti i titoli/alias noti di un anime.
    """
    query_norm = normalize_search_text(query)
    if not query_norm:
        return 0.0
    query_compact = query_norm.replace(" ", "")
    query_tokens = query_norm.split()
    if len(query_tokens) > 1:
        filtered = [t for t in query_tokens if t not in _STOPWORDS]
        if filtered:
            query_tokens = filtered

    best = 0.0
    for title in titles:
        if not title:
            continue
        score = _score_single_title(query_norm, query_compact, query_tokens, title)
        if score > best:
            best = score
    return best


def rank_fuzzy_candidates(
    query: str,
    candidates: Sequence[tuple[T, Sequence[str | None]]],
    min_score: float = 0.62,
) -> list[T]:
    """
    Ordina i candidati per rilevanza fuzzy rispetto a `query` e scarta il rumore
    sotto `min_score` (o troppo distante dal miglior match trovato).
    """
    scored: list[tuple[float, int, T]] = []
    for idx, (item, aliases) in enumerate(candidates):
        score = score_query_against_titles(query, aliases)
        if score >= min_score:
            scored.append((score, idx, item))

    if not scored:
        return []

    scored.sort(key=lambda entry: (-entry[0], entry[1]))
    best_score = scored[0][0]
    cutoff = max(min_score, best_score - 0.20) if best_score >= 0.85 else min_score
    return [item for score, _, item in scored if score >= cutoff]
