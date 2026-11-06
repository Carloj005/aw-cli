import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
from rich.progress import Progress

sys.argv = ["aw-cli"]

from aw_cli import arg_parser, download, download_hls as hls, run, utilities as ut
from aw_cli.anime import Anime, AnimeStatus
from aw_cli.history import History, legacy
from aw_cli.interface.fzf import Fzf
from aw_cli.providers.animeunity import Animeunity
from aw_cli.providers.animeworld import Animeworld
from aw_cli.providers.local import LocalProvider
from aw_cli.search_utils import desanitize_search_query


# =====================================================================
# 1. CLI Argument Parsing (Combined Short Flags & Offline Combinations)
# =====================================================================


@pytest.mark.parametrize(
    "argv, expected_flags, expected_latest, expected_hist_val",
    [
        (["-c"], {"hist": True, "downl": False, "info": False, "private": False}, False, None),
        (["-cd"], {"hist": True, "downl": True, "info": False, "private": False}, False, None),
        (["-dc"], {"hist": True, "downl": True, "info": False, "private": False}, False, None),
        (["-ci"], {"hist": True, "downl": False, "info": True, "private": False}, False, None),
        (["-cp"], {"hist": True, "downl": False, "info": False, "private": True}, False, None),
        (["-c", "r"], {"hist": True, "downl": False, "info": False, "private": False}, False, "r"),
        (["-l"], {"latest": True, "downl": False, "info": False, "private": False}, "a", False),
        (["-ld"], {"latest": True, "downl": True, "info": False, "private": False}, "a", False),
        (["-li"], {"latest": True, "downl": False, "info": True, "private": False}, "a", False),
        (["-lp"], {"latest": True, "downl": False, "info": False, "private": True}, "a", False),
        (["-l", "t"], {"latest": True, "downl": False, "info": False, "private": False}, "t", False),
        (["-o", "-p", "-i"], {"offline": True, "hist": True, "private": True, "info": True}, False, False),
        (["-opi"], {"offline": True, "hist": True, "private": True, "info": True}, False, False),
    ],
)
def test_cli_combined_and_offline_flags(argv, expected_flags, expected_latest, expected_hist_val):
    parsed, flags = arg_parser.parse_cli_args(argv)
    for key, val in expected_flags.items():
        assert flags[key] is val, f"Flag {key} mismatch for {argv}"
    assert parsed.latest == expected_latest
    assert parsed.history == expected_hist_val


# =====================================================================
# 2. Non-Numeric / Special / Range Episode Sorting & numeric()
# =====================================================================


def test_episode_non_numeric_and_special_sorting_and_numeric():
    anime = Anime("Test Specials", "100", last_ep="12")
    anime.update_episodes(
        {
            "OVA": "r_ova",
            "12": "r12",
            "1-2": "r1_2",
            "7b": "r7b",
            "7a": "r7a",
            "7": "r7",
            "SP1": "r_sp1",
            "12.5-13": "r12_13",
            "1-12.5": "r1_125",
        },
        specials=True,
    )
    eps = anime.episodes()
    assert eps.index("1-2") < eps.index("7") < eps.index("7a") < eps.index("7b") < eps.index("12")
    assert eps.index("12") < eps.index("SP1") < eps.index("OVA")

    assert anime.episode("OVA").numeric() == 0
    assert anime.episode("SP1").numeric() == 1
    assert anime.episode("7a").numeric() == 7
    assert anime.episode("12.5-13").numeric() == 13
    assert anime.episode("1-12.5").numeric() == 12


# =====================================================================
# 3. Fzf Infinite-Loop & Missing Binary Guards
# =====================================================================


def test_fzf_empty_elements_returns_immediately():
    fzf = Fzf()
    with patch("subprocess.run") as mock_run:
        assert fzf.run([]) == ""
        mock_run.assert_not_called()


def test_fzf_missing_binary_exits_cleanly():
    fzf = Fzf()
    with (
        patch("aw_cli.utilities.find_binary", return_value=None),
        patch("subprocess.run", side_effect=FileNotFoundError("fzf not found")),
    ):
        with pytest.raises(SystemExit) as exc:
            fzf.run(["a", "b"])
        assert exc.value.code == 1


