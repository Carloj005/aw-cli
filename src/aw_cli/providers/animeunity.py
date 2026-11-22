import re
import json
import threading
from time import sleep
from html import unescape
from functools import cached_property
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from ..anime import Anime, AnimeStatus
from ..domain_resolver import (
    DEFAULT_ANIMEUNITY_URL,
    get_configured_provider_url,
    resolve_animeunity_session,
)
from ..search_utils import (
    desanitize_search_query,
    generate_fallback_queries,
    rank_fuzzy_candidates,
    sort_animeunity_records,
)
from .provider import Provider, HTTPError


_SEASON_LABELS: dict[str, str] = {
    "winter": "Inverno",
    "spring": "Primavera",
    "summer": "Estate",
    "fall": "Autunno",
    "autumn": "Autunno",
    "inverno": "Inverno",
    "primavera": "Primavera",
    "estate": "Estate",
    "autunno": "Autunno",
}

_TYPE_LABELS: dict[str, str] = {
    "tv": "TV",
    "movie": "Movie",
    "ova": "OVA",
    "ona": "ONA",
    "tv_short": "TV Short",
    "special": "Special",
}

_LANGUAGE_LABELS: dict[str, str] = {
    "ita": "Italiano",
    "jpn": "Giapponese",
    "chn": "Cinese",
    "kor": "Coreano",
    "eng": "Inglese",
}


def _map_status(raw_status: object) -> AnimeStatus:
    val = str(raw_status or "").strip().lower()
    if val in ("ongoing", "in corso"):
        return AnimeStatus.ONGOING
    if val in ("completed", "terminato", "finito"):
        return AnimeStatus.FINISHED
    if val in ("upcoming", "not_yet_released", "non rilasciato"):
        return AnimeStatus.NOT_RELEASED
    return AnimeStatus.UNKNOWN


