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


_SANITIZED_TO_ASCII = str.maketrans("”⁎∕꞉‹›︖＼⏐", '"*/:<>?\\|')


def desanitize_search_query(text: str) -> str:
    """
    Ripristina la punteggiatura ASCII standard nei titoli precedentemente passati
    da `utilities.sanitize_filename` (es. `꞉` -> `:`, `∕` -> `/`), così che le query
    inviate ai server di ricerca (es. durante `update_link` dalla cronologia) trovino
    subito corrispondenza diretta.
    """
    if not text:
        return ""
    restored = text.replace("．．", "..").translate(_SANITIZED_TO_ASCII)
    return re.sub(r"\s+", " ", restored).strip()


def normalize_search_text(text: str) -> str:
    """
    Normalizza una stringa per il confronto di ricerca:
    rimuove accenti, converte in minuscolo, sostituisce punteggiatura con spazi
    e compatta gli spazi multipli.
    """
    if not text:
        return ""
    text = desanitize_search_query(text)
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
    sotto `min_score` (o troppo distante dal miglior match trovato), preservando
    l'ordine cronologico delle stagioni (`idx`) a parità di fascia di rilevanza.
    """
    scored: list[tuple[float, int, T]] = []
    for idx, (item, aliases) in enumerate(candidates):
        score = score_query_against_titles(query, aliases)
        if score >= min_score:
            scored.append((score, idx, item))

    if not scored:
        return []

    best_score = max(entry[0] for entry in scored)
    cutoff = max(min_score, best_score - 0.20) if best_score >= 0.85 else min_score
    filtered = [entry for entry in scored if entry[0] >= cutoff]
    # Raggruppa per fascia di rilevanza (~0.066) così che le varie stagioni/parti
    # dello stesso anime mantengano l'ordine cronologico fornito in input (`idx`).
    filtered.sort(key=lambda entry: (-int(entry[0] * 15), entry[1]))
    return [item for _, _, item in filtered]


_SEASON_QUARTER: dict[str, int] = {
    "inverno": 1,
    "winter": 1,
    "primavera": 2,
    "spring": 2,
    "estate": 3,
    "summer": 3,
    "autunno": 4,
    "autumn": 4,
    "fall": 4,
}


def strip_dub_tags(title: str) -> str:
    """Rimuove tag di lingua/edizione come (ITA), (SUB ITA), (CR), (DUB)."""
    cleaned = re.sub(
        r"\s*\((?:ITA|SUB\s*ITA|CR|DUB)\)\s*",
        " ",
        title,
        flags=re.IGNORECASE,
    )
    return re.sub(r"\s+", " ", cleaned).strip()


def extract_franchise_key(title: str) -> str:
    """
    Estrae la radice normalizzata del franchise dal titolo per raggruppare
    tutte le stagioni/film/OVA della stessa opera.
    """
    base = strip_dub_tags(desanitize_search_query(title))
    cut = re.split(
        r"(?::\s+|\s+[-–—]\s*|\s+\b(?:the\s+final\s+season|final\s+season|season\s+\d+|stagione\s+\d+|part(?:e)?\s+\d+|the\s+movie\b|movie\b|film\b|ova\b|oav\b|special\b|prequel\b))",
        base,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    cut = re.sub(r"\s+(?:\d+|ii|iii|iv|v|vi)$", "", cut.strip(), flags=re.IGNORECASE)
    norm = normalize_search_text(cut)
    return norm or normalize_search_text(base)


def extract_part_number(title: str) -> int:
    """
    Estrae l'indice progressivo di parte/stage/stagione dal titolo (default 1),
    usato per ordinare correttamente `Part 1 -> Part 2 -> Part 3` o `2nd Stage`.
    """
    base = strip_dub_tags(title)
    if match := re.search(
        r"\b(?:part(?:e)?|stage|cour|special)\s*(\d+)",
        base,
        flags=re.IGNORECASE,
    ):
        val = int(match.group(1))
        return val if val >= 1 else 1
    if match := re.search(
        r"\b(\d+)(?:st|nd|rd|th)\s*(?:[-–—]?\s*\d+(?:st|nd|rd|th)\s*)?(?:stage|season|part|cour)?",
        base,
        flags=re.IGNORECASE,
    ):
        val = int(match.group(1))
        return val if val >= 1 else 1
    if match := re.search(r"\s+(\d+)$", base):
        val = int(match.group(1))
        return val if val >= 1 else 1
    return 1


def sort_animeunity_records(records: list[dict], query: str = "") -> list[dict]:
    """
    Ordina i risultati di AnimeUnity raggruppando per franchise (unificando prefissi
    e varianti con la parola intera cercata, es. Dragon Ball / Z / GT / Super / Daima,
    Neon Genesis Evangelion / Evangelion 1.0, High School DxD / New / BorN / Hero)
    e ordinando tutte le stagioni/parti/film in ordine cronologico di uscita
    (Stagione 1 -> ultima stagione, con SUB subito seguito da ITA).
    """
    if len(records) <= 1:
        return list(records)

    q_norm = normalize_search_text(query)
    q_tokens = [t for t in q_norm.split() if t not in _STOPWORDS] or q_norm.split()

    raw_f_keys: list[str] = []
    for rec in records:
        raw_title = (
            rec.get("title_eng")
            or rec.get("title")
            or rec.get("title_it")
            or ""
        )
        raw_f_keys.append(extract_franchise_key(raw_title))

    # Unifica sotto-serie che condividono una radice presente nei risultati
    # (es. "dragon ball" unifica "dragon ball z", "dragon ball gt", "dragon ball super", "dragon ball daima")
    unique_roots = sorted(
        {fk for fk in raw_f_keys if len(fk) >= 4},
        key=len,
    )

    def _canonical_franchise(fk: str, aliases_tokens: set[str]) -> str:
        if q_tokens and all(qt in aliases_tokens for qt in q_tokens):
            # Tutte le parole chiave della query compaiono come parole intere nel titolo/alias
            # (es. "baki" in "Grappler Baki" e "Baki Hanma", ma NON in "Tsubaki"):
            # unifichiamo il gruppo principale della query per ordinarlo cronologicamente.
            return f"__query_exact__{q_norm}"
        for root in unique_roots:
            if fk == root or fk.startswith(root + " "):
                return root
        return fk

    has_primary_query_match = bool(q_tokens) and any(
        all(
            qt in set(
                normalize_search_text(
                    r.get("title_eng") or r.get("title") or r.get("title_it") or ""
                ).split()
            )
            for qt in q_tokens
        )
        for r in records
    )

    work_meta: dict[str, tuple[int, int, int, int]] = {}
    franchise_first_idx: dict[str, int] = {}
    franchise_best_rel: dict[str, int] = {}
    item_info: list[tuple[str, str, int, dict]] = []

    for idx, rec in enumerate(records):
        raw_title = (
            rec.get("title_eng")
            or rec.get("title")
            or rec.get("title_it")
            or ""
        )
        base_norm = normalize_search_text(strip_dub_tags(raw_title))
        fk_raw = raw_f_keys[idx]

        aliases_norm_list = [
            normalize_search_text(a or "")
            for a in (rec.get("title_eng"), rec.get("title"), rec.get("title_it"))
            if a
        ]
        aliases_norm = " ".join(aliases_norm_list)
        primary_tokens = set(normalize_search_text(raw_title).split())
        all_alias_tokens = set(aliases_norm.split())

        f_key = _canonical_franchise(fk_raw, primary_tokens)
        if (
            not has_primary_query_match
            and f_key != f"__query_exact__{q_norm}"
            and q_tokens
            and all(qt in all_alias_tokens for qt in q_tokens)
        ):
            f_key = f"__query_exact__{q_norm}"

        if f_key not in franchise_first_idx:
            franchise_first_idx[f_key] = idx

        if f_key == f"__query_exact__{q_norm}":
            rel = 0
        elif q_norm and fk_raw == q_norm:
            rel = 0
        elif q_norm and fk_raw.startswith(q_norm):
            rel = 1
        elif q_norm and q_norm in fk_raw:
            rel = 2
        elif q_norm and q_norm in aliases_norm:
            rel = 3
        else:
            rel = 4

        if f_key not in franchise_best_rel or rel < franchise_best_rel[f_key]:
            franchise_best_rel[f_key] = rel

        raw_date = str(rec.get("date") or "").strip()
        year = int(raw_date) if raw_date.isdigit() else 9999
        season_str = str(rec.get("season") or "").strip().lower()
        quarter = _SEASON_QUARTER.get(season_str, 3)
        created = str(rec.get("created_at") or "")
        if quarter == 1 and year != 9999 and created.startswith(f"{year}-12"):
            quarter = 5

        part_num = extract_part_number(raw_title)
        rec_id = int(rec["id"]) if str(rec.get("id", "")).isdigit() else 999999

        if base_norm not in work_meta:
            work_meta[base_norm] = (year, quarter, part_num, rec_id)
        else:
            prev_y, prev_q, prev_p, prev_id = work_meta[base_norm]
            merged_y = min(prev_y, year)
            merged_q = (
                max(prev_q, quarter)
                if prev_y == year and 5 in (prev_q, quarter)
                else min(prev_q, quarter)
            )
            work_meta[base_norm] = (
                merged_y,
                merged_q,
                min(prev_p, part_num),
                min(prev_id, rec_id),
            )

        is_dub = 1 if (rec.get("dub") or "(ita)" in raw_title.lower()) else 0
        item_info.append((f_key, base_norm, is_dub, rec))

    item_info.sort(
        key=lambda entry: (
            franchise_best_rel[entry[0]],
            franchise_first_idx[entry[0]],
            *work_meta[entry[1]],
            entry[1],
            entry[2],
        )
    )
    return [entry[3] for entry in item_info]


def sort_animeworld_entries(
    entries: Sequence[tuple[T, Sequence[str | None]]],
    get_title,
) -> list[tuple[T, list[str | None]]]:
    """
    Raffina l'ordine cronologico (`sort=4`) di AnimeWorld:
    accoppia sempre SUB prima di (ITA)/(CR) per la stessa stagione e riordina
    eventuali split `Part/Parte/Stage 1 -> 2 -> 3` fuori posto (es. Stone Ocean).
    """
    if len(entries) <= 1:
        return [(item, list(aliases)) for item, aliases in entries]

    stem_first_idx: dict[str, int] = {}
    decorated: list[tuple[str, int, int, int, int, T, list[str | None]]] = []

    for idx, (item, aliases) in enumerate(entries):
        title = str(get_title(item) or "")
        base = strip_dub_tags(title)
        # Rimuove suffissi espliciti "Part/Parte/Stage/Cour N" per unificare gli split della stessa stagione
        stem_raw = re.sub(
            r"\s*[-–—:]?\s*\b(?:part(?:e)?|stage|cour)\s*\d+\b.*$",
            "",
            base,
            flags=re.IGNORECASE,
        ).strip()
        stem = normalize_search_text(stem_raw or base)
        if stem not in stem_first_idx:
            stem_first_idx[stem] = idx

        part_match = re.search(
            r"\b(?:part(?:e)?|stage|cour)\s*(\d+)\b",
            base,
            flags=re.IGNORECASE,
        )
        part_num = int(part_match.group(1)) if part_match else 1
        lower_title = title.lower()
        is_dub = 1 if "(ita)" in lower_title else 0
        is_cr = 1 if "(cr)" in lower_title else 0
        decorated.append((stem, part_num, is_dub, is_cr, idx, item, list(aliases)))

    decorated.sort(
        key=lambda d: (
            stem_first_idx[d[0]],
            d[1],
            d[2],
            d[3],
            d[4],
        )
    )
    return [(d[5], d[6]) for d in decorated]
