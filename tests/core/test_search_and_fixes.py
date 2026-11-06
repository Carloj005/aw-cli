import pytest
from unittest.mock import MagicMock, patch

from aw_cli.anime import Anime, AnimeStatus
from aw_cli.providers.animeunity import Animeunity
from aw_cli.providers.animeworld import Animeworld
from aw_cli.providers.provider import update_link
from aw_cli.search_utils import (
    generate_fallback_queries,
    normalize_search_text,
    rank_fuzzy_candidates,
    sort_animeunity_records,
    sort_animeworld_entries,
)


def test_normalize_search_text():
    assert normalize_search_text("JoJo's Bizarre Adventure: Steel Ball Run!") == (
        "jojo s bizarre adventure steel ball run"
    )
    assert normalize_search_text("  L'Attacco   dei Giganti — Parte 2 ") == (
        "l attacco dei giganti parte 2"
    )


def test_generate_fallback_queries_for_typo_and_compound():
    q_sbr = generate_fallback_queries("jojo steal ball run")
    assert "ball run" in q_sbr
    assert "jojo" in q_sbr

    q_short = generate_fallback_queries("steal ball run")
    assert q_short[0] == "ball run"

    q_single = generate_fallback_queries("chainsawman")
    assert any("chainsaw".startswith(sub) or sub.startswith("chai") for sub in q_single)


def test_rank_fuzzy_candidates_finds_steel_ball_run_and_filters_noise():
    candidates = [
        (
            "Stardust",
            [
                "Le Bizzarre Avventure di JoJo: Stardust Crusaders",
                "JoJo no Kimyou na Bouken: Stardust Crusaders",
            ],
        ),
        (
            "DragonBall",
            ["Dragon Ball Super", "Dragon Ball Z"],
        ),
        (
            "SBR_SUB",
            [
                "Le Bizzarre Avventure di JoJo: Steel Ball Run",
                "Steel Ball Run: JoJo no Kimyou na Bouken",
            ],
        ),
        (
            "SBR_ITA",
            [
                "Le Bizzarre Avventure di JoJo: Steel Ball Run (ITA)",
                "Steel Ball Run: JoJo's Bizarre Adventure (ITA)",
            ],
        ),
    ]

    ranked = rank_fuzzy_candidates("jojo steal ball run", candidates)
    assert "SBR_SUB" in ranked
    assert "SBR_ITA" in ranked
    assert "DragonBall" not in ranked
    assert "Stardust" not in ranked

    ranked_no_jojo = rank_fuzzy_candidates("steal ball run", candidates)
    assert ranked_no_jojo == ["SBR_SUB", "SBR_ITA"]


def test_rank_fuzzy_candidates_handles_compounds_and_typos():
    candidates = [
        ("DanDaDan", ["Dan Da Dan", "Dandadan"]),
        ("ChainsawMan", ["Chainsaw Man"]),
        ("AttackOnTitan", ["L'Attacco dei Giganti", "Attack on Titan"]),
        ("OnePiece", ["One Piece"]),
    ]

    assert rank_fuzzy_candidates("dandadan", candidates) == ["DanDaDan"]
    assert rank_fuzzy_candidates("chainsawman", candidates) == ["ChainsawMan"]
    assert rank_fuzzy_candidates("attak on titan", candidates) == ["AttackOnTitan"]
    assert rank_fuzzy_candidates("one pice", candidates) == ["OnePiece"]


def _make_au_record(
    rec_id: int,
    title_eng: str,
    title: str | None = None,
    title_it: str | None = None,
    dub: int = 0,
    anilist_id: int | None = 190327,
    episodes_count: int | None = 1,
    real_episodes_count: int | None = 1,
) -> dict:
    return {
        "id": rec_id,
        "title": title,
        "title_eng": title_eng,
        "title_it": title_it,
        "slug": title_eng.lower().replace(" ", "-").replace(":", ""),
        "anilist_id": anilist_id,
        "status": "In Corso",
        "type": "TV",
        "dub": dub,
        "date": "2026",
        "season": "Primavera",
        "studio": "David Production",
        "score": "9.1",
        "episodes_length": 24,
        "episodes_count": episodes_count,
        "real_episodes_count": real_episodes_count,
        "visite": 1000,
        "plot": "Steel Ball Run plot",
        "genres": [{"id": 1, "name": "Action"}, {"id": 2, "name": "Adventure"}],
    }


