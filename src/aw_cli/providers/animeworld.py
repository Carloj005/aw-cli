from functools import lru_cache
import re
from html import unescape
from urllib.parse import quote_plus, urlsplit, urlunsplit
from ..anime import Anime, AnimeStatus
from ..domain_resolver import (
    DEFAULT_ANIMEWORLD_URL,
    extract_origin,
    get_configured_provider_url,
    install_doh_fallback,
    resolve_animeworld_url,
)
from ..search_utils import (
    desanitize_search_query,
    generate_fallback_queries,
    rank_fuzzy_candidates,
    sort_animeworld_entries,
)
from .provider import Provider, HTTPError


class Animeworld(Provider):
    """
    Classe che gestisce il collegamento con Animeworld.

    Attributes:
        _url (str): l'url del sito di Animeworld.
        _headers (dict): gli headers da utilizzare per le richieste HTTP.
        _cookies (dict): i cookies da utilizzare per le richieste HTTP.
        _visited (dict): le pagine già visitate.
    """

    def __init__(self, client=None):
        if client is not None:
            base_url = DEFAULT_ANIMEWORLD_URL
        else:
            base_url = get_configured_provider_url("animeworld", DEFAULT_ANIMEWORLD_URL)
        super().__init__(base_url, client=client)
        self._visited = {}

    def _normalize_url(self, url: str) -> str:
        """
        Riscrive eventuali vecchi domini di AnimeWorld (es. animeworld.so / animeworld.to
        salvati in history.json) verso il dominio attivo corrente `self.BASE_URL`.
        """
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if parts.scheme in ("http", "https") and re.fullmatch(
            r"(?:www\.)?animeworld\.[a-z]{2,10}", host
        ):
            base_parts = urlsplit(self.BASE_URL)
            if base_parts.scheme and base_parts.netloc and parts.netloc.lower() != base_parts.netloc.lower():
                return urlunsplit(
                    (base_parts.scheme, base_parts.netloc, parts.path, parts.query, parts.fragment)
                )
        return url

    @lru_cache
    def _get_html(self, url: str) -> str:
        """
        Ottiene l'html della pagina web `url`.
        Gestisce cookie JavaScript (document.cookie) e imposta il csrf-token se presente.
        """
        if not re.match(r"^https?://", url):
            raise HTTPError("Errore 404: pagina non trovata")

        url = self._normalize_url(url)
        install_doh_fallback()
        try:
            response = self.Client.get(url)
        except Exception:
            if url == self.BASE_URL:
                self.BASE_URL = resolve_animeworld_url(self.Client, self.BASE_URL)
                url = self.BASE_URL
            response = self.Client.get(url)

        if url == self.BASE_URL and getattr(response, "url", None):
            self.BASE_URL = extract_origin(str(response.url), fallback=self.BASE_URL)

        # Gestione cookie impostati tramite JavaScript (document.cookie)
        cookie_match = re.search(
            r'document\.cookie\s*?=\s*?"([^=]+)=([^";]+)', response.text
        )
        if cookie_match:
            name, value = cookie_match.groups()
            self.Client.cookies.set(name, value)
            response = self.Client.get(url)

        response.raise_for_status()

        # Estrai e aggiorna il csrf-token se presente nella pagina per le chiamate API
        csrf_token_match = re.search(
            r'<meta.*?id="csrf-token"\s*?content="(.*?)">', response.text
        )
        if csrf_token_match:
            self.Client.headers["csrf-token"] = csrf_token_match.group(1)

        if response.status_code != 200 or "Errore 404" in response.text:
            raise HTTPError("Errore 404: pagina non trovata")

        return response.text

    def _search_api_v2(self, query: str) -> list[tuple[Anime, list[str | None]]]:
        with self._http_lock:
            response = self.Client.post(
                f"{self.BASE_URL}/api/search/v2", params={"keyword": query}
            )
        response.raise_for_status()

        data = response.json()
        if not isinstance(data, dict) or "error" in data or "animes" not in data:
            return []

        results: list[tuple[Anime, list[str | None]]] = []
        for elem in data["animes"]:
            link = elem.get("link")
            identifier = elem.get("identifier")
            if link and identifier:
                url = f"{self.BASE_URL}/play/{link}.{identifier}"
            else:
                url = self.BASE_URL

            name = unescape(elem.get("name", ""))
            jtitle_raw = elem.get("jtitle")
            jtitle = unescape(jtitle_raw) if isinstance(jtitle_raw, str) and jtitle_raw else None
            slug_alias = link.replace("-", " ") if isinstance(link, str) else None

            anime = Anime(name, url)
            anilist_id, status, info = self._parse_info(elem)
            anime.set_info(anilist_id, status, info)
            results.append((anime, [name, jtitle, slug_alias]))
        return results

    def _search_filter_html(self, query: str) -> list[tuple[Anime, list[str | None]]]:
        """
        Estrae i risultati completi dalla pagina `/filter?keyword=...&sort=4` di AnimeWorld
        in ordine cronologico di uscita (superando il limite di 5 risultati di `/api/search/v2`),
        ignorando la sidebar.
        """
        try:
            html = self._get_html(
                f"{self.BASE_URL}/filter?keyword={quote_plus(query)}&sort=4"
            )
        except Exception:
            return []

        if not isinstance(html, str) or not html:
            return []

        block_match = re.search(
            r'<div class="film-list">(.*?)(?:<div class="paging-wrapper|window\.paginationUrl|<div id="sidebar"|<div class="widget\b|\Z)',
            html,
            re.DOTALL,
        )
        film_block = block_match.group(1) if block_match else ""
        if not film_block:
            return []

        results: list[tuple[Anime, list[str | None]]] = []
        seen_refs: set[str] = set()
        for attrs, raw_title in re.findall(
            r'<a\s+([^>]*\bclass="name"[^>]*)>([^<]+)</a>',
            film_block,
        ):
            href_match = re.search(r'href="(/play/[^"]+)"', attrs)
            if not href_match:
                continue
            href = href_match.group(1)
            ref = f"{self.BASE_URL}{href}"
            if ref in seen_refs:
                continue
            seen_refs.add(ref)

            title = unescape(raw_title.strip())
            jtitle_match = re.search(r'data-jtitle="([^"]*)"', attrs)
            jtitle = unescape(jtitle_match.group(1).strip()) if jtitle_match else None
            slug_part = href.split("/play/", 1)[-1].rsplit(".", 1)[0].replace("-", " ")

            anime = Anime(title, ref)
            if "(ITA)" in title:
                anime.info["Audio"] = "Italiano"
                anime.dub = True
            results.append((anime, [title, jtitle, slug_part]))

        return results

    def _search(self, input: str) -> list[Anime]:
        query = desanitize_search_query(input.strip())
        if not query:
            return []

        # Assicuriamoci che il client abbia cookies/csrf-token caricando prima la homepage
        if "csrf-token" not in self.Client.headers:
            try:
                self._get_html(self.BASE_URL)
            except Exception:
                pass

        primary = self._search_api_v2(query)
        filter_hits = self._search_filter_html(query)

        if filter_hits:
            api_by_ref: dict[str, tuple[Anime, list[str | None]]] = {
                anime.ref: (anime, list(aliases))
                for anime, aliases in primary
                if anime.ref != self.BASE_URL
            }
            ordered_entries: list[tuple[Anime, list[str | None]]] = []
            seen_refs: set[str] = set()
            for f_anime, f_aliases in filter_hits:
                seen_refs.add(f_anime.ref)
                if f_anime.ref in api_by_ref:
                    api_anime, api_aliases = api_by_ref[f_anime.ref]
                    combined = list(api_aliases)
                    for al in f_aliases:
                        if al and al not in combined:
                            combined.append(al)
                    ordered_entries.append((api_anime, combined))
                else:
                    ordered_entries.append((f_anime, list(f_aliases)))

            for p_anime, p_aliases in primary:
                if p_anime.ref not in seen_refs:
                    ordered_entries.append((p_anime, list(p_aliases)))

            sorted_entries = sort_animeworld_entries(ordered_entries, lambda a: a.name)
            return [anime for anime, _ in sorted_entries]

        if primary:
            return [anime for anime, _ in primary]

        # Fallback multi-token / fuzzy per tollerare refusi e ordine parole diverso
        fallback_by_ref: dict[str, tuple[Anime, list[str | None]]] = {}
        for sub_query in generate_fallback_queries(query):
            for anime, aliases in self._search_filter_html(sub_query):
                if anime.ref not in fallback_by_ref:
                    fallback_by_ref[anime.ref] = (anime, list(aliases))
                else:
                    _, existing_aliases = fallback_by_ref[anime.ref]
                    for al in aliases:
                        if al and al not in existing_aliases:
                            existing_aliases.append(al)

            try:
                api_hits = self._search_api_v2(sub_query)
            except Exception:
                api_hits = []
            for anime, aliases in api_hits:
                key = anime.ref if anime.ref != self.BASE_URL else f"{anime.ref}#{anime.name}"
                if key not in fallback_by_ref:
                    fallback_by_ref[key] = (anime, list(aliases))
                else:
                    existing_anime, existing_aliases = fallback_by_ref[key]
                    for al in aliases:
                        if al and al not in existing_aliases:
                            existing_aliases.append(al)
                    if not existing_anime.info and anime.info:
                        fallback_by_ref[key] = (anime, existing_aliases)

        if not fallback_by_ref:
            return []

        ordered_fallback = sort_animeworld_entries(
            list(fallback_by_ref.values()),
            lambda a: a.name,
        )
        return rank_fuzzy_candidates(query, ordered_fallback)

    def _latest(self, filter: str, specials: bool) -> list[Anime]:
        html = self._get_html(self.BASE_URL)
        filter_key = (filter or "a")[0].lower()
        tab_map = {"a": "all", "s": "sub", "d": "dub", "t": "trending"}
        tab_name = tab_map.get(filter_key, "all")

        tab_match = re.search(
            rf'<div class="content[^"]*"\s+data-name="{tab_name}">(.*?)(?=<div class="content|<div class="widget|\Z)',
            html,
            re.DOTALL,
        )
        source_html = tab_match.group(1) if tab_match else html

        def _parse_posters(chunk: str, filter_specials: bool) -> list[Anime]:
            parsed = list[Anime]()
            for a_tag in re.findall(r'<a\s+[^>]*class="poster"[^>]*>', chunk):
                href_match = re.search(r'href="([^"]+)"', a_tag)
                title_match = re.search(r'title="([^"]+)"', a_tag)
                if not href_match or not title_match:
                    continue

                url = href_match.group(1)
                title = title_match.group(1)

                # Estrae l'episodio dal titolo (es: "Nome Anime Ep 9")
                ep = "0"
                ep_match = re.search(r' Ep[\.\s]+([^"]+)$', title)
                if ep_match:
                    ep = ep_match.group(1).strip()
                    name = title[: ep_match.start()].strip()
                else:
                    name = title

                if filter_specials and not specials and ("." in ep or ep == "0"):
                    continue

                parts = url.strip("/").split("/")
                if len(parts) >= 3:
                    anime_slug = parts[1]
                    player_id = parts[2]
                    anime_ref = f"{self.BASE_URL}/play/{anime_slug}"
                    ep_ref = player_id
                else:
                    anime_ref = self.BASE_URL + url
                    ep_ref = self.BASE_URL + url

                anime = Anime(unescape(name), anime_ref, ep)
                anime.update_episodes({ep: ep_ref}, specials=specials)
                parsed.append(anime)
            return parsed

        if tab_match:
            return _parse_posters(source_html, filter_specials=True)

        animes = _parse_posters(html, filter_specials=False)
        BLOCK_SIZE = 45
        filter_keys = ["a", "s", "d", "t"]
        multiplier = filter_keys.index(filter_key) if filter_key in filter_keys else 0
        start = multiplier * BLOCK_SIZE
        end = (multiplier + 1) * BLOCK_SIZE if multiplier < len(filter_keys) else None
        sliced = animes[start:end]
        if not specials:
            sliced = [a for a in sliced if not ("." in a.curr_ep or a.curr_ep == "0")]
        return sliced

    def _episodes(self, anime: Anime):
        html = self._get_html(anime.ref)
        episodes_url = dict[str, str]()

        # Estrae i tag <a> in modo super stabile limitandosi agli elementi con classe "episode"
        for a_tag in re.findall(r'<li class="episode">[\s\n]*(<a\s+[^>]+>)', html):
            id_match = re.search(r'data-id="([^"]+)"', a_tag)
            num_match = re.search(r'data-num="([^"]+)"', a_tag)
            if id_match and num_match:
                episodes_url[num_match.group(1)] = id_match.group(1)
        return episodes_url

    def _episode_link(self, anime: Anime, episode: Anime.Episode) -> str:
        player_id = episode.ref
        if "play/" in player_id:
            player_id = player_id.split("/")[-1]

        api_url = f"{self.BASE_URL}/api/episode/serverPlayerAnimeWorld?id={player_id}"
        with self._http_lock:
            response = self.Client.get(api_url)
        response.raise_for_status()

        source_matches = re.findall(r'<source\s+[^>]*src="([^"]+)"[^>]*>', response.text)
        if not source_matches:
            source_match = re.search(r'<source src="([^"]+)"', response.text)
            if source_match:
                source_matches = [source_match.group(1)]
        if not source_matches:
            raise ValueError("Errore nel parsing della pagina dell'episodio")

        valid_urls: list[tuple[int, str]] = []
        for raw in source_matches:
            candidate = raw.strip()
            parts = urlsplit(candidate)
            if parts.scheme in {"http", "https"} and parts.netloc:
                q_match = re.search(r"(2160|1440|1080|720|480|360)p?", candidate, re.IGNORECASE)
                q_score = int(q_match.group(1)) if q_match else 0
                valid_urls.append((q_score, candidate))

        if not valid_urls:
            raise ValueError("URL del video non valido")
        valid_urls.sort(key=lambda item: item[0], reverse=True)
        return valid_urls[0][1]

    def _info_anime(self, anime: Anime):
        html = self._get_html(anime.ref)

        res = re.search(r'<a.*id="anilist-button".*href="\D*(\d*)"', html)
        anilist_id = int(res.group(1)) if res else 0

        temp = re.findall(
            r'<dt>(.*?):</dt>[\n\s]*<dd(?: class="[^"]*")?>((?:.|\n)+?)</dd>', html
        )
        plot_match = re.search(r'<div class="desc">((?:.|\n)+?)</div>', html)
        if plot_match:
            temp.append(("Trama", plot_match.group(1)))

        info = dict[str, str]()
        status = AnimeStatus.UNKNOWN
        for key, value in temp:
            key, value = key.strip(), value.strip()
            if key == "Stato":
                if match := re.search(r'status=(\d+)"', value):
                    status_val = int(match.group(1))
                    if status_val < len(AnimeStatus):
                        status = list(AnimeStatus)[status_val]
            value = re.sub(r"[\s\n]+", " ", re.sub(r"<.*?>", "", value)).strip()
            info[key] = unescape(value)

        anime.set_info(anilist_id, status, info)

        ep_nums = [
            num_match.group(1)
            for a_tag in re.findall(r'<li class="episode">[\s\n]*(<a\s+[^>]+>)', html)
            if (num_match := re.search(r'data-num="([^"]+)"', a_tag))
        ]
        if ep_nums:
            try:
                max_ep = max(ep_nums, key=lambda n: Anime.Episode(anime, n, ""))
                if not anime.last_ep or anime.last_ep == "0":
                    anime.last_ep = max_ep
                elif Anime.Episode(anime, max_ep, "") > Anime.Episode(anime, anime.last_ep, ""):
                    anime.last_ep = max_ep
            except ValueError:
                anime.last_ep = ep_nums[-1]

    def _parse_info(self, elem: dict) -> tuple[int, AnimeStatus, dict[str, str]]:
        """Estrae e mappa i metadati ritornati dal JSON dell'API di ricerca di AnimeWorld."""
        anilist_id = elem.get("anilistId", 0)
        state_val = elem.get("stateName", "Sconosciuto")
        status = AnimeStatus.UNKNOWN
        if state_val == "Finito":
            status = AnimeStatus.FINISHED
        elif state_val == "In corso":
            status = AnimeStatus.ONGOING
        elif state_val == "Non rilasciato":
            status = AnimeStatus.NOT_RELEASED

        info = {
            "Categoria": "Anime" if elem.get("animeType") == "0" else "Movie",
            "Audio": "Italiano" if elem.get("dub") == "1" else "Giapponese",
            "Data di Uscita": elem.get("release") or elem.get("year", ""),
            "Studio": elem.get("studio", ""),
            "Genere": ", ".join(
                cat.get("name", "") for cat in elem.get("categories", [])
            ),
            "Voto": f"{elem.get('malVote')}" if elem.get("malVote") else "Sconosciuto",
            "Episodi": f"{elem.get('episodes', '0')}",
            "Stato": state_val,
            "Trama": elem.get("story", ""),
        }
        return int(anilist_id) if anilist_id else 0, status, info
