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