def test_animeunity_search_combines_archivio_beyond_livesearch_cap():
    mock_client = MagicMock()
    au = Animeunity(client=mock_client)
    setattr(au, "_session", "<html></html>")

    archivio_records = [
        _make_au_record(100 + i, f"JoJo Parte {i}") for i in range(1, 12)
    ] + [
        _make_au_record(
            7431,
            "Le Bizzarre Avventure di JoJo: Steel Ball Run",
            "Steel Ball Run: JoJo no Kimyou na Bouken",
        )
    ]
    livesearch_records = archivio_records[:8]

    def post_side_effect(*args, **kwargs):
        url = kwargs.get("url") or (args[0] if args else "")
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        if "/archivio/get-animes" in url:
            resp.json.return_value = {"records": archivio_records, "tot": len(archivio_records)}
        else:
            resp.json.return_value = {"records": livesearch_records}
        return resp

    mock_client.post.side_effect = post_side_effect
    results = au._search("jojo")

    assert len(results) == 12
    assert any("Steel Ball Run" in a.name for a in results)


def test_animeunity_search_fuzzy_fallback_on_typo():
    mock_client = MagicMock()
    au = Animeunity(client=mock_client)
    setattr(au, "_session", "<html></html>")

    sbr_rec = _make_au_record(
        7431,
        "Le Bizzarre Avventure di JoJo: Steel Ball Run",
        "Steel Ball Run: JoJo no Kimyou na Bouken",
    )
    other_rec = _make_au_record(
        101,
        "Le Bizzarre Avventure di JoJo: Stardust Crusaders",
        "JoJo no Kimyou na Bouken: Stardust Crusaders",
    )

    def post_side_effect(*args, **kwargs):
        payload = kwargs.get("json") or kwargs.get("data") or {}
        q = payload.get("title", "")
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        if q in ("ball run",):
            resp.json.return_value = {"records": [sbr_rec], "tot": 1}
        elif q == "jojo":
            resp.json.return_value = {"records": [other_rec, sbr_rec], "tot": 2}
        else:
            resp.json.return_value = {"records": [], "tot": 0}
        return resp

    mock_client.post.side_effect = post_side_effect
    results = au._search("jojo steal ball run")

    assert len(results) == 1
    assert results[0].ref == "7431"
    assert "Steel Ball Run" in results[0].name


def test_animeunity_null_safety_and_vixcloud_referer():
    mock_client = MagicMock()
    au = Animeunity(client=mock_client)
    setattr(au, "_session", "<html></html>")

    rec_with_nulls = _make_au_record(
        7826,
        "Nuovo Anime",
        title=None,
        title_it=None,
        anilist_id=None,
        episodes_count=None,
        real_episodes_count=None,
    )
    rec_with_nulls["episodes_length"] = None
    rec_with_nulls["plot"] = None

    title, last_ep, anilist_id, status, info = au._parse_info(rec_with_nulls)
    assert title == "Nuovo Anime"
    assert last_ep == "0"
    assert anilist_id == 0
    assert info["Episodi"] == "??"
    assert info["Durata"] == "??"
    assert info["Genere"] == "Action, Adventure"

    anime = Anime(title, "7826", last_ep="5")
    ep = Anime.Episode(anime, "1", "99999")

    embed_resp = MagicMock(text="https://vixcloud.cc/embed/692680?token=abc&expires=999&canPlayFHD=1")
    embed_resp.raise_for_status = lambda: None
    player_resp = MagicMock(text="window.downloadUrl = 'https://cdn.example.com/ep1.mp4';")
    player_resp.raise_for_status = lambda: None
    mock_client.get.side_effect = [embed_resp, player_resp]

    url = au._episode_link(anime, ep)
    assert url == "https://cdn.example.com/ep1.mp4"
    iframe_call = mock_client.get.call_args_list[1]
    assert iframe_call.kwargs["headers"]["Referer"] == "https://www.animeunity.co/"


def test_animeworld_filter_html_excludes_sidebar_and_fuzzy_matches():
    mock_client = MagicMock()
    aw = Animeworld(client=mock_client)
    aw.Client.headers = {"csrf-token": "tok"}

    sample_filter_html = """
    <div class="film-list">
        <div class="item">
            <a href="/play/le-bizzarre-avventure-di-jojo-steel-ball-run.Wtwgu"
               data-jtitle="Steel Ball Run: JoJo no Kimyou na Bouken"
               class="name">Le Bizzarre Avventure di JoJo: Steel Ball Run</a>
        </div>
        <div class="item">
            <a href="/play/le-bizzarre-avventure-di-jojo-steel-ball-run-ita.tblVT"
               data-jtitle="Steel Ball Run: JoJo no Kimyou na Bouken (ITA)"
               class="name">Le Bizzarre Avventure di JoJo: Steel Ball Run (ITA)</a>
        </div>
    </div>
    <div class="paging-wrapper"></div>
    <div class="widget simple-film-list">
        <a href="/play/one-piece-subita.qzG-LE" class="name">One Piece</a>
    </div>
    """

    def post_side_effect(*args, **kwargs):
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.json.return_value = {"animes": []}
        return resp

    mock_client.post.side_effect = post_side_effect
    with patch.object(
        aw,
        "_get_html",
        side_effect=lambda url: sample_filter_html if "keyword=ball+run" in url else '<div class="film-list"></div><div class="paging-wrapper"></div>',
    ):
        results = aw._search("jojo steal ball run")

    assert [r.name for r in results] == [
        "Le Bizzarre Avventure di JoJo: Steel Ball Run",
        "Le Bizzarre Avventure di JoJo: Steel Ball Run (ITA)",
    ]
    assert results[1].dub is True


