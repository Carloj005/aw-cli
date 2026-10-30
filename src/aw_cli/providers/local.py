import re
import threading
from pathlib import Path
from .provider import Provider, Anime

class LocalProvider(Provider):
    """
    Provider locale per la gestione degli anime scaricati.
    Permette di cercare, elencare e ottenere informazioni sugli anime presenti localmente.
    - search: cerca tra gli anime scaricati
    - latest: non implementata
    - episodes: carica gli episodi scaricati
    - episode_link: restituisce il path locale
    - info_anime: carica info dalla cronologia (o valori predefiniti se assente)
    """
    def __init__(self, path, history: list[Anime]):
        self.BASE_URL = path
        self.history = history
        self._http_lock = threading.RLock()

    def _search(self, input: str) -> list[Anime]:
        """Restituisce gli anime scaricati localmente, ignorando l'input di ricerca."""
        animes = []
        base_path = Path(self.BASE_URL)
        if not base_path.exists():
            return []

        for item in base_path.iterdir():
            if not item.is_dir():
                continue
            name = item.name
            anime = Anime(name, "local")
            anime.update_episodes(self._episodes(anime), specials=True)
            if not anime.episodes():
                continue
            self.info_anime(anime)
            if anime in self.history:
                other = self.history[self.history.index(anime)]
                for ep_num in anime.episodes():
                    if other.has_episode(ep_num):
                        src_ep = other.episode(ep_num)
                        dst_ep = anime.episode(ep_num)
                        dst_ep.progress = src_ep.progress
                        dst_ep.completed = src_ep.completed
                anime.curr_ep = other.curr_ep if anime.has_episode(other.curr_ep) else anime.episodes()[0]
            else:
                anime.curr_ep = anime.episodes()[0]
            anime.last_ep = anime.episodes()[-1]
            animes.append(anime)
        return animes

    def _latest(self, filter: str, specials: bool) -> list[Anime]:
        for anime in (res := self.search("")):
            if anime.episodes():
                anime.curr_ep = anime.episodes()[-1]
        return res

    def _episodes(self, anime: Anime) -> dict[str, str]:
        base_path = Path(self.BASE_URL) / anime.name
        if not base_path.exists() or not base_path.is_dir():
            return {}

        episodes = dict[str, str]()
        for entry in base_path.iterdir():
            if not entry.is_file() or entry.suffix.lower() != ".mp4":
                continue
            match = re.search(r"Ep\.\s*([^/\\]+)\.mp4$", entry.name, re.IGNORECASE)
            if match:
                num = match.group(1).strip()
                if num:
                    episodes[num] = entry.name
        return episodes

    def _episode_link(self, anime: Anime, episode: Anime.Episode) -> str:
        return str(Path(self.BASE_URL) / anime.name / episode.ref)

    def _info_anime(self, anime: Anime):
        if anime in self.history:
            other = self.history[self.history.index(anime)]
            anime.ref = other.ref
            anime.set_info(other.anilist_id, other.status, other.info)
        else:
            anime.set_info(0, anime.status, {"Episodi": str(len(anime.episodes()))})
