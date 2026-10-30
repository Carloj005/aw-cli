from pathlib import Path
from unittest.mock import MagicMock
from aw_cli import utilities as ut
from aw_cli import download
from aw_cli import domain_resolver
from aw_cli.providers.animeunity import Animeunity


def test_sanitize_filename_blocks_traversal_and_invalid_chars(monkeypatch):
    monkeypatch.setattr(ut, "os_name", "Windows")
    assert ".." not in ut.sanitize_filename("../../Windows/System32")
    assert ":" not in ut.sanitize_filename("Le Bizzarre Avventure di JoJo: Stardust")
    assert not ut.sanitize_filename("Bakuman... ").endswith(".")
    assert "\x00" not in ut.sanitize_filename("Test\x00Anime")


def test_download_path_supports_secondary_drive_config(tmp_path, monkeypatch):
    secondary_drive_dir = tmp_path / "SecondaryDrive" / "Anime"
    monkeypatch.setitem(ut.config_data, "general", {"download-path": str(secondary_drive_dir)})
    resolved = download.path(create=True)
    assert resolved == secondary_drive_dir
    assert resolved.exists()


def test_data_dir_respects_aw_cli_home_env(tmp_path, monkeypatch):
    custom_home = tmp_path / "CustomDataHome"
    monkeypatch.setenv("AW_CLI_HOME", str(custom_home))
    resolved = ut.get_data_dir()
    assert resolved == custom_home
    assert resolved.exists()


def test_animeunity_resolves_telegram_domain_and_redirect():
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.url = "https://www.animeunity.co/"
    mock_response.text = '<meta name="csrf-token" content="tok123"><layout-items items-json="{}"></layout-items>'
    mock_client.get.return_value = mock_response

    canonical, html = domain_resolver.resolve_animeunity_session(mock_client, "https://animeunity.co")
    assert canonical == "https://www.animeunity.co"
    assert "tok123" in html


def test_animeunity_discovers_new_domain_from_telegram_when_primary_down(monkeypatch, tmp_path):
    monkeypatch.setenv("AW_CLI_HOME", str(tmp_path))
    mock_client = MagicMock()

    def fake_get(url, **kwargs):
        resp = MagicMock()
        if "t.me/s/animeunitytv" in url:
            resp.status_code = 200
            resp.text = "Nuovo dominio ufficiale: https://animeunity.newtld"
            return resp
        if "animeunity.newtld" in url:
            resp.status_code = 200
            resp.url = "https://www.animeunity.newtld/"
            resp.text = '<meta name="csrf-token" content="new_csrf"><layout-items items-json="{}"></layout-items>'
            return resp
        raise RuntimeError("Domain unreachable")

    mock_client.get.side_effect = fake_get
    monkeypatch.setattr(domain_resolver, "resolve_via_doh", lambda host: None)

    canonical, html = domain_resolver.resolve_animeunity_session(mock_client, "https://animeunity.co")
    assert canonical == "https://www.animeunity.newtld"
    assert "new_csrf" in html


def test_anti_phishing_rejects_clone_without_csrf():
    assert not domain_resolver.is_valid_animeunity_html("<html><body>Fake AnimeUnity Clone</body></html>")
    assert domain_resolver.is_valid_animeunity_html(
        '<html><head><meta name="csrf-token" content="abc"></head><body><layout-items></layout-items></body></html>'
    )