def test_anime_update_episodes_updates_last_ep_from_zero_and_part2_numbering():
    # Caso AnimeWorld: last_ep iniziale "0" deve aggiornarsi all'ultimo episodio disponibile
    aw_anime = Anime("Steel Ball Run", "https://www.animeworld.ac/play/sbr.Wtwgu")
    assert aw_anime.last_ep == "0"
    aw_anime.update_episodes({"1": "id1", "2": "id2", "8": "id8"})
    assert aw_anime.last_ep == "8"

    # Caso Part 2 con episodi numerati 13..24 ma episodes_count=12
    cour2 = Anime("Anime Cour 2", "9999", last_ep="12")
    cour2.update_episodes({str(n): f"id{n}" for n in range(13, 25)})
    assert cour2.last_ep == "24"

    # Caso cronologia parziale: ep 1 salvato su 12 totali NON deve abbassare last_ep a 1
    partial = Anime("Partial History", "1627", curr_ep="1", last_ep="12")
    partial.update_episodes({"1": "Not available"})
    assert partial.last_ep == "12"


def test_update_link_preserves_dub_variant(monkeypatch):
    monkeypatch.setattr("aw_cli.providers.provider.ut.console", MagicMock())
    monkeypatch.setattr("aw_cli.providers.provider.ut.config_data", {"general": {"specials": False}})

    sub_anime = Anime("Le Bizzarre Avventure di JoJo: Steel Ball Run", "7431")
    sub_anime.set_info(190327, AnimeStatus.FINISHED, {"Audio": "??"})

    dub_anime = Anime("Le Bizzarre Avventure di JoJo: Steel Ball Run (ITA)", "7432")
    dub_anime.set_info(190327, AnimeStatus.FINISHED, {"Audio": "Italiano"})

    stale_history_entry = Anime("Le Bizzarre Avventure di JoJo: Steel Ball Run (ITA)", "0000")
    stale_history_entry.set_info(190327, AnimeStatus.FINISHED, {"Audio": "Italiano"})

    provider = MagicMock()
    provider.search.return_value = [sub_anime, dub_anime]
    callback = MagicMock(return_value="ok")
    callback.__name__ = "_info_anime"

    res = update_link(provider, callback, stale_history_entry)
    assert res == "ok"
    assert stale_history_entry.ref == "7432"


def test_sort_animeunity_records_chronological_jojo_and_december_winter():
    # Ordine alfabetico "sporco" come restituito da /archivio/get-animes
    scrambled = [
        {**_make_au_record(627, "Le Bizzarre Avventure di JoJo: Diamond is Unbreakable"), "date": "2016", "season": "Primavera"},
        {**_make_au_record(628, "Le Bizzarre Avventure di JoJo: Stardust Crusaders"), "date": "2014", "season": "Primavera"},
        {**_make_au_record(7432, "Le Bizzarre Avventure di JoJo: Steel Ball Run (ITA)", dub=1), "date": "2026", "season": "Inverno"},
        {**_make_au_record(7431, "Le Bizzarre Avventure di JoJo: Steel Ball Run", dub=0), "date": "2026", "season": "Inverno"},
        {**_make_au_record(7808, "Le Bizzarre Avventure di JoJo: Steel Ball Run 2nd - 3rd Stage"), "date": "2026", "season": "Autunno"},
        {**_make_au_record(3327, "Le Bizzarre Avventure di JoJo: Stone Ocean"), "date": "2021", "season": "Autunno"},
        {**_make_au_record(2625, "Le Bizzarre Avventure di JoJo: Vento Aureo"), "date": "2018", "season": "Autunno"},
        {**_make_au_record(2624, "Le Bizzarre Avventure di JoJo"), "date": "2012", "season": "Autunno"},
    ]

    ordered = sort_animeunity_records(scrambled, "jojo")
    titles = [r["title_eng"] for r in ordered]
    assert titles == [
        "Le Bizzarre Avventure di JoJo",
        "Le Bizzarre Avventure di JoJo: Stardust Crusaders",
        "Le Bizzarre Avventure di JoJo: Diamond is Unbreakable",
        "Le Bizzarre Avventure di JoJo: Vento Aureo",
        "Le Bizzarre Avventure di JoJo: Stone Ocean",
        "Le Bizzarre Avventure di JoJo: Steel Ball Run",
        "Le Bizzarre Avventure di JoJo: Steel Ball Run (ITA)",
        "Le Bizzarre Avventure di JoJo: Steel Ball Run 2nd - 3rd Stage",
    ]

    # Verifica che Inverno di dicembre YYYY (es. Entertainment District dic 2021) venga dopo Autunno YYYY (Mugen Train ott 2021)
    ds = [
        {**_make_au_record(3337, "Demon Slayer: Entertainment District Arc"), "date": "2021", "season": "Inverno", "created_at": "2021-12-05"},
        {**_make_au_record(3257, "Demon Slayer: Mugen Train Arc"), "date": "2021", "season": "Autunno", "created_at": "2021-10-10"},
    ]
    ordered_ds = sort_animeunity_records(ds, "demon slayer")
    assert [r["id"] for r in ordered_ds] == [3257, 3337]


