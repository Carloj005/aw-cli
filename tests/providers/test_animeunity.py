import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
from urllib.parse import parse_qs, urlsplit
from aw_cli.providers.animeunity import Animeunity
from aw_cli.anime import Anime

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"


class TestAnimeunity:
    """Test unitari per il nuovo provider AnimeUnity (animeunity.cx, Inertia + API REST)."""

    @pytest.fixture
    def au(self):
        mock_client = MagicMock()
        provider = Animeunity(client=mock_client)
        setattr(provider, "_session", "<html></html>")
        provider._inertia_version = "test-ver-1"
        return provider

    def test_animeunity_search(self, au):
        data = json.loads((FIXTURES_DIR / "au_search.json").read_text(encoding="utf-8"))

        with patch.object(au, "Client") as mock_client:
            mock_client.get.return_value.json.return_value = data
            mock_client.get.return_value.raise_for_status = lambda: None

            animes = au._search("naruto")
            assert len(animes) > 0, "Nessun anime trovato nella fixture per la ricerca"
            assert animes[0].name != ""
            called_urls = [c.kwargs.get("url") or c.args[0] for c in mock_client.get.call_args_list]
            assert any(u == "https://animeunity.cx/api/search" for u in called_urls)

    def test_animeunity_latest(self, au):
        html = (FIXTURES_DIR / "au_latest.html").read_text(encoding="utf-8")
        setattr(au, "_session", html)

        with patch.object(au, "Client") as mock_client:
            mock_client.get.side_effect = RuntimeError("use embedded home fallback")
            animes = au._latest("a", specials=False)
            assert len(animes) > 0, "Nessun anime trovato nella fixture per il latest"
            assert animes[0].name != ""
            for anime in animes:
                if anime.curr_ep and anime.has_episode(anime.curr_ep):
                    assert anime.episode(anime.curr_ep).ref.isdigit()

    def test_animeunity_episodes(self, au):
        anime = Anime("Naruto", "1469", last_ep="2")
        anime.info["Episodi"] = "2"
        mocked_episodes = {
            "episodes": [
                {
                    "id": 24782,
                    "number": "1",
                    "videos": [
                        {"id": 28317, "language": "ita", "is_hidden": False},
                        {"id": 28546, "language": "jpn", "is_hidden": False},
                    ],
                },
                {
                    "id": 24783,
                    "number": "2",
                    "videos": [
                        {"id": 28318, "language": "ita", "is_hidden": False},
                        {"id": 80045, "language": "jpn", "is_hidden": False},
                    ],
                },
            ],
            "start": 1,
            "tab_size": 120,
            "total": 2,
            "offset": 1,
        }

        with patch.object(au, "Client") as mock_client:
            mock_client.get.return_value.status_code = 200
            mock_client.get.return_value.json.return_value = mocked_episodes
            mock_client.get.return_value.raise_for_status = lambda: None

            episodes_sub = au._episodes(anime)
            assert episodes_sub == {"1": "28546", "2": "80045"}

            anime_dub = Anime("Naruto (ITA)", "1469", last_ep="2")
            episodes_dub = au._episodes(anime_dub)
            assert episodes_dub == {"1": "28317", "2": "28318"}

    def test_animeunity_episode_link(self, au):
        anime = Anime("Naruto", "1469")
        episode = Anime.Episode(anime, "1", ref="28546")

        inertia_resp = MagicMock(
            status_code=200,
            headers={"content-type": "application/json"},
            text='{"props":{"embedUrl":"https://vixcloud.cc/embed/223701"}}',
        )
        inertia_resp.json.return_value = {
            "props": {"embedUrl": "https://vixcloud.cc/embed/223701"}
        }
        inertia_resp.raise_for_status = lambda: None

        player_resp = MagicMock(
            status_code=200,
            text="window.downloadUrl = 'https://source-video.mp4';",
        )
        player_resp.raise_for_status = lambda: None

        with patch.object(au, "Client") as mock_client:
            mock_client.get.side_effect = [inertia_resp, player_resp]

            video_url = au._episode_link(anime, episode)
            assert video_url == "https://source-video.mp4"
            args, kwargs = mock_client.get.call_args_list[0]
            assert "/anime/1469-x/28546" in args[0]
            assert kwargs["headers"]["X-Inertia"] == "true"

    @pytest.mark.parametrize("fhd, expected_h", [(True, ["1"]), (False, None)])
    def test_animeunity_hls_when_download_url_is_empty(self, au, fhd, expected_h):
        anime = Anime("Naruto", "1469")
        episode = Anime.Episode(anime, "1", ref="28546")
        page = """
            window.downloadUrl = '';
            window.masterPlaylist = {
                params: {'token': 'a+b/=', 'expires': '123456', 'asn': ''},
                url: 'https://vixcloud.cc/playlist/987?existing=yes',
            };
        """ + f"window.canPlayFHD = {str(fhd).lower()};"

        inertia_resp = MagicMock(
            status_code=200,
            headers={"content-type": "application/json"},
            text='{"props":{"embedUrl":"https://vixcloud.cc/embed/987"}}',
        )
        inertia_resp.json.return_value = {
            "props": {"embedUrl": "https://vixcloud.cc/embed/987"}
        }
        inertia_resp.raise_for_status = lambda: None

        player_resp = MagicMock(status_code=200, text=page)
        player_resp.raise_for_status = lambda: None

        with patch.object(au, "Client") as mock_client:
            mock_client.get.side_effect = [inertia_resp, player_resp]
            link = au._episode_link(anime, episode)

        parts = urlsplit(link)
        assert parts.path == "/playlist/987"
        query = parse_qs(parts.query)
        assert query["token"] == ["a+b/="]
        assert query["expires"] == ["123456"]
        assert query["existing"] == ["yes"]
        assert query.get("h") == expected_h

    def test_animeunity_empty_video_link_has_clear_error(self, au):
        anime = Anime("Naruto", "1469")
        episode = Anime.Episode(anime, "1", ref="28546")

        inertia_resp = MagicMock(
            status_code=200,
            headers={"content-type": "application/json"},
            text='{"props":{"embedUrl":"https://vixcloud.cc/embed/987"}}',
        )
        inertia_resp.json.return_value = {
            "props": {"embedUrl": "https://vixcloud.cc/embed/987"}
        }
        inertia_resp.raise_for_status = lambda: None

        player_resp = MagicMock(status_code=200, text="window.downloadUrl = '';")
        player_resp.raise_for_status = lambda: None

        with patch.object(au, "Client") as mock_client:
            mock_client.get.side_effect = [inertia_resp, player_resp]
            with pytest.raises(ValueError, match="playlist HLS non trovati"):
                au._episode_link(anime, episode)

    def test_animeunity_info_anime(self, au):
        data = json.loads((FIXTURES_DIR / "au_info.json").read_text(encoding="utf-8"))
        anime = Anime("Naruto", "1469")

        inertia_resp = MagicMock(
            status_code=200,
            headers={"content-type": "application/json"},
            text='{"props":{}}',
        )
        inertia_resp.json.return_value = {"props": {"anime": data}}
        inertia_resp.raise_for_status = lambda: None

        with patch.object(au, "Client") as mock_client:
            mock_client.get.return_value = inertia_resp

            au._info_anime(anime)
            assert anime.last_ep == "220"
            assert "Genere" in anime.info
            assert "Action" in anime.info["Genere"]
            assert anime.anilist_id == 20