def test_animeunity_stream_candidates_prefer_1080p_hls_with_mp4_fallback():
    from aw_cli.anime import Anime

    mock_client = MagicMock()
    au = Animeunity(client=mock_client)
    setattr(au, "_session", "<html></html>")

    anime = Anime("Attack on Titan 3 (ITA)", "2620")
    episode = Anime.Episode(anime, "8", ref="45633")

    embed_page = """
        window.masterPlaylist = {
            params: {'token': 'tok123', 'expires': '1798539322', 'asn': ''},
            url: 'https://vixcloud.cc/playlist/332759',
        };
        window.canPlayFHD = true;
        window.downloadUrl = 'https://au-d1-02.vix-content.net/download/ep8/1080p.mp4?token=dl';
    """
    hls_master_manifest = (
        "#EXTM3U\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=4500000,RESOLUTION=1920x1080\n"
        "https://vixcloud.cc/playlist/332759?type=video&rendition=1080p&token=v1080\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=1800000,RESOLUTION=1280x720\n"
        "https://vixcloud.cc/playlist/332759?type=video&rendition=720p&token=v720\n"
    )

    resp_embed_url = MagicMock(text="https://vixcloud.cc/embed/332759")
    resp_embed_url.raise_for_status = lambda: None
    resp_page = MagicMock(text=embed_page)
    resp_page.raise_for_status = lambda: None
    resp_master = MagicMock(text=hls_master_manifest, url="https://vixcloud.cc/playlist/332759")
    resp_master.raise_for_status = lambda: None

    mock_client.get.side_effect = [resp_embed_url, resp_page, resp_master]

    candidates = au._episode_stream_candidates(anime, episode)
    assert len(candidates) == 3
    assert "rendition=1080p" in candidates[0]
    assert candidates[1].startswith("https://vixcloud.cc/playlist/332759?")
    assert "h=1" in candidates[1]
    assert candidates[2] == "https://au-d1-02.vix-content.net/download/ep8/1080p.mp4?token=dl"


def test_watch_episode_falls_back_on_503_and_preserves_progress_on_total_failure(monkeypatch, tmp_path):
    import sys
    sys.argv = ["aw-cli"]
    from aw_cli import run
    from aw_cli.anime import Anime
    from aw_cli.history import History

    monkeypatch.setitem(ut.config_data, "general", {"download-path": str(tmp_path / "dl")})
    monkeypatch.setattr(run, "private", False)
    monkeypatch.setattr(run, "offline", False)
    monkeypatch.setattr(run, "open_player", run.open_mpv)
    monkeypatch.setattr(run, "sleep", lambda _: None)

    hist_obj = History(str(tmp_path / "history.json"), [])
    monkeypatch.setattr(run, "history", hist_obj)

    anime = Anime("Attack on Titan 3 (ITA)", "2620", curr_ep="8", last_ep="12")
    anime.update_episodes({"8": "45633", "9": "45634"})
    ep8 = anime.episode("8")
    ep8.set_progress(336)
    ep9 = anime.episode("9")

    provider = MagicMock()
    provider.episode_stream_candidates.return_value = [
        "https://broken-primary.example/playlist.m3u8",
        "https://working-fallback.example/video.mp4",
    ]

    # Case 1: Primary fails (playback_ok=False), fallback succeeds at 500s -> plays & saves 500s
    calls = []

    def fake_mpv_fallback(url, title, start_prog):
        calls.append((url, start_prog))
        if "broken-primary" in url:
            return False, 0, False, False
        return False, 500, True, True

    monkeypatch.setattr(run, "_open_mpv_detailed", fake_mpv_fallback)
    run.watch_episode(anime, ep8, provider)
    assert len(calls) == 2
    assert ep8.progress == 500
    assert anime.curr_ep == "8"

    # Case 2: Total stream failure on 'prossimo' (Ep 9) -> must NOT corrupt curr_ep or wipe ep8 progress
    monkeypatch.setattr(run, "_open_mpv_detailed", lambda u, t, p: (False, 0, False, False))
    run.watch_episode(anime, ep9, provider)
    assert anime.curr_ep == "8"
    assert ep8.progress == 500


def test_rewatch_menu_offers_resume_or_restart_when_progress_exists(monkeypatch):
    import sys
    sys.argv = ["aw-cli"]
    from aw_cli import run
    from aw_cli.anime import Anime

    anime = Anime("Test", "1", "1", "2")
    anime.update_episodes({"1": "10", "2": "11"})
    ep1 = anime.episode("1")

    # No progress -> no prompt, returns ep1 immediately
    menu = run.create_ep_menu(anime, ep1)
    assert menu["riguarda"]() is ep1
    assert ep1.progress == 0

    # Partial progress -> selecting "riprendi da 05:36" keeps progress 336
    ep1.set_progress(336)
    monkeypatch.setattr(run.Fzf, "run", lambda self, elems, prompt="> ", **kw: "riprendi da 05:36")
    assert menu["riguarda"]() is ep1
    assert ep1.progress == 336

    # Partial progress -> selecting "ricomincia dall'inizio" resets progress to 0
    monkeypatch.setattr(run.Fzf, "run", lambda self, elems, prompt="> ", **kw: "ricomincia dall'inizio")
    assert menu["riguarda"]() is ep1
    assert ep1.progress == 0
