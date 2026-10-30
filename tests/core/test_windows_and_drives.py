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


def test_max_quality_selection_ranks_highest_resolution_and_bitrate():
    from aw_cli.anime import Anime
    from aw_cli.providers.animeworld import Animeworld

    # 1. AnimeUnity: even if 480p/720p appear first or have misleading order, 1920x1080 wins, and canPlayFHD=1 unlocks h=1
    mock_client = MagicMock()
    au = Animeunity(client=mock_client)
    setattr(au, "_session", "<html></html>")

    page = """
        window.masterPlaylist = {
            params: {'token': 't', 'expires': '999'},
            url: 'https://vixcloud.cc/playlist/555',
        };
        window.canPlayFHD = 1;
    """
    manifest = (
        "#EXTM3U\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=1080000,RESOLUTION=854x480\n"
        "https://vixcloud.cc/playlist/555?rendition=480p\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=4500000,RESOLUTION=1920x1080\n"
        "https://vixcloud.cc/playlist/555?rendition=1080p\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=1800000,RESOLUTION=1280x720\n"
        "https://vixcloud.cc/playlist/555?rendition=720p\n"
    )
    r1 = MagicMock(text="https://vixcloud.cc/embed/555?canPlayFHD=1")
    r1.raise_for_status = lambda: None
    r2 = MagicMock(text=page)
    r2.raise_for_status = lambda: None
    r3 = MagicMock(text=manifest, url="https://vixcloud.cc/playlist/555")
    r3.raise_for_status = lambda: None
    mock_client.get.side_effect = [r1, r2, r3]

    anime = Anime("Quality Test", "1")
    ep = Anime.Episode(anime, "1", "10")
    cands = au._episode_stream_candidates(anime, ep)
    assert cands[0] == "https://vixcloud.cc/playlist/555?rendition=1080p"
    assert "h=1" in cands[1]

    # 2. AnimeWorld: picks 1080p source over 480p/720p if multiple <source> tags exist
    aw_mock = MagicMock()
    aw = Animeworld(client=aw_mock)
    aw_resp = MagicMock(
        text='<video><source src="https://cdn.example.com/ep1_480p.mp4"><source src="https://cdn.example.com/ep1_1080p.mp4"></video>'
    )
    aw_resp.raise_for_status = lambda: None
    aw_mock.get.return_value = aw_resp
    assert aw._episode_link(anime, ep) == "https://cdn.example.com/ep1_1080p.mp4"


def test_security_doh_rejects_private_and_metadata_ips(monkeypatch):
    class FakeResp:
        status_code = 200

        def json(self):
            return {
                "Answer": [
                    {"data": "169.254.169.254"},
                    {"data": "192.168.1.1"},
                    {"data": "10.0.0.1"},
                    {"data": "104.21.55.12"},
                ]
            }

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get(self, *a, **kw):
            return FakeResp()

    monkeypatch.setattr(domain_resolver, "Client", FakeClient)
    domain_resolver._DOH_IP_CACHE.pop("test.animeunity.co", None)
    ip = domain_resolver.resolve_via_doh("test.animeunity.co")
    assert ip == "104.21.55.12"


def test_security_fzf_filter_and_update_branch_block_injection(monkeypatch):
    from aw_cli.interface.fzf import Fzf
    from aw_cli import update

    fzf = Fzf()
    try:
        malicious_elements = ["1", "2'; calc.exe & echo '3"]
        cmd = fzf._build_cmd(malicious_elements, "> ", multi=False, filter=True)
        binds = [c for c in cmd if c.startswith("change:reload(")]
        assert len(binds) == 1
        assert "calc.exe" not in binds[0]
        assert "--episodes-file" in binds[0]
    finally:
        fzf._cleanup_temp_files()

    ran_cmds = []
    monkeypatch.setattr(update.subprocess, "run", lambda cmd, **kw: ran_cmds.append(cmd))
    update.update("--index-url=https://evil.example")
    update.update("main;whoami")
    assert ran_cmds == []


def test_local_provider_handles_temp_files_and_untracked_folders(tmp_path):
    from aw_cli.anime import Anime
    from aw_cli.providers.local import LocalProvider

    anime_dir = tmp_path / "Solo Leveling"
    anime_dir.mkdir()
    (anime_dir / "Solo Leveling Ep. 1.mp4").write_bytes(b"00")
    (anime_dir / "Solo Leveling Ep. 2.mp4.temp").write_bytes(b"00")
    (anime_dir / "desktop.ini").write_text("test", encoding="utf-8")
    (tmp_path / "Empty Folder").mkdir()

    lp = LocalProvider(str(tmp_path), history=[])
    found = lp._search("")
    assert len(found) == 1
    assert found[0].name == "Solo Leveling"
    assert found[0].episodes() == ["1"]
