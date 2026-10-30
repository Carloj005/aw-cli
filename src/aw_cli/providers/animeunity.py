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
from .provider import Provider, HTTPError

class Animeunity(Provider):
    """
    Classe che gestisce il collegamento con AnimeUnity.

    Attributes:
        BASE_URL (str): l'URL del sito di AnimeUnity.
        Client: client HTTP usato per le richieste.
    """
    def __init__(self, client=None):
        self._session_lock = threading.Lock()
        if client is not None:
            base_url = "https://www.animeunity.co"
        else:
            base_url = get_configured_provider_url("animeunity", DEFAULT_ANIMEUNITY_URL)
        super().__init__(base_url, client=client)

    @cached_property
    def _session(self) -> str:
        """Inizializza la sessione (token CSRF, cookies e risoluzione dominio) al primo accesso. Ritorna l'HTML."""
        with self._session_lock:
            canonical_url, html = resolve_animeunity_session(self.Client, self.BASE_URL)
            self.BASE_URL = canonical_url
            csrf_match = re.search(r'<meta.*?name="csrf-token".*?content="([^"]*)".*?>', html)
            if not csrf_match:
                raise ValueError("CSRF token not found in the HTML")
            self.Client.headers['x-csrf-token'] = csrf_match.group(1)
            return html


    def _search(self, input: str) -> list[Anime]:
        _ = self._session  # trigger lazy init
        search_url = f"{self.BASE_URL}/livesearch"
        with self._http_lock:
            response = self.Client.post(
                url=search_url,
                data={"title": input}
            )
        response.raise_for_status()

        results = response.json()['records']
        animes = list[Anime]()
        for result in results:
            title, last_ep, anilist_id, status, info = self._parse_info(result)
            anime = Anime(title, str(result['id']), last_ep=last_ep)
            anime.set_info(anilist_id, status, info)
            animes.append(anime)

        return animes


    def _latest(self, filter, specials) -> list[Anime]:
        regex = re.search(r'<layout-items[^>]*items-json="([^"]*)"', self._session)
        if not regex:
            raise ValueError("Errore nel parsing della pagina principale")

        data_attribute = unescape(regex.group(1))
        json_data = json.loads(data_attribute)['data']
        animes = list[Anime]()
        for data in json_data:
            result = data['anime']
            title, last_ep, anilist_id, status, info = self._parse_info(result)
            if filter == "d" and info["Audio"] != "Italiano":
                continue
            if filter == "s" and info["Audio"] == "Italiano":
                continue
            anime = Anime(title, str(result['id']), curr_ep=str(data['number']), last_ep=last_ep)
            anime.update_episodes({str(data['number']): str(data['id'])}, specials=specials)
            anime.set_info(anilist_id, status, info)
            animes.append(anime)
        return animes

    def _episodes(self, anime: Anime) -> dict[str, str]:
        _ = self._session
        episodes = {}
        start_range = 0
        last_ep_num = int(anime.last_ep) if str(anime.last_ep).isdigit() else 0
        info_ep_num = int(anime.info["Episodi"]) if anime.info.get("Episodi", "").isdigit() else 0
        episode_count = max(last_ep_num, info_ep_num, len(anime._episodes)) # potenzialmente non numerico
        # Fetch episodes in chunks
        while start_range <= episode_count:
            end_range = min(start_range + 119, episode_count)
            search_url = f"{self.BASE_URL}/info_api/{anime.ref}/1"
            with self._http_lock:
                response = self.Client.get(
                    url=search_url,
                    params={
                        "start_range": start_range,
                        "end_range": end_range
                    }
                )
            response.raise_for_status()
            episodes.update({str(episode['number']): str(episode['id']) for episode in response.json()['episodes']})
            start_range = end_range + 1
        return episodes

    def _fetch_embed_page(self, episode: Anime.Episode) -> str:
        """Recupera l'HTML del player VixCloud per l'episodio con retry automatico su errori di rete transitori."""
        _ = self._session
        embed_url = f"{self.BASE_URL}/embed-url/{episode.ref}"
        last_exc: Exception | None = None
        for attempt in range(2):
            try:
                with self._http_lock:
                    response = self.Client.get(embed_url)
                    response.raise_for_status()
                    iframe_src = response.text.strip()
                    iframe_parts = urlsplit(iframe_src)
                    if iframe_parts.scheme in {"http", "https"} and iframe_parts.netloc:
                        video_response = self.Client.get(iframe_src)
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
                master_resp = self.Client.get(master_url)
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
        page = self._fetch_embed_page(episode)
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
        page = self._fetch_embed_page(episode)
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
        search_url = f"{self.BASE_URL}/info_api/{anime.ref}/"
        with self._http_lock:
            response = self.Client.get(search_url)
        response.raise_for_status()
        data = response.json()
        match data['status']:
            case "In Corso":
                anime.status = AnimeStatus.ONGOING
            case "Terminato":
                anime.status = AnimeStatus.FINISHED
            case "Non Rilasciato":
                anime.status = AnimeStatus.NOT_RELEASED
            case _:
                anime.status = AnimeStatus.UNKNOWN
        anime.last_ep = str(data['episodes_count'])
        if anime.info.get("Episodi") in (None, "", "??", "0") and data.get("episodes_count"):
            anime.info["Episodi"] = str(data["episodes_count"])
        anime.info["Genere"] = ', '.join(data['genres'])
        # anime.info["Correlati"] = data['related']

    def _parse_info(self, data: dict) -> tuple[str, str, int, AnimeStatus, dict[str, str]]:
        title = data['title_eng'] or data['title'] or data['title_it']
        last_ep = str(data['real_episodes_count']) if data.get('real_episodes_count') is not None else "0"
        anilist_id = data['anilist_id']
        match data['status']:
            case "In Corso":
                status = AnimeStatus.ONGOING
            case "Terminato":
                status = AnimeStatus.FINISHED
            case "Non Rilasciato":
                status = AnimeStatus.NOT_RELEASED
            case _:
                status = AnimeStatus.UNKNOWN
        info = {
            "Categoria": data['type'],
            "Audio": "Italiano" if data['dub'] else "??",
            "Data di Uscita": data['date'],
            "Stagione": data['season'],
            "Studio": data['studio'],
            "Voto": data['score'],
            "Durata": f"{data['episodes_length']} min", # potrebbe non essere in minuti"
            "Episodi": str(data['episodes_count']) if data['episodes_count'] != 0 else "??",
            "Visualizzazioni": data['visite'],
            "Trama": data['plot'],
        }
        return title, last_ep, anilist_id, status, info
