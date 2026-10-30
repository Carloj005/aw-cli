from abc import ABC, abstractmethod
import threading
from httpx import Client, HTTPError  # , AsyncClient
from ..anime import Anime
from .. import utilities as ut


def error_handler(relink=False):
    """
    Decoratore per gestire gli errori delle funzioni.
    """

    def decorator(func):
        def wrapper(self, *args, **kwargs):
            try:
                return func(self, *args, **kwargs)
            except Exception as e:
                if relink and isinstance(e, HTTPError):
                    return update_link(self, func, *args, **kwargs)
                ut.console.print(
                    f"Errore {e.__class__.__name__} durante {func.__name__}: {e}",
                    style="error",
                )
                exit(1)

        return wrapper

    return decorator


def update_link(self, callback, anime: Anime, episode: Anime.Episode | None = None):
    """
    Gestisce il caso in cui il riferimento dell'anime o all'episodio non è più valido
    """
    if episode:
        ut.console.print("Il link dell'episodio è stato cambiato", style="error")
        self.episodes(anime)
        target_ep = anime.episode(episode.num) if anime.has_episode(episode.num) else episode
        return callback(self, anime, target_ep)
    else:
        ut.console.print("Il link dell'anime è stato cambiato", style="warning")
        res: list[Anime] | None = self.search(anime.name)
        if not res:
            ut.console.print(f"Errore o nessun risultato durante la ricerca di {anime.name} su {self.__class__.__name__}", style="error")
            ut.console.print("Cercarlo manualmente", style="highlight")
            exit(1)
        anime.ref = res[0].ref
        if len(anime.episodes()) > 0 and callback.__name__ != "_episodes":
            try:
                anime.update_episodes(
                    self._episodes(anime), ut.config_data["general"].get("specials", False)
                )
            except Exception:
                pass
        return callback(self, anime)


class Provider(ABC):
    """
    Classe astratta che rappresenta un generico Provider.

    Attributes:
        _url (str): l'url del sito del provider.
        _headers (dict): gli headers da utilizzare per le richieste HTTP.
    """

    def __init__(self, url: str, client: Client | None = None):
        """
        Costruisce un'instanza di Provider.

        Args:
            url (str): l'url del sito del provider.
            client (Client | None): client HTTP opzionale da iniettare (utile per testing).
        """
        self.BASE_URL = url
        self._http_lock = threading.RLock()
        self.Client = client or Client(
            headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/109.0.0.0 Safari/537.36",
                "Connection": "close"
            },
            follow_redirects=True,
            timeout=10,
        )

    @error_handler(relink=False)
    def search(self, input: str) -> list[Anime]:
        """
        Ricerca un anime in base al titolo.

        Args:
            input (str): il titolo dell'anime da ricercare.

        Returns:
            list[Anime]: la lista degli anime trovati.
        """
        ut.console.print("Ricerco...", style="warning")
        res = self._search(input)
        for anime in res:
            anime.name = ut.sanitize_filename(anime.name)
        return res

    @abstractmethod
    def _search(self, input: str) -> list[Anime]:
        """
        Implementazione della search
        """

    @error_handler(relink=False)
    def latest(self, filter: str = "all") -> list[Anime]:
        """
        Restituisce le ultime uscite degli anime.
        Si assicura che tutte le entry di uno stesso anime abbiano gli stessi episodi.

        Args:
            filter (str, optional): permette di filtrare i risultati tra [all, dub, sub]. Default: all.

        Returns:
            list[Anime]: la lista degli anime trovati.
        """
        specials = ut.config_data["general"]["specials"]
        animes = self._latest(filter, specials)

        grouped_animes: dict[str, list[Anime]] = {}
        for anime in animes:
            anime.name = ut.sanitize_filename(anime.name)
            grouped_animes.setdefault(anime.name, []).append(anime)

        for group in grouped_animes.values():
            if len(group) > 1:
                all_episodes = {}
                for anime in group:
                    for ep in anime._episodes:
                        all_episodes[ep.num] = ep.ref

                for anime in group:
                    anime.update_episodes(all_episodes, specials=specials)

        return animes

    @abstractmethod
    def _latest(self, filter: str, specials: bool) -> list[Anime]:
        """
        Implementazione della latest
        """

    @error_handler(relink=True)
    def episodes(self, anime: Anime):
        """
        Ottiene i riferimenti agli episodi disponibili dell'anime,
        caricandole dentro `anime` che viene modificato di conseguenza.

        Args:
            anime (Anime): l'anime di riferimento.
        """
        anime.update_episodes(
            self._episodes(anime), ut.config_data["general"]["specials"]
        )

    @abstractmethod
    def _episodes(self, anime: Anime) -> dict[str, str]:
        """
        Ottiene i riferimenti agli episodi disponibili dell'anime.

        Args:
            anime (Anime): l'anime di riferimento.

        Returns:
            dict[str, str]: dizionario dei riferimenti degli episodi dell'anime (numero->URL/ID).
        """

    @error_handler(relink=True)
    def episode_link(self, anime: Anime, episode: Anime.Episode) -> str:
        """
        Cerca il link del video dell'episodio.

        Args:
            episode (Anime.Episode): l'episodio di riferimento.

        Returns:
            str: il link.
        """
        return self._episode_link(anime, episode)

    @error_handler(relink=True)
    def episode_stream_candidates(self, anime: Anime, episode: Anime.Episode) -> list[str]:
        """
        Restituisce la lista ordinata di URL candidati per lo streaming dell'episodio
        (es. HLS 1080p segmentato seguito da fallback MP4 diretto).
        """
        return self._episode_stream_candidates(anime, episode)

    def _episode_stream_candidates(self, anime: Anime, episode: Anime.Episode) -> list[str]:
        """
        Implementazione predefinita dei candidati per lo streaming: usa `_episode_link`.
        """
        return [self._episode_link(anime, episode)]

    @abstractmethod
    def _episode_link(self, anime: Anime, episode: Anime.Episode) -> str:
        """
        Implementazione della episode_link.
        """

    @error_handler(relink=True)
    def info_anime(self, anime: Anime):
        """
        Prende le informazioni dell'anime selezionato,
        caricandole dentro `anime` che viene modificato di conseguenza.

        Args:
            anime (Anime): l'anime di riferimento.
        """
        self._info_anime(anime)

    @abstractmethod
    def _info_anime(self, anime: Anime) -> None:
        """
        Prende le informazioni dell'anime selezionato e le imposta dentro l'anime.

        Args:
            anime (Anime): l'anime di riferimento.
        """