def test_sort_animeworld_entries_fixes_parts_and_dub_pairing():
    raw_entries = [
        (Anime("Le Bizzarre Avventure di JoJo: Vento Aureo (ITA)", "u1"), ["Vento Aureo (ITA)"]),
        (Anime("Le Bizzarre Avventure di JoJo: Vento Aureo", "u2"), ["Vento Aureo"]),
        (Anime("Le Bizzarre Avventure di JoJo: Stone Ocean", "u3"), ["Stone Ocean"]),
        (Anime("Le Bizzarre Avventure di JoJo: Stone Ocean Parte 3", "u4"), ["Stone Ocean Parte 3"]),
        (Anime("Le Bizzarre Avventure di JoJo: Stone Ocean (ITA)", "u5"), ["Stone Ocean (ITA)"]),
        (Anime("Le Bizzarre Avventure di JoJo: Stone Ocean Parte 2", "u6"), ["Stone Ocean Parte 2"]),
        (Anime("Le Bizzarre Avventure di JoJo: Steel Ball Run", "u7"), ["Steel Ball Run"]),
    ]

    sorted_entries = sort_animeworld_entries(raw_entries, lambda a: a.name)
    assert [a.name for a, _ in sorted_entries] == [
        "Le Bizzarre Avventure di JoJo: Vento Aureo",
        "Le Bizzarre Avventure di JoJo: Vento Aureo (ITA)",
        "Le Bizzarre Avventure di JoJo: Stone Ocean",
        "Le Bizzarre Avventure di JoJo: Stone Ocean (ITA)",
        "Le Bizzarre Avventure di JoJo: Stone Ocean Parte 2",
        "Le Bizzarre Avventure di JoJo: Stone Ocean Parte 3",
        "Le Bizzarre Avventure di JoJo: Steel Ball Run",
    ]


def test_sort_animeunity_unifies_subseries_without_colon_and_japanese_titles():
    # Franchise senza ":" tra radice e suffisso stagione (Dragon Ball / Z / GT / Super / Daima,
    # Neon Genesis Evangelion / Evangelion 1.0) e filtro rumore sottostringa (Baki vs Tsubaki).
    db_scrambled = [
        {**_make_au_record(10, "Dragon Ball Daima"), "date": "2024", "season": "Autunno"},
        {**_make_au_record(11, "Dragon Ball GT"), "date": "1996", "season": "Inverno"},
        {**_make_au_record(12, "Dragon Ball Super"), "date": "2015", "season": "Estate"},
        {**_make_au_record(13, "Dragon Ball Z"), "date": "1989", "season": "Primavera"},
        {**_make_au_record(14, "Dragon Ball"), "date": "1986", "season": "Inverno"},
    ]
    assert [r["title_eng"] for r in sort_animeunity_records(db_scrambled, "dragon ball")] == [
        "Dragon Ball",
        "Dragon Ball Z",
        "Dragon Ball GT",
        "Dragon Ball Super",
        "Dragon Ball Daima",
    ]

    baki_mixed = [
        {**_make_au_record(20, "In the Heart of Kunoichi Tsubaki"), "date": "2022", "season": "Primavera"},
        {**_make_au_record(21, "Baki Hanma"), "date": "2021", "season": "Autunno"},
        {**_make_au_record(22, "Grappler Baki"), "date": "1994", "season": "Estate"},
        {**_make_au_record(23, "Baki (2018)"), "date": "2018", "season": "Estate"},
    ]
    assert [r["title_eng"] for r in sort_animeunity_records(baki_mixed, "baki")] == [
        "Grappler Baki",
        "Baki (2018)",
        "Baki Hanma",
        "In the Heart of Kunoichi Tsubaki",
    ]