def test_fzf_persistent_error_code_stops_retrying():
    fzf = Fzf()
    failed_proc = MagicMock(returncode=2, stdout="")
    with patch("subprocess.run", return_value=failed_proc) as mock_run:
        with pytest.raises(SystemExit) as exc:
            fzf.run(["a", "b"])
        assert exc.value.code == 1
        assert mock_run.call_count == 4


# =====================================================================
# 4. VLC Tokenized URL Progress & Missing Player Guard
# =====================================================================


def test_open_vlc_reads_progress_for_urls_with_equals_and_local_files(tmp_path, monkeypatch):
    fake_vlc = tmp_path / "vlc.exe"
    fake_vlc.write_text("bin", encoding="utf-8")
    appdata = tmp_path / "appdata"
    vlc_dir = appdata / "vlc"
    vlc_dir.mkdir(parents=True)

    stream_url = "https://vixcloud.cc/playlist/692680?type=video&rendition=1080p&token=abc=def&expires=999"
    local_mp4 = tmp_path / "Anime Ep. 1.mp4"
    local_mp4.write_bytes(b"mp4")
    local_uri = local_mp4.resolve().as_uri()

    (vlc_dir / "vlc-qt-interface.ini").write_text(
        f"[General]\n[RecentsMRL]\nlist={stream_url}, {local_uri}\ntimes=425000, 180000\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setitem(ut.config_data, "player", {"path": str(fake_vlc), "type": "vlc"})

    with patch("subprocess.run"):
        completed_stream, prog_stream = run.open_vlc(stream_url, "Ep 1", 0)
        completed_local, prog_local = run.open_vlc(str(local_mp4), "Ep 1", 0)

    assert completed_stream is True and prog_stream == 425
    assert completed_local is True and prog_local == 180


def test_open_vlc_missing_binary_does_not_crash(monkeypatch):
    monkeypatch.setitem(ut.config_data, "player", {"path": "", "type": "vlc"})
    with patch("aw_cli.utilities.find_binary", return_value=None):
        completed, prog = run.open_vlc("https://example.com/v.mp4", "Ep 1", 0)
    assert completed is False and prog == 0


# =====================================================================
# 5. Download Headers, .temp Cleanup & FFmpeg Referer Forwarding
# =====================================================================


def test_build_download_headers_strips_csrf_and_sets_referer():
    provider = MagicMock()
    provider.BASE_URL = "https://www.animeunity.co"
    provider.Client.headers = {
        "User-Agent": "TestAgent/1.0",
        "x-csrf-token": "secret-csrf-token",
        "csrf-token": "secret-aw-token",
    }

    vix_headers = download._build_download_headers(
        "https://au-d1-04.vix-content.net/download/1080p.mp4?token=1", provider
    )
    assert vix_headers["User-Agent"] == "TestAgent/1.0"
    assert vix_headers["Referer"] == "https://vixcloud.cc/"
    assert "x-csrf-token" not in vix_headers
    assert "csrf-token" not in vix_headers

    aw_provider = MagicMock()
    aw_provider.BASE_URL = "https://www.animeworld.ac"
    aw_provider.Client.headers = {"User-Agent": "TestAgent/1.0", "csrf-token": "secret"}
    cdn_headers = download._build_download_headers(
        "https://srv1.sweetpixel.org/DDL/ANIME/Test.mp4", aw_provider
    )
    assert cdn_headers["Referer"] == "https://www.animeworld.ac/"
    assert "csrf-token" not in cdn_headers


def test_failed_download_cleans_up_temp_file_and_sanitizes_path(tmp_path, monkeypatch):
    monkeypatch.setenv("AW_CLI_DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setitem(ut.config_data, "general", {"connections-per-download": 2, "parallel-downloads": 1})

    anime = Anime("JoJo: Steel Ball Run?", "7431", last_ep="1")
    anime.update_episodes({"1": "999"})
    ep = anime.episode("1")

    provider = MagicMock()
    provider.BASE_URL = "https://www.animeunity.co"
    provider.Client.headers = {"User-Agent": "TestAgent"}
    provider.episode_link.return_value = "https://cdn.example.org/ep1.mp4"

    class FailingAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def head(self, url, headers=None):
            return httpx.Response(
                200,
                headers={"content-length": "1048576", "accept-ranges": "bytes", "content-type": "video/mp4"},
            )

        def stream(self, method, url, headers=None):
            raise httpx.ConnectError("Connessione interrotta durante il download")

    monkeypatch.setattr(download, "AsyncClient", FailingAsyncClient)
    download.episodes(anime, [ep], provider)

    temp_files = list(tmp_path.rglob("*.temp"))
    assert temp_files == [], f"File .temp non rimosso dopo errore: {temp_files}"


def test_download_with_ffmpeg_passes_referer_header(tmp_path, monkeypatch):
    captured_cmd = []
    out_file = tmp_path / "out.mp4.temp"
    out_file.write_bytes(b"mp4data")

    class FakeStream:
        def __init__(self, lines):
            self._lines = list(lines)

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self._lines:
                raise StopAsyncIteration
            return self._lines.pop(0)

    class FakeProc:
        returncode = 0
        stdout = FakeStream([b"total_size=7\n", b"out_time=00:00:10.00\n"])
        stderr = FakeStream([b"  Duration: 00:00:10.00, start: 0.0\n"])

        async def wait(self):
            return 0

    async def fake_exec(*cmd, **kwargs):
        captured_cmd.extend(cmd)
        return FakeProc()

    monkeypatch.setattr(hls.asyncio, "create_subprocess_exec", fake_exec)

    async def _run():
        progress = Progress(disable=True)
        tid = progress.add_task("Ep 1", total=None, hls=True)
        await hls.download_with_ffmpeg(
            "https://vixcloud.cc/playlist/1",
            {"User-Agent": "UA", "Referer": "https://vixcloud.cc/"},
            out_file,
            progress,
            tid,
        )

    asyncio.run(_run())
    assert "-headers" in captured_cmd
    idx = captured_cmd.index("-headers")
    assert captured_cmd[idx + 1] == "Referer: https://vixcloud.cc/\r\n"


# =====================================================================
# 6. History Safety, Cross-Provider Reload & Resilient Legacy CSV
# =====================================================================


def test_history_isolated_default_list_and_safe_save(tmp_path):
    h1 = History()
    h2 = History()
    h1.get().append(Anime("A", "1"))
    assert h2.get() == []
    # Salvataggio con path vuoto non deve sollevare FileNotFoundError
    h1.save()

    hist_dir = tmp_path / "data"
    hist_dir.mkdir()
    hist_file = hist_dir / "history.json"
    valid_json = json.dumps([Anime("KeepMe", "123", "1", "12").to_dict()], indent=4)
    hist_file.write_text(valid_json, encoding="utf-8")

    h3 = History.read(str(hist_dir))
    bad_anime = Anime("Broken", "999")
    bad_anime.info["bad"] = object()  # type: ignore[assignment]
    h3.get().append(bad_anime)
    with pytest.raises(TypeError):
        h3.save()

    # Il file precedente non deve essere stato troncato a 0 byte!
    assert hist_file.read_text(encoding="utf-8") == valid_json


def test_history_reload_cross_provider_updates_last_ep_without_polluting_refs(tmp_path):
    aw_anime = Anime("Kaiju No. 8 2", "https://www.animeworld.ac/play/kaiju.abc", curr_ep="6", last_ep="6")
    aw_anime.set_info(178754, AnimeStatus.ONGOING, {"Episodi": "12"})
    aw_anime.update_episodes({"6": "aw_ep6_id"})

    hist = History(str(tmp_path / "history.json"), [aw_anime])

    au_latest = Anime("Kaiju No. 8 2", "6726", curr_ep="7", last_ep="7")
    au_latest.set_info(178754, AnimeStatus.ONGOING, {"Episodi": "12"})
    au_latest.update_episodes({"7": "999888"})

    hist.reload([au_latest])
    assert aw_anime.last_ep == "7"
    # Non deve aver inserito un ref numerico di AnimeUnity dentro un anime con URL AnimeWorld
    assert not aw_anime.has_episode("7")
    assert aw_anime.episode("6").ref == "aw_ep6_id"


def test_legacy_csv_ignores_blank_rows_and_invalid_ints(tmp_path):
    csv_file = tmp_path / "aw-cronologia.csv"
    csv_file.write_text("\n\nTest Anime,6.5,1234,12,99,12,not_an_int,bad_prog\n\n", encoding="utf-8")
    monkey_cfg = {"specials": False}
    with patch.dict(ut.config_data, {"general": monkey_cfg}):
        res = legacy(str(tmp_path))
    assert len(res) == 1
    assert res[0].name == "Test Anime"
    assert res[0].anilist_id == 0
    assert res[0].status == AnimeStatus.UNKNOWN


# =====================================================================
# 7. Providers: Desanitize, Legacy Domain Rewrite, Latest 't', LocalProvider
# =====================================================================


def test_desanitize_search_query_restores_ascii_punctuation():
    sanitized = ut.sanitize_filename('JoJo: Steel Ball Run? "Part 1/2" ..')
    restored = desanitize_search_query(sanitized)
    assert ":" in restored and "?" in restored and "/" in restored


def test_animeworld_rewrites_legacy_domains_to_active_base_url():
    aw = Animeworld(client=MagicMock())
    aw.BASE_URL = "https://www.animeworld.ac"
    rewritten = aw._normalize_url("https://www.animeworld.so/play/dan-da-dan-2.IsJ2N")
    assert rewritten == "https://www.animeworld.ac/play/dan-da-dan-2.IsJ2N"
    # Non deve riscrivere domini esterni (es. CDN video)
    cdn = "https://srv1.sweetpixel.org/DDL/ANIME/Test.mp4"
    assert aw._normalize_url(cdn) == cdn


def test_animeunity_latest_trending_sorts_by_views_and_deduplicates():
    au = Animeunity(client=MagicMock())
    items = {
        "data": [
            {"id": 10, "number": "2", "anime": {"id": 1, "title_eng": "Low Views", "visite": 100, "real_episodes_count": 2}},
            {"id": 21, "number": "5", "anime": {"id": 2, "title_eng": "Top Hit", "visite": 50000, "real_episodes_count": 5}},
            {"id": 20, "number": "4", "anime": {"id": 2, "title_eng": "Top Hit", "visite": 50000, "real_episodes_count": 5}},
            {"id": 30, "number": "3.5", "anime": {"id": 3, "title_eng": "Recap Special", "visite": 99999, "real_episodes_count": 3}},
        ]
    }
    escaped = json.dumps(items).replace('"', "&quot;")
    setattr(au, "_session", f'<layout-items items-json="{escaped}"></layout-items>')

    trending = au._latest("t", specials=False)
    assert [a.name for a in trending] == ["Top Hit", "Low Views"]
    assert trending[0].curr_ep == "5"


def test_local_provider_matches_unsanitized_history_entry(tmp_path):
    sanitized_dir = ut.sanitize_filename("Tokyo Ghoul:re")
    ep_dir = tmp_path / sanitized_dir
    ep_dir.mkdir()
    (ep_dir / f"{sanitized_dir} Ep. 1.mp4").write_bytes(b"video")

    hist_entry = Anime("Tokyo Ghoul:re", "9999", curr_ep="1", last_ep="12")
    hist_entry.set_info(100240, AnimeStatus.FINISHED, {"Episodi": "12"})
    hist_entry.update_episodes({"1": "ref1"})
    hist_entry.episode("1").set_progress(320)

    lp = LocalProvider(str(tmp_path), [hist_entry])
    found = lp.search("")
    assert len(found) == 1
    assert found[0].anilist_id == 100240
    assert found[0].episode("1").progress == 320
