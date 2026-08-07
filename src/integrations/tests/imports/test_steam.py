from datetime import UTC, datetime
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase
from requests import Response
from requests.exceptions import HTTPError

from app.models import (
    Game,
    Item,
    MediaTypes,
    Sources,
    Status,
)
from integrations.imports import (
    helpers,
    steam,
)

STEAM_ID = "76561198000000000"


def _owned_games(*games):
    """Wrap game dicts in a Steam GetOwnedGames response."""
    return {"response": {"games": list(games)}}


def _achievements(*achieved):
    """Build a GetPlayerAchievements response from (achieved, unlocktime) pairs."""
    return {
        "playerstats": {
            "achievements": [{"achieved": a, "unlocktime": ts} for a, ts in achieved],
        },
    }


class ImportSteam(TestCase):
    """Test importing media from Steam."""

    def setUp(self):
        """Create user for the tests."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)

    @patch("integrations.imports.steam.services.api_request")
    @patch("integrations.imports.steam.external_game")
    @patch("integrations.imports.steam.services.get_media_metadata")
    def test_import_steam_games(
        self,
        mock_get_metadata,
        mock_external_game,
        mock_api_request,
    ):
        """Test importing games from Steam maps playtime to statuses."""
        mock_api_request.return_value = _owned_games(
            {
                "appid": 730,
                "name": "Counter-Strike 2",
                "playtime_forever": 1250,
                "playtime_2weeks": 120,  # Recent activity
            },
            {
                "appid": 570,
                "name": "Dota 2",
                "playtime_forever": 0,  # Never played
                "playtime_2weeks": 0,
            },
            {
                "appid": 440,
                "name": "Team Fortress 2",
                "playtime_forever": 500,
                "playtime_2weeks": 0,  # No recent activity
            },
        )
        mock_external_game.side_effect = [1, 2, 3]
        mock_get_metadata.side_effect = [
            {"title": "Counter-Strike 2", "image": "http://example.com/cs2.jpg"},
            {"title": "Dota 2", "image": "http://example.com/dota2.jpg"},
            {"title": "Team Fortress 2", "image": "http://example.com/tf2.jpg"},
        ]

        imported_counts, _ = steam.importer(
            STEAM_ID, self.user, "new", achievements=False
        )

        self.assertEqual(imported_counts[MediaTypes.GAME.value], 3)

        games = Game.objects.filter(user=self.user)
        self.assertEqual(games.count(), 3)

        cs2_game = games.get(item__title="Counter-Strike 2")
        self.assertEqual(cs2_game.status, Status.IN_PROGRESS.value)
        self.assertEqual(cs2_game.progress, 1250)
        self.assertEqual(cs2_game.notes, "Imported from Steam")

        dota_game = games.get(item__title="Dota 2")
        self.assertEqual(dota_game.status, Status.PLANNING.value)
        self.assertEqual(dota_game.progress, 0)

        tf2_game = games.get(item__title="Team Fortress 2")
        self.assertEqual(tf2_game.status, Status.PAUSED.value)
        self.assertEqual(tf2_game.progress, 500)

        # achievements disabled: no second API call per game
        self.assertEqual(mock_api_request.call_count, 1)

    @patch("integrations.imports.steam.services.api_request")
    @patch("integrations.imports.steam.external_game")
    @patch("integrations.imports.steam.services.get_media_metadata")
    def test_import_steam_achievements(
        self,
        mock_get_metadata,
        mock_external_game,
        mock_api_request,
    ):
        """Test achievements populate notes and start date, not status."""
        mock_api_request.side_effect = [
            _owned_games(
                {
                    "appid": 730,
                    "name": "Counter-Strike 2",
                    "playtime_forever": 1250,
                    "playtime_2weeks": 0,
                    "has_community_visible_stats": 1,
                },
                {
                    "appid": 440,
                    "name": "Team Fortress 2",
                    "playtime_forever": 500,
                    "playtime_2weeks": 0,
                    "has_community_visible_stats": 1,
                },
            ),
            _achievements((1, 1700000000), (1, 1704067200)),
            # 7 of 9 unlocked: 77.77...% is truncated to one decimal
            _achievements(*[(1, 1700000000 + i) for i in range(7)], (0, 0), (0, 0)),
        ]
        mock_external_game.side_effect = [1, 2]
        mock_get_metadata.side_effect = [
            {"title": "Counter-Strike 2", "image": "http://example.com/cs2.jpg"},
            {"title": "Team Fortress 2", "image": "http://example.com/tf2.jpg"},
        ]

        steam.importer(STEAM_ID, self.user, "new", achievements=True)

        games = Game.objects.filter(user=self.user)
        cs2_game = games.get(item__title="Counter-Strike 2")
        self.assertEqual(cs2_game.status, Status.PAUSED.value)
        self.assertEqual(
            cs2_game.notes,
            "[Steam Importer] Achievements: 2/2 (100.0%) - last unlock 2024-01-01",
        )
        self.assertEqual(
            cs2_game.start_date, datetime(2023, 11, 14, 22, 13, tzinfo=UTC)
        )
        self.assertIsNone(cs2_game.end_date)

        tf2_game = games.get(item__title="Team Fortress 2")
        self.assertEqual(tf2_game.status, Status.PAUSED.value)
        self.assertEqual(
            tf2_game.notes,
            "[Steam Importer] Achievements: 7/9 (77.7%) - last unlock 2023-11-14",
        )
        self.assertEqual(
            tf2_game.start_date, datetime(2023, 11, 14, 22, 13, tzinfo=UTC)
        )
        self.assertIsNone(tf2_game.end_date)

        # one achievements call per game on top of the owned-games call
        self.assertEqual(mock_api_request.call_count, 3)

    @patch("integrations.imports.steam.services.api_request")
    @patch("integrations.imports.steam.external_game")
    @patch("integrations.imports.steam.services.get_media_metadata")
    def test_import_steam_achievements_unavailable(
        self,
        mock_get_metadata,
        mock_external_game,
        mock_api_request,
    ):
        """Test games without usable achievements import silently."""
        response = Response()
        response.status_code = 400
        mock_api_request.side_effect = [
            _owned_games(
                {
                    "appid": 730,
                    "name": "Counter-Strike 2",
                    "playtime_forever": 500,
                    "playtime_2weeks": 0,
                    # no has_community_visible_stats, not worth a request
                },
                {
                    "appid": 440,
                    "name": "Team Fortress 2",
                    "playtime_forever": 500,
                    "playtime_2weeks": 0,
                    "has_community_visible_stats": 1,
                },
            ),
            # Steam answers 400 for apps that expose no stats after all
            HTTPError(response=response),
        ]
        mock_external_game.side_effect = [1, 2]
        mock_get_metadata.side_effect = [
            {"title": "Counter-Strike 2", "image": "http://example.com/cs2.jpg"},
            {"title": "Team Fortress 2", "image": "http://example.com/tf2.jpg"},
        ]

        imported_counts, warnings = steam.importer(
            STEAM_ID, self.user, "new", achievements=True
        )

        self.assertEqual(imported_counts[MediaTypes.GAME.value], 2)
        # a game without achievements is not a problem worth reporting
        self.assertEqual(warnings, "")

        for game in Game.objects.filter(user=self.user):
            self.assertEqual(game.status, Status.PAUSED.value)
            self.assertEqual(game.notes, "Imported from Steam")

        # only the flagged game is requested
        self.assertEqual(mock_api_request.call_count, 2)

    @patch("integrations.imports.steam.services.api_request")
    def test_import_steam_private_profile(self, mock_api_request):
        """Test handling of private Steam profile."""
        response = Response()
        response.status_code = 403
        mock_api_request.side_effect = HTTPError(response=response)

        with self.assertRaises(helpers.MediaImportError) as context:
            steam.importer(STEAM_ID, self.user, "new", achievements=False)

        self.assertIn("private or invalid", str(context.exception))

    @patch("integrations.imports.steam.services.api_request")
    @patch("integrations.imports.steam.external_game")
    def test_import_steam_game_not_found_in_igdb(
        self,
        mock_external_game,
        mock_api_request,
    ):
        """Test handling of games not found in IGDB."""
        mock_api_request.return_value = _owned_games(
            {
                "appid": 999,
                "name": "Unknown Game",
                "playtime_forever": 100,
                "playtime_2weeks": 0,
            },
        )
        mock_external_game.return_value = None

        imported_counts, warnings = steam.importer(
            STEAM_ID, self.user, "new", achievements=False
        )

        self.assertEqual(imported_counts.get(MediaTypes.GAME.value, 0), 0)
        self.assertIn("Unknown Game (999)", warnings)
        self.assertIn(f"Couldn't find a match in {Sources.IGDB.label}", warnings)
        self.assertEqual(Game.objects.filter(user=self.user).count(), 0)

    @patch("integrations.imports.steam.services.api_request")
    def test_import_steam_no_api_key(self, _mock_api_request):
        """Test handling when Steam API key is not configured."""
        with patch.object(settings, "STEAM_API_KEY", ""):
            with self.assertRaises(helpers.MediaImportError) as context:
                steam.importer(STEAM_ID, self.user, "new", achievements=False)

            self.assertIn("Steam API key not configured", str(context.exception))


@patch("integrations.imports.steam.services.api_request")
@patch("integrations.imports.steam.external_game")
@patch("integrations.imports.steam.services.get_media_metadata")
class ImportSteamOverwrite(TestCase):
    """Test Steam overwrite behavior for existing games."""

    def setUp(self):
        """Create user and common data for the tests."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)

        self.item = Item.objects.create(
            media_id="1",
            source=Sources.IGDB.value,
            media_type=MediaTypes.GAME.value,
            title="Counter-Strike 2",
            image="http://example.com/cs2.jpg",
        )

    def _create_game(self, status=Status.PLANNING.value, progress=0, notes=""):
        """Create a game with a specific status and progress."""
        return Game.objects.create(
            item=self.item,
            user=self.user,
            status=status,
            progress=progress,
            notes=notes,
        )

    def _setup_mocks(
        self,
        mock_get_metadata,
        mock_external_game,
        mock_api_request,
        *,
        playtime=1300,
        has_stats=False,
    ):
        """Set up the common Steam and IGDB mocks."""
        mock_get_metadata.return_value = {
            "title": "Counter-Strike 2",
            "image": "http://example.com/cs2.jpg",
            "max_progress": None,
        }
        mock_external_game.return_value = 1

        game = {
            "appid": 730,
            "name": "Counter-Strike 2",
            "playtime_forever": playtime,
            "playtime_2weeks": 120,
        }
        if has_stats:
            game["has_community_visible_stats"] = 1
        mock_api_request.return_value = _owned_games(game)

    def test_overwrite_updates_existing(
        self,
        mock_get_metadata,
        mock_external_game,
        mock_api_request,
    ):
        """Test overwrite mode updates an existing game instead of recreating it."""
        self._setup_mocks(mock_get_metadata, mock_external_game, mock_api_request)
        game = self._create_game()

        imported_counts, _ = steam.importer(
            STEAM_ID, self.user, "overwrite", achievements=False
        )

        game.refresh_from_db()
        self.assertEqual(imported_counts[MediaTypes.GAME.value], 1)
        self.assertEqual(Game.objects.filter(user=self.user).count(), 1)
        self.assertEqual(game.progress, 1300)
        self.assertEqual(game.status, Status.IN_PROGRESS.value)
        self.assertEqual(game.history.count(), 2)

    def test_overwrite_does_not_downgrade_completed(
        self,
        mock_get_metadata,
        mock_external_game,
        mock_api_request,
    ):
        """Test overwrite mode does not downgrade completed games."""
        self._setup_mocks(
            mock_get_metadata,
            mock_external_game,
            mock_api_request,
            playtime=1100,
        )
        game = self._create_game(status=Status.COMPLETED.value, progress=1000)

        steam.importer(STEAM_ID, self.user, "overwrite", achievements=False)

        game.refresh_from_db()
        self.assertEqual(game.progress, 1100)
        self.assertEqual(game.status, Status.COMPLETED.value)

    def test_overwrite_appends_achievement_note_without_regressing_dropped(
        self,
        mock_get_metadata,
        mock_external_game,
        mock_api_request,
    ):
        """Test achievements append the note but don't reopen a dropped game."""
        self._setup_mocks(
            mock_get_metadata,
            mock_external_game,
            mock_api_request,
            has_stats=True,
        )
        mock_api_request.side_effect = [
            mock_api_request.return_value,
            _achievements((1, 1700000000), (1, 1704067200)),
        ]
        game = self._create_game(
            status=Status.DROPPED.value,
            notes="Imported from Steam",
        )

        steam.importer(STEAM_ID, self.user, "overwrite", achievements=True)

        game.refresh_from_db()
        self.assertEqual(game.status, Status.DROPPED.value)
        self.assertEqual(
            game.notes,
            "Imported from Steam\n\n"
            "[Steam Importer] Achievements: 2/2 (100.0%) - last unlock 2024-01-01",
        )
        self.assertEqual(game.start_date, datetime(2023, 11, 14, 22, 13, tzinfo=UTC))

    def test_overwrite_refreshes_stale_achievement_note(
        self,
        mock_get_metadata,
        mock_external_game,
        mock_api_request,
    ):
        """Test an existing achievements note is replaced, not duplicated."""
        self._setup_mocks(
            mock_get_metadata,
            mock_external_game,
            mock_api_request,
            has_stats=True,
        )
        mock_api_request.side_effect = [
            mock_api_request.return_value,
            _achievements((1, 1700000000), (0, 0)),
        ]
        game = self._create_game(
            notes="Imported from Steam\n\n[Steam Importer] Achievements: 1/10 (10.0%)",
        )

        _, warnings = steam.importer(
            STEAM_ID, self.user, "overwrite", achievements=True
        )

        game.refresh_from_db()
        self.assertEqual(warnings, "")
        self.assertEqual(
            game.notes,
            "Imported from Steam\n\n"
            "[Steam Importer] Achievements: 1/2 (50.0%) - last unlock 2023-11-14",
        )

    def test_overwrite_updates_newest_game_instance(
        self,
        mock_get_metadata,
        mock_external_game,
        mock_api_request,
    ):
        """Test overwrite mode updates the newest game instance."""
        self._setup_mocks(mock_get_metadata, mock_external_game, mock_api_request)
        older_game = self._create_game(progress=100)
        newer_game = self._create_game(progress=200)

        steam.importer(STEAM_ID, self.user, "overwrite", achievements=False)

        older_game.refresh_from_db()
        newer_game.refresh_from_db()
        self.assertEqual(older_game.progress, 100)
        self.assertEqual(older_game.status, Status.PLANNING.value)
        self.assertEqual(newer_game.progress, 1300)
        self.assertEqual(newer_game.status, Status.IN_PROGRESS.value)

    def test_new_mode_skips_existing_game(
        self,
        mock_get_metadata,
        mock_external_game,
        mock_api_request,
    ):
        """Test that new mode still skips existing games."""
        self._setup_mocks(mock_get_metadata, mock_external_game, mock_api_request)
        game = self._create_game()

        imported_counts, _ = steam.importer(
            STEAM_ID, self.user, "new", achievements=False
        )

        game.refresh_from_db()
        self.assertEqual(imported_counts, {})
        self.assertEqual(game.progress, 0)
        self.assertEqual(game.status, Status.PLANNING.value)