class Animeunity(Provider):
    """
    Classe che gestisce il collegamento con il nuovo AnimeUnity (https://animeunity.cx,
    architettura Laravel + Inertia.js + API REST `/api/*`).

    Attributes:
        BASE_URL (str): l'URL del sito di AnimeUnity.
        Client: client HTTP usato per le richieste.
    """

    def __init__(self, client=None):
        self._session_lock = threading.Lock()
        self._inertia_version: str = ""
        if client is not None:
            base_url = DEFAULT_ANIMEUNITY_URL
        else:
            base_url = get_configured_provider_url("animeunity", DEFAULT_ANIMEUNITY_URL)
        super().__init__(base_url, client=client)

    @staticmethod
    def _extract_inertia_page(html: str) -> dict | None:
        if not isinstance(html, str) or "data-page=" not in html:
            return None
        match = re.search(r'data-page="([^"]+)"', html)
        if not match:
            return None
        try:
            data = json.loads(unescape(match.group(1)))
            return data if isinstance(data, dict) else None
        except (ValueError, json.JSONDecodeError):
            return None

    @cached_property
    def _session(self) -> str:
        """Inizializza la sessione (risoluzione dominio, cookie e versione Inertia) al primo accesso."""
        with self._session_lock:
            canonical_url, html = resolve_animeunity_session(self.Client, self.BASE_URL)
            self.BASE_URL = canonical_url
            page = self._extract_inertia_page(html)
            if page and page.get("version"):
                self._inertia_version = str(page["version"])
            csrf_match = re.search(r'<meta.*?name="csrf-token".*?content="([^"]*)".*?>', html)
            if csrf_match:
                self.Client.headers["x-csrf-token"] = csrf_match.group(1)
            return html

    def _api_headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json, text/plain, */*",
            "Referer": f"{self.BASE_URL}/",
        }

    def _refresh_inertia_version(self) -> None:
        """Aggiorna la versione Inertia in caso di deploy frontend (HTTP 409)."""
        try:
            with self._http_lock:
                resp = self.Client.get(
                    f"{self.BASE_URL}/",
                    headers={"Accept": "text/html,application/xhtml+xml"},
                )
            page = self._extract_inertia_page(getattr(resp, "text", ""))
            if page and page.get("version"):
                self._inertia_version = str(page["version"])
        except Exception:
            pass

    def _inertia_get(
        self,
        path: str,
        component: str,
        partial_data: list[str] | None = None,
    ) -> dict:
        """
        Esegue una richiesta Inertia (partial reload se la versione è nota, con retry
        automatico su 409 e fallback parsing HTML `data-page`).
        """
        _ = self._session
        url = f"{self.BASE_URL}{path}"
        if not self._inertia_version:
            page_from_sess = self._extract_inertia_page(self._session)
            if page_from_sess and page_from_sess.get("version"):
                self._inertia_version = str(page_from_sess["version"])

        for attempt in range(2):
            headers = {
                "Referer": f"{self.BASE_URL}/",
                "Accept": "application/json, text/html;q=0.9",
            }
            if self._inertia_version:
                headers["X-Inertia"] = "true"
                headers["X-Inertia-Version"] = self._inertia_version
                headers["X-Requested-With"] = "XMLHttpRequest"
                if partial_data:
                    headers["X-Inertia-Partial-Component"] = component
                    headers["X-Inertia-Partial-Data"] = ",".join(partial_data)

            with self._http_lock:
                resp = self.Client.get(url, headers=headers)

            status = getattr(resp, "status_code", 200)
            if status == 409 and attempt == 0:
                self._refresh_inertia_version()
                continue

            resp.raise_for_status()
            resp_headers = getattr(resp, "headers", {}) or {}
            content_type = str(
                resp_headers.get("content-type") or resp_headers.get("Content-Type") or ""
            ).lower()
            text = getattr(resp, "text", "")

            if "json" in content_type or (isinstance(text, str) and text.lstrip().startswith("{")):
                data = resp.json()
                if isinstance(data, dict):
                    if data.get("version"):
                        self._inertia_version = str(data["version"])
                    return data

            page = self._extract_inertia_page(text)
            if page is not None:
                if page.get("version"):
                    self._inertia_version = str(page["version"])
                return page

            raise ValueError(f"Risposta Inertia non valida per {path}")

        raise HTTPError(f"Errore Inertia 409 persistente per {path}")

    @staticmethod
    def _expand_record_variants(rec: dict) -> list[dict]:
        """
        Il nuovo AnimeUnity unifica le lingue (es. jpn + ita) sotto lo stesso record tramite `variants`.
        Espande le varianti in voci separate SUB e `(ITA)` così che l'utente possa scegliere lingua
        e cronologia in modo coerente con `aw-cli` e AniList.
        """
        variants = rec.get("variants")
        if not isinstance(variants, list) or not variants:
            return [rec]

        base_title = str(rec.get("title") or rec.get("title_eng") or rec.get("title_it") or "Anime").strip()
        ita_variant = next(
            (v for v in variants if isinstance(v, dict) and str(v.get("language", "")).lower() == "ita"),
            None,
        )
        non_ita_variants = [
            v for v in variants if isinstance(v, dict) and str(v.get("language", "")).lower() != "ita"
        ]
        sub_variant = (
            next((v for v in non_ita_variants if str(v.get("language", "")).lower() == "jpn"), None)
            or (non_ita_variants[0] if non_ita_variants else None)
        )

        expanded: list[dict] = []
        if sub_variant is not None:
            sub_rec = dict(rec)
            sub_rec["title"] = base_title
            sub_rec["dub"] = 0
            sub_rec["_variant_lang"] = str(sub_variant.get("language") or "jpn").lower()
            sub_rec["_variant_status"] = sub_variant.get("status")
            sub_rec["_variant_episodes_count"] = sub_variant.get("episodes_count")
            expanded.append(sub_rec)

        if ita_variant is not None:
            ita_rec = dict(rec)
            ita_title = base_title if "(ita)" in base_title.lower() else f"{base_title} (ITA)"
            ita_rec["title"] = ita_title
            ita_rec["dub"] = 1
            ita_rec["_variant_lang"] = "ita"
            ita_rec["_variant_status"] = ita_variant.get("status")
            ita_rec["_variant_episodes_count"] = ita_variant.get("episodes_count")
            expanded.append(ita_rec)

        return expanded or [rec]

    def _query_records(self, query: str) -> list[dict]:
        """
        Interroga `/api/archive?query=...` (con paginazione completa fino a 120 anime)
        e `/api/search?q=...`, unendo e deduplicando i record per ID.
        """
        merged: list[dict] = []
        seen_ids: set[str] = set()

        def _collect(items: object) -> None:
            if not isinstance(items, list):
                return
            for item in items:
                if isinstance(item, dict) and item.get("id") is not None:
                    rec_id = str(item["id"])
                    if rec_id not in seen_ids:
                        seen_ids.add(rec_id)
                        merged.append(item)

        headers = self._api_headers()
        archive_url = f"{self.BASE_URL}/api/archive"
        try:
            for page_num in range(1, 5):
                with self._http_lock:
                    arch_resp = self.Client.get(
                        url=archive_url,
                        params={"query": query, "page": page_num},
                        headers=headers,
                    )
                arch_resp.raise_for_status()
                arch_data = arch_resp.json()
                if not isinstance(arch_data, dict):
                    break
                page_records = arch_data.get("data")
                _collect(page_records)
                last_page = arch_data.get("last_page")
                per_page = arch_data.get("per_page") or 30
                if (
                    not isinstance(page_records, list)
                    or len(page_records) < per_page
                    or (isinstance(last_page, int) and page_num >= last_page)
                ):
                    break
        except Exception:
            pass

        search_url = f"{self.BASE_URL}/api/search"
        try:
            with self._http_lock:
                response = self.Client.get(
                    url=search_url,
                    params={"q": query},
                    headers=headers,
                )
            response.raise_for_status()
            search_data = response.json()
            if isinstance(search_data, dict):
                _collect(search_data.get("data", []))
            elif isinstance(search_data, list):
                _collect(search_data)
        except Exception:
            if not merged:
                raise

        expanded: list[dict] = []
        for rec in merged:
            expanded.extend(self._expand_record_variants(rec))
        return expanded

    def _record_to_anime(self, result: dict) -> Anime:
        title, last_ep, anilist_id, status, info = self._parse_info(result)
        anime = Anime(title, str(result["id"]), last_ep=last_ep)
        anime.set_info(anilist_id, status, info)
        return anime

    @staticmethod
    def _record_aliases(result: dict) -> list[str | None]:
        slug = result.get("slug")
        return [
            result.get("title"),
            result.get("title_eng"),
            result.get("title_ja"),
            result.get("title_it"),
            slug.replace("-", " ") if isinstance(slug, str) else None,
        ]

    def _search(self, input: str) -> list[Anime]:
        query = desanitize_search_query(input.strip())
        if not query:
            return []

        _ = self._session  # trigger lazy init
        records = self._query_records(query)
        if records:
            ordered = sort_animeunity_records(records, query)
            return [self._record_to_anime(result) for result in ordered]

        # Fallback multi-token / fuzzy per tollerare refusi (es. 'steal' vs 'steel')
        # e differenze di punteggiatura/ordine parole nel titolo.
        fallback_records: list[dict] = []
        seen_keys: set[tuple[str, str]] = set()
        for sub_query in generate_fallback_queries(query):
            try:
                sub_results = self._query_records(sub_query)
            except Exception:
                continue
            for rec in sub_results:
                rec_key = (str(rec["id"]), str(rec.get("_variant_lang") or rec.get("dub") or ""))
                if rec_key not in seen_keys:
                    seen_keys.add(rec_key)
                    fallback_records.append(rec)

        if not fallback_records:
            return []

        ordered_fallback = sort_animeunity_records(fallback_records, query)
        candidates = [
            (self._record_to_anime(rec), self._record_aliases(rec))
            for rec in ordered_fallback
        ]
        return rank_fuzzy_candidates(query, candidates)

    def _latest(self, filter, specials) -> list[Anime]:
        _ = self._session
        filter_key = (filter or "a")[0].lower()
        headers = self._api_headers()

        if filter_key == "t":
            # Tendenze: usa la classifica ufficiale `/api/top-anime` (con fallback a `topAnimes` home)
            top_records: list[dict] = []
            try:
                with self._http_lock:
                    resp = self.Client.get(
                        url=f"{self.BASE_URL}/api/top-anime",
                        params={"page": 1},
                        headers=headers,
                    )
                resp.raise_for_status()
                payload = resp.json()
                if isinstance(payload, dict) and isinstance(payload.get("data"), list):
                    top_records = payload["data"]
            except Exception:
                page = self._extract_inertia_page(self._session)
                top_map = (page.get("props") or {}).get("topAnimes") if page else None
                if isinstance(top_map, dict):
                    top_records = list(top_map.get("day") or top_map.get("week") or [])

            if top_records:
                animes: list[Anime] = []
                for rec in top_records:
                    if not isinstance(rec, dict) or rec.get("id") is None:
                        continue
                    # Per le tendenze mostra la variante principale (preferendo SUB, altrimenti ITA)
                    expanded = self._expand_record_variants(rec)
                    primary = expanded[0]
                    title, last_ep, anilist_id, status, info = self._parse_info(primary)
                    curr_ep = last_ep if last_ep and last_ep != "0" else "1"
                    anime = Anime(title, str(rec["id"]), curr_ep=curr_ep, last_ep=last_ep)
                    anime.set_info(anilist_id, status, info)
                    animes.append(anime)
                if animes:
                    return animes

        # Ultimi episodi rilasciati: `/api/home/latest-episodes` (supporta `language=sub|dub`)
        items: list[dict] = []
        params_base: dict[str, object] = {}
        if filter_key == "s":
            params_base["language"] = "sub"
        elif filter_key == "d":
            params_base["language"] = "dub"

        try:
            for page_num in (1, 2):
                params = {**params_base, "page": page_num}
                with self._http_lock:
                    resp = self.Client.get(
                        url=f"{self.BASE_URL}/api/home/latest-episodes",
                        params=params,
                        headers=headers,
                    )
                resp.raise_for_status()
                data = resp.json()
                page_items = data.get("data") if isinstance(data, dict) else None
                if not isinstance(page_items, list) or not page_items:
                    break
                items.extend(page_items)
                last_page = data.get("last_page") if isinstance(data, dict) else None
                if isinstance(last_page, int) and page_num >= last_page:
                    break
        except Exception:
            page = self._extract_inertia_page(self._session)
            latest_prop = (page.get("props") or {}).get("latestEpisodes") if page else None
            if isinstance(latest_prop, dict) and isinstance(latest_prop.get("data"), list):
                items = latest_prop["data"]
            else:
                raise ValueError("Errore nel recupero degli ultimi episodi da AnimeUnity")

        if filter_key == "t":
            items = sorted(
                items,
                key=lambda it: int(((it.get("episode") or {}).get("anime") or {}).get("views") or 0),
                reverse=True,
            )

        animes = list[Anime]()
        seen_trending: set[tuple[str, bool]] = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            ep_obj = item.get("episode") or {}
            anime_obj = ep_obj.get("anime") or {}
            if not anime_obj or anime_obj.get("id") is None:
                continue

            ep_num = str(ep_obj.get("number") or "0")
            if not specials and ("." in ep_num or ep_num == "0"):
                continue

            lang = str(item.get("language") or "jpn").lower()
            is_dub = lang == "ita"
            if filter_key == "d" and not is_dub:
                continue
            if filter_key == "s" and is_dub:
                continue

            if filter_key == "t":
                key = (str(anime_obj["id"]), is_dub)
                if key in seen_trending:
                    continue
                seen_trending.add(key)

            rec = dict(anime_obj)
            base_title = str(rec.get("title") or rec.get("title_eng") or "Anime").strip()
            rec["title"] = (
                base_title
                if (not is_dub or "(ita)" in base_title.lower())
                else f"{base_title} (ITA)"
            )
            rec["dub"] = 1 if is_dub else 0
            rec["_variant_lang"] = lang
            # Estrae lo stato/conteggio dalla variante corrispondente se presente
            for var in rec.get("variants") or []:
                if isinstance(var, dict) and str(var.get("language", "")).lower() == lang:
                    rec["_variant_status"] = var.get("status")
                    rec["_variant_episodes_count"] = var.get("episodes_count")
                    break

            title, last_ep, anilist_id, status, info = self._parse_info(rec)
            anime = Anime(title, str(anime_obj["id"]), curr_ep=ep_num, last_ep=last_ep)
            anime.update_episodes({ep_num: str(item["id"])}, specials=specials)
            anime.set_info(anilist_id, status, info)
            animes.append(anime)
        return animes

    def _resolve_canonical_anime_id(self, anime: Anime) -> str:
        """
        Risolve un eventuale vecchio ID anime (es. vecchio ID separato `(ITA)` in `history.json`)
        nel nuovo ID unificato di AnimeUnity tramite redirect `/anime/{ref}-x`.
        """
        page = self._inertia_get(f"/anime/{anime.ref}-x", component="Anime", partial_data=["anime"])
        anime_data = (page.get("props") or {}).get("anime")
        if isinstance(anime_data, dict) and anime_data.get("id") is not None:
            anime.ref = str(anime_data["id"])
        return anime.ref

    @staticmethod
    def _pick_episode_video(videos: list[dict], want_dub: bool) -> dict | None:
        visible = [v for v in videos if isinstance(v, dict) and not v.get("is_hidden")]
        if not visible:
            visible = [v for v in videos if isinstance(v, dict)]
        if not visible:
            return None

        if want_dub:
            return next(
                (v for v in visible if str(v.get("language", "")).lower() == "ita"),
                None,
            )

        non_ita = [v for v in visible if str(v.get("language", "")).lower() != "ita"]
        if non_ita:
            return (
                next((v for v in non_ita if str(v.get("language", "")).lower() == "jpn"), None)
                or non_ita[0]
            )
        return None

    def _episodes(self, anime: Anime) -> dict[str, str]:
        _ = self._session
        if not anime.ref.isdigit():
            raise HTTPError("Errore 404: pagina non trovata")

        headers = self._api_headers()
        episodes: dict[str, str] = {}
        start: int | None = None
        seen_starts: set[int] = set()
        retried_canonical = False

        while True:
            url = f"{self.BASE_URL}/api/anime/{anime.ref}/episodes"
            params = {"start": start} if start is not None else None
            with self._http_lock:
                response = self.Client.get(url=url, params=params, headers=headers)

            if getattr(response, "status_code", 200) == 404 and not retried_canonical:
                retried_canonical = True
                prev_ref = anime.ref
                new_ref = self._resolve_canonical_anime_id(anime)
                if new_ref != prev_ref:
                    continue

            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                break

            raw_episodes = payload.get("episodes")
            if not isinstance(raw_episodes, list) or not raw_episodes:
                break

            added_any = False
            for ep in raw_episodes:
                if not isinstance(ep, dict):
                    continue
                ep_num = str(ep.get("number") or "").strip()
                if not ep_num:
                    continue
                videos = ep.get("videos")
                if isinstance(videos, list):
                    chosen = self._pick_episode_video(videos, want_dub=anime.dub)
                    if chosen and chosen.get("id") is not None:
                        if ep_num not in episodes:
                            added_any = True
                        episodes[ep_num] = str(chosen["id"])
                elif ep.get("id") is not None:
                    if ep_num not in episodes:
                        added_any = True
                    episodes[ep_num] = str(ep["id"])

            cur_start = int(payload.get("start") or 1)
            tab_size = int(payload.get("tab_size") or 120)
            total = int(payload.get("total") or 0)
            offset = int(payload.get("offset") or 1)
            seen_starts.add(cur_start)

            next_start = cur_start + tab_size
            max_start = total + offset - 1 if offset <= 1 else total
            if tab_size <= 0 or next_start > max_start or next_start in seen_starts or (start is not None and not added_any):
                break
            start = next_start

        return episodes

    def _fetch_embed_page(self, anime: Anime, episode: Anime.Episode) -> str:
        """
        Recupera l'embedUrl VixCloud tramite partial reload Inertia su `/anime/{anime.ref}-x/{episode.ref}`
        e scarica l'HTML del player VixCloud con `Referer` corretto e retry automatico.
        """
        _ = self._session
        last_exc: Exception | None = None
        for attempt in range(2):
            try:
                page_data = self._inertia_get(
                    f"/anime/{anime.ref}-x/{episode.ref}",
                    component="Anime",
                    partial_data=["embedUrl", "video"],
                )
                props = page_data.get("props") or {}
                iframe_src = str(props.get("embedUrl") or "").strip()
                if not iframe_src:
                    raise ValueError("embedUrl non trovato nella risposta di AnimeUnity")

                iframe_parts = urlsplit(iframe_src)
                if iframe_parts.scheme in {"http", "https"} and iframe_parts.netloc:
                    with self._http_lock:
                        video_response = self.Client.get(
                            iframe_src,
                            headers={"Referer": f"{self.BASE_URL}/"},
                        )
                    video_response.raise_for_status()
                    page = video_response.text
                    if "canPlayFHD=1" in iframe_parts.query and "canPlayFHD" not in page:
                        page += "\nwindow.canPlayFHD = true;\n"
                    return page
                return iframe_src
            except (HTTPError, OSError) as exc:
                last_exc = exc
                if attempt == 0:
                    sleep(0.4)
        assert last_exc is not None
        raise last_exc

    @staticmethod
    def _extract_direct_url(page: str) -> str | None:
        direct = re.search(r"window\.downloadUrl\s*=\s*(['\"])(.*?)\1", page, re.DOTALL)
        if direct and direct.group(2).strip():
            direct_url = direct.group(2).strip()
            direct_parts = urlsplit(direct_url)
            if direct_parts.scheme in {"http", "https"} and direct_parts.netloc:
                return direct_url
        return None

    @staticmethod
    def _extract_hls_master_url(page: str) -> str:
        playlist = re.search(r"window\.masterPlaylist\s*=\s*\{", page)
        if not playlist:
            raise ValueError("Link video e playlist HLS non trovati nella pagina")

        player_data = page[playlist.end():]
        params = re.search(r"\bparams\s*:\s*\{([^}]*)\}", player_data)
        url = re.search(r"\burl\s*:\s*(['\"])(.*?)\1", player_data)
        if not params or not url or not url.group(2).strip():
            raise ValueError("Playlist HLS incompleta nella pagina")

        def parameter(name: str) -> str | None:
            match = re.search(rf"(?:['\"]?{name}['\"]?)\s*:\s*(['\"])(.*?)\1", params.group(1))
            return match.group(2) if match else None

        token, expires = parameter("token"), parameter("expires")
        if not token or not expires:
            raise ValueError("Token o scadenza della playlist HLS mancanti")

        parts = urlsplit(url.group(2).strip())
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise ValueError("URL della playlist HLS non valido")

        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query.update(token=token, expires=expires)
        asn = parameter("asn")
        if asn:
            query["asn"] = asn
        if re.search(r"window\.canPlayFHD\s*=\s*(?:true|1)\b", page, re.IGNORECASE):
            query["h"] = "1"
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))

    def _resolve_best_hls_variant(self, master_url: str) -> str | None:
        """
        Risolve direttamente la variante HLS alla massima qualità possibile
        (ordinando per risoluzione in pixel e poi per BANDWIDTH, es. 1920x1080 FHD)
        per garantire sempre lo streaming alla qualità massima ed eliminare il probing multi-variante.
        """
        try:
            with self._http_lock:
                master_resp = self.Client.get(
                    master_url,
                    headers={"Referer": "https://vixcloud.cc/"},
                )
            master_resp.raise_for_status()
            text = getattr(master_resp, "text", "")
            if not isinstance(text, str) or not text.lstrip().startswith("#EXTM3U"):
                return None
            lines = text.splitlines()
            if any(line.startswith("#EXT-X-MEDIA:") for line in lines):
                return None
            variants: list[tuple[int, int, str]] = []
            base_url = str(getattr(master_resp, "url", master_url) or master_url)
            for idx, line in enumerate(lines[:-1]):
                if line.startswith("#EXT-X-STREAM-INF"):
                    res_match = re.search(r"\bRESOLUTION=(\d+)x(\d+)", line)
                    pixels = (int(res_match.group(1)) * int(res_match.group(2))) if res_match else 0
                    bw = re.search(r"\bBANDWIDTH=(\d+)", line)
                    bandwidth = int(bw.group(1)) if bw else 0
                    variant = lines[idx + 1].strip()
                    if variant and not variant.startswith("#"):
                        variants.append((pixels, bandwidth, urljoin(base_url, variant)))
            return max(variants)[2] if variants else None
        except Exception:
            return None

    def _episode_link(self, anime: Anime, episode: Anime.Episode) -> str:
        page = self._fetch_embed_page(anime, episode)
        direct_url = self._extract_direct_url(page)
        if direct_url:
            return direct_url
        return self._extract_hls_master_url(page)

    def _episode_stream_candidates(self, anime: Anime, episode: Anime.Episode) -> list[str]:
        """
        Per lo streaming in mpv/VLC preferisce lo stream HLS 1080p segmentato su CDN streaming
        (immune al limite di 2 connessioni simultanee `HTTP 503` dei server `/download/` durante skip rapidi),
        mantenendo il master HLS e l'MP4 diretto come fallback automatici.
        """
        page = self._fetch_embed_page(anime, episode)
        candidates: list[str] = []

        hls_error: ValueError | None = None
        try:
            hls_master = self._extract_hls_master_url(page)
            best_variant = self._resolve_best_hls_variant(hls_master)
            if best_variant:
                candidates.append(best_variant)
            if hls_master not in candidates:
                candidates.append(hls_master)
        except ValueError as exc:
            hls_error = exc

        direct_url = self._extract_direct_url(page)
        if direct_url and direct_url not in candidates:
            candidates.append(direct_url)

        if not candidates:
            if hls_error:
                raise hls_error
            raise ValueError("Link video e playlist HLS non trovati nella pagina")

        return candidates

    def _info_anime(self, anime: Anime):
        _ = self._session
        if not anime.ref.isdigit():
            raise HTTPError("Errore 404: pagina non trovata")

        page = self._inertia_get(
            f"/anime/{anime.ref}-x",
            component="Anime",
            partial_data=["anime"],
        )
        data = (page.get("props") or {}).get("anime")
        if not isinstance(data, dict):
            raise ValueError("Informazioni anime non trovate nella pagina")

        if data.get("id") is not None:
            anime.ref = str(data["id"])

        rec = dict(data)
        variants = rec.get("variants") if isinstance(rec.get("variants"), list) else []
        chosen_variant = None
        if anime.dub:
            chosen_variant = next(
                (v for v in variants if isinstance(v, dict) and str(v.get("language", "")).lower() == "ita"),
                None,
            )
            rec["dub"] = 1
            rec["_variant_lang"] = "ita"
        else:
            non_ita = [v for v in variants if isinstance(v, dict) and str(v.get("language", "")).lower() != "ita"]
            chosen_variant = (
                next((v for v in non_ita if str(v.get("language", "")).lower() == "jpn"), None)
                or (non_ita[0] if non_ita else None)
            )
            rec["dub"] = 0
            if chosen_variant:
                rec["_variant_lang"] = str(chosen_variant.get("language") or "jpn").lower()

        if chosen_variant:
            rec["_variant_status"] = chosen_variant.get("status")
            rec["_variant_episodes_count"] = chosen_variant.get("episodes_count")

        _, last_ep, anilist_id, status, parsed_info = self._parse_info(rec)
        if last_ep and last_ep != "0":
            anime.last_ep = last_ep
        merged_info = dict(anime.info)
        for k, v in parsed_info.items():
            if v not in (None, "", "??") or k not in merged_info:
                merged_info[k] = v
        if anime.dub:
            merged_info["Audio"] = "Italiano"
        anime.set_info(anilist_id or anime.anilist_id, status, merged_info)

    def _parse_info(self, data: dict) -> tuple[str, str, int, AnimeStatus, dict[str, str]]:
        title = (
            data.get("title_eng")
            or data.get("title")
            or data.get("title_it")
            or "Anime"
        )

        # Determina il numero di episodi rilasciati (variante selezionata -> real_episodes_count -> variants max)
        released_eps = data.get("_variant_episodes_count")
        if released_eps is None:
            released_eps = data.get("real_episodes_count")
        if released_eps is None and isinstance(data.get("variants"), list) and data["variants"]:
            counts = [
                v.get("episodes_count")
                for v in data["variants"]
                if isinstance(v, dict) and v.get("episodes_count") is not None
            ]
            if counts:
                released_eps = max(counts)
        last_ep = str(released_eps) if released_eps is not None else "0"

        raw_anilist = data.get("anilist_id")
        try:
            anilist_id = int(raw_anilist) if raw_anilist else 0
        except (TypeError, ValueError):
            anilist_id = 0

        raw_status = (
            data.get("_variant_status")
            or data.get("status")
            or data.get("default_status")
        )
        status = _map_status(raw_status)

        variant_lang = str(data.get("_variant_lang") or "").lower()
        if data.get("dub") or variant_lang == "ita" or "(ita)" in str(title).lower():
            audio_label = "Italiano"
        elif variant_lang:
            audio_label = _LANGUAGE_LABELS.get(variant_lang, variant_lang.upper())
        else:
            audio_label = "Giapponese"

        raw_type = str(data.get("type") or "").strip()
        type_label = _TYPE_LABELS.get(raw_type.lower(), raw_type) if raw_type else "??"

        raw_season = str(data.get("season") or "").strip()
        season_label = _SEASON_LABELS.get(raw_season.lower(), raw_season) if raw_season else "??"

        raw_year = data.get("year") if data.get("year") is not None else data.get("date")
        date_label = str(raw_year) if raw_year not in (None, "") else "??"

        raw_studios = data.get("studios")
        if isinstance(raw_studios, list) and raw_studios:
            studio_label = ", ".join(
                s.get("name", "") if isinstance(s, dict) else str(s)
                for s in raw_studios
                if s
            ) or "??"
        else:
            studio_label = str(data.get("studio") or "??")

        ep_cnt = data.get("total_episodes")
        if ep_cnt in (None, 0, "0", ""):
            ep_cnt = data.get("episodes_count")
        ep_len = data.get("episode_runtime")
        if ep_len is None:
            ep_len = data.get("episodes_length")
        views = data.get("views")
        if views is None:
            views = data.get("visite")

        info = {
            "Categoria": type_label,
            "Audio": audio_label,
            "Data di Uscita": date_label,
            "Stagione": season_label,
            "Studio": studio_label,
            "Voto": str(data["score"]) if data.get("score") is not None else "??",
            "Durata": f"{ep_len} min" if ep_len is not None else "??",
            "Episodi": str(ep_cnt) if ep_cnt not in (None, 0, "0", "") else "??",
            "Visualizzazioni": str(views) if views is not None else "??",
            "Trama": data.get("plot") or "",
        }

        raw_genres = data.get("genres")
        if isinstance(raw_genres, list) and raw_genres:
            info["Genere"] = ", ".join(
                g.get("name", "") if isinstance(g, dict) else str(g)
                for g in raw_genres
                if g
            )
        return title, last_ep, anilist_id, status, info
