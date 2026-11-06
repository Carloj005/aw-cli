import sys
import argparse
from .utilities import os_name
from importlib.metadata import version
from .update import update

class Update(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        update(str(values) if values else "")
        parser.exit()

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        "aw-cli",
        description="Guarda anime dal terminale e molto altro!",
        add_help=False,
    )

    info_group = p.add_argument_group("Informazioni")
    options_group = p.add_argument_group("Opzioni")
    config_group = p.add_argument_group("Configurazione")

    info_group.add_argument(
        "-h",
        "--help",
        action="help",
        help="mostra questo messaggio",
    )

    info_group.add_argument(
        "-v",
        "--versione",
        action="version",
        version=version("aw-cli"),
        help="stampa la versione del programma",
    )

    options_group.add_argument(
        "-c",
        "--cronologia",
        nargs="?",
        default=False,
        const=None,
        choices=["r"],
        dest="history",
        help="continua a guardare un anime dalla cronologia. 'r' per rimuovere un anime (opzionale)",
    )

    options_group.add_argument(
        "-l",
        "--lista",
        nargs="?",
        default=False,
        const=None,
        choices=["a", "s", "d", "t"],
        dest="latest",
        help="lista degli ultimi anime usciti. Filtri: a = all, s = sub, d = dub, t = tendenze. Default 'a'",
    )

    options_group.add_argument(
        "-i",
        "--info",
        action="store_true",
        dest="info",
        help="visualizza le informazioni e la trama di un anime",
    )

    if os_name != "Android":
        options_group.add_argument(
            "-s",
            "--syncplay",
            action="store_true",
            dest="syncpl",
            help="usa syncplay per guardare un anime insieme ai tuoi amici",
        )

    options_group.add_argument(
        "-d",
        "--download",
        action="store_true",
        dest="download",
        help="scarica gli episodi che preferisci",
    )

    options_group.add_argument(
        "-o",
        "--offline",
        action="store_true",
        dest="offline",
        help="apri gli episodi scaricati precedentemente direttamente dal terminale",
    )

    options_group.add_argument(
        "-p",
        "--privato",
        action="store_true",
        dest="private",
        help="guarda un episodio senza che si aggiorni la cronologia o AniList",
    )

    options_group.add_argument(
        "-u",
        "--update",
        nargs="?",
        action=Update,
        type=str,
        help="aggiorna il programma",
    )

    config_group.add_argument(
        "-a",
        "--configurazione",
        action="store_true",
        dest="start_config",
        help="avvia il menu di configurazione",
    )

    return p


_COMBINABLE_SHORT_FLAGS = frozenset({"c", "l", "i", "s", "d", "o", "p", "a"})


def _expand_combined_short_flags(raw_argv: list[str]) -> list[str]:
    """
    Espande combinazioni di flag brevi (es. `-cd`, `-ci`, `-cp`, `-ld`, `-li`, `-lp`, `-op`)
    in flag singoli (`['-c', '-d']`), evitando che `nargs='?'` su `-c`/`-l` interpreti i flag
    adiacenti come argomento testuale non valido. I valori espliciti con spazio (`-c r`, `-l t`)
    restano invariati.
    """
    expanded: list[str] = []
    for token in raw_argv:
        if (
            len(token) >= 3
            and token.startswith("-")
            and not token.startswith("--")
            and all(ch in _COMBINABLE_SHORT_FLAGS for ch in token[1:])
        ):
            expanded.extend(f"-{ch}" for ch in token[1:])
        else:
            expanded.append(token)
    return expanded


def parse_cli_args(
    argv: list[str] | None = None,
    arg_parser: argparse.ArgumentParser | None = None,
) -> tuple[argparse.Namespace, dict[str, bool]]:
    p = arg_parser or build_parser()
    effective_argv = _expand_combined_short_flags(sys.argv[1:] if argv is None else list(argv))
    parsed = p.parse_args(effective_argv)
    flags = {
        "downl": False,
        "latest": False,
        "offline": False,
        "hist": False,
        "info": False,
        "private": False,
    }

    if parsed.offline:
        flags["offline"] = True
        flags["hist"] = True
        flags["info"] = bool(parsed.info)
        flags["private"] = bool(parsed.private)
    elif parsed.history == "r":
        flags["hist"] = True
    else:
        if parsed.info:
            flags["info"] = True
        if parsed.download:
            flags["downl"] = True
        if parsed.latest is not False:
            if parsed.latest is None:
                parsed.latest = "a"
            flags["latest"] = True
        if parsed.private:
            flags["private"] = True
        if parsed.history is not False:
            flags["hist"] = True

    return parsed, flags


parser = build_parser()
args, _flags = parse_cli_args(arg_parser=parser)
downl = _flags["downl"]
latest = _flags["latest"]
offline = _flags["offline"]
hist = _flags["hist"]
info = _flags["info"]
private = _flags["private"]
