import json
import threading
from pathlib import Path
from . import utilities as ut
from .anime import Anime, AnimeStatus

_SAVE_LOCK = threading.Lock()

class History:

    """
    Classe che gestisce la cronologia degli anime.
    """
    def __init__(self, path: str = "", anime_log: list[Anime] | None = None):
        self._path = path
        self._anime_log = anime_log if anime_log is not None else []

    @classmethod
    def read(cls, path: str) -> "History":
        """
        Legge la cronologia da un file json.

        Args:
            path (str): il percorso della cartella contenente il file history.json.

        Returns:
            History: l'oggetto History con i dati letti dal file.
        """
        anime_log = []
        history_path = Path(path) / "history.json"
        try:
            with open(history_path, encoding='utf-8') as file:
                anime_log = [Anime.from_dict(entry) for entry in json.load(file)]
        except (FileNotFoundError, json.JSONDecodeError):
            anime_log = legacy(path)

        return cls(str(history_path), anime_log)

    def get(self) -> list[Anime]:
        """
        Prende i dati dalla cronologia.

        Returns:
            list[Anime]: la lista degli anime trovati

        """
        return self._anime_log

    def remove(self, Anime: Anime) -> None:
        """
        Rimuove un anime dalla cronologia.

        Args:
            Anime (Anime): l'anime da rimuovere.
        """
        self._anime_log.remove(Anime)
        self.save()

    def has_ongoing(self) -> bool:
        return AnimeStatus.ONGOING in [anime.status for anime in self._anime_log]

    def reload(self, last_releases: list[Anime]) -> None:
        """
        Aggiorna la cronologia degli anime con le ultime uscite disponibili.

        Questa funzione esamina ciascun anime nella cronologia e verifica se sono disponibili nuove uscite.
        Se trova nuove uscite per un anime, ne aggiorna lo stato.

        Args:
            last_releases (list[Anime]): La lista degli ultimi anime rilasciati.
        """

        for anime in reversed(self._anime_log):
            for anime_latest in last_releases:
                if anime == anime_latest and anime.last_ep != anime_latest.last_ep:
                    same_provider_scheme = anime.ref.isdigit() == anime_latest.ref.isdigit()
                    if same_provider_scheme:
                        anime.update_episodes({
                            num: anime_latest.episode(num).ref
                            for num in anime_latest.episodes()
                        })
                    else:
                        anime.last_ep = anime_latest.last_ep
                    break
        self.save()

    def update(self, anime: Anime, episode: Anime.Episode):
        """
        Aggiorna la cronologia.

        Args:
            anime (Anime): l'anime da aggiornare.
            episode (Anime.Episode): l'episodio da aggiornare.
        """
        self._anime_log.remove(anime) if anime in self._anime_log else None
        last_completed = episode.is_completed() and episode.num == anime.last_ep
        if anime.status == AnimeStatus.FINISHED and last_completed:
            self.save()
            return

        if last_completed:
            self._anime_log.append(anime)
        else:
            self._anime_log.insert(0, anime)

        self.save()

    def save(self) -> None:
        """
        Salva la cronologia su un file JSON in modo thread-safe.
        """
        if not self._path:
            return
        with _SAVE_LOCK:
            payload = json.dumps(
                [anime.to_dict() for anime in self._anime_log],
                ensure_ascii=False,
                indent=4,
            )
            with open(self._path, 'w', encoding='utf-8') as file:
                file.write(payload)

def legacy(base_dir: str = "") -> list[Anime]:
    """
    Legge i dati dalla vecchia cronologia in csv
    """
    import csv
    legacy = []
    csv_path = (Path(base_dir) / "aw-cronologia.csv") if base_dir else (Path(__file__).parent / "aw-cronologia.csv")
    try:
        with open(csv_path, encoding='utf-8') as file:
            legacy = [riga for riga in csv.reader(file)]
    except FileNotFoundError:
        pass
    if not legacy:
        return []

    animes = []
    status_list = list(AnimeStatus)
    for row in legacy:
        if not row or not row[0].strip():
            continue
        if len(row) < 4:
            row.append("??")
        if len(row) < 5:
            row.append("0")
        if len(row) < 6:
            row.append(row[1] if len(row) > 1 else "0")
        if len(row) < 7:
            row.append("0")
        if len(row) < 8:
            row.append("0")
        anime = Anime(name=row[0], ref=row[2], curr_ep=row[1], last_ep=row[5])
        anime.update_episodes({anime.curr_ep: "Not available"}, ut.config_data["general"].get("specials", False))
        if anime.has_episode(anime.curr_ep):
            episode = anime.episode(anime.curr_ep)
            progress = int(row[7]) if str(row[7]).lstrip("-").isdigit() else 0
            if progress <= 0:
                episode.mark_completed()
            else:
                episode.set_progress(progress)
        anilist_val = int(row[6]) if str(row[6]).isdigit() else 0
        status_idx = int(row[4]) if str(row[4]).isdigit() and 0 <= int(row[4]) < len(status_list) else (len(status_list) - 1)
        anime.set_info(anilist_val, status_list[status_idx], {"Episodi": row[3]})
        animes.append(anime)

    return animes
