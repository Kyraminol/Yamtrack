import logging
import re
from collections import defaultdict
from datetime import UTC, datetime
from math import trunc
from typing import NamedTuple

import requests
from django.conf import settings

import app
from app.models import MediaTypes, Sources, Status
from app.providers import services
from app.providers.igdb import ExternalGameSource, external_game
from integrations.forms import ImportMode
from integrations.imports import helpers
from integrations.imports.helpers import MediaImportError

logger = logging.getLogger(__name__)

BASE_NOTE = "Imported from Steam"
ACHIEVEMENTS_BASE_NOTE = (
    "[Steam Importer] Achievements: {unlocked}/{total} ({percentage}%)"
)
ACHIEVEMENTS_NOTE_REGEX = re.compile(
    rf"^{
        re.sub(r'([\[\]()])', r'\\\1', ACHIEVEMENTS_BASE_NOTE).format(
            unlocked=r'\d+', total=r'\d+', percentage=r'\d+(?:\.\d)?'
        )
    }$",
    re.MULTILINE,
)

STEAM_API_BASE_URL = "https://api.steampowered.com"
STEAM_OWNED_GAMES_URL = f"{STEAM_API_BASE_URL}/IPlayerService/GetOwnedGames/v0001/"
STEAM_ACHIEVEMENTS_URL = (
    f"{STEAM_API_BASE_URL}/ISteamUserStats/GetPlayerAchievements/v0001/"
)


def importer(steam_id, user, mode, *, achievements=False):
    """Import the user's games from Steam."""
    steam_importer = SteamImporter(steam_id, user, mode, achievements=achievements)
    return steam_importer.import_data()


class SteamAchievementsProgress(NamedTuple):
    """Steam game achievement stats."""

    unlocked: int = 0
    total: int = 0
    start_date: datetime | None = None

    @property
    def percentage(self) -> float | int:
        """Return the achievement progress percentage."""
        return (
            trunc(self.unlocked * 100 / self.total * 10) / 10 if self.total > 0 else 0
        )

    @property
    def note(self) -> str:
        """Returns achievements note."""
        if not self.unlocked or not self.total:
            return ""
        return ACHIEVEMENTS_BASE_NOTE.format(
            unlocked=self.unlocked, total=self.total, percentage=self.percentage
        )


class SteamImporter:
    """Class to handle importing user game data from Steam."""

    def __init__(self, steam_id, user, mode, *, achievements):
        """Initialize the importer with user details and mode.

        Args:
            steam_id (str): Steam user ID (64-bit SteamID) to import from
            user: Django user object to import data for
            mode (str): Import mode ("new" or "overwrite")
            achievements (bool): Whether to import achievements
        """
        self.steam_id = steam_id
        self.user = user
        self.mode = mode
        self.achievements = achievements
        self.warnings = []
        self.api_key = settings.STEAM_API_KEY

        if not self.api_key:
            msg = "Steam API key not configured in environment variables"
            raise MediaImportError(msg)

        self.existing_media = helpers.get_existing_media(user)

        self.to_delete = defaultdict(lambda: defaultdict(set))

        self.bulk_media = defaultdict(list)
        self.bulk_media_updates = defaultdict(list)

        logger.info(
            "Initialized Steam importer for Steam ID %s with mode %s",
            steam_id,
            mode,
        )

    def import_data(self):
        """Import user's Steam game library."""
        owned_games = self._get_owned_games()

        if not owned_games:
            logger.info("No games found for Steam user %s", self.steam_id)
            return {}, ""

        for game_data in owned_games:
            self._process_game(game_data)

        helpers.cleanup_existing_media(self.to_delete, self.user)
        helpers.bulk_create_media(self.bulk_media, self.user)
        helpers.bulk_update_media(
            self.bulk_media_updates,
            {
                MediaTypes.GAME.value: [
                    "progress",
                    "status",
                    "start_date",
                    "notes",
                ]
            },
            self.user,
        )

        created_games = len(self.bulk_media[MediaTypes.GAME.value])
        updated_games = len(self.bulk_media_updates[MediaTypes.GAME.value])
        imported_counts = {}
        if created_games or updated_games:
            imported_counts[MediaTypes.GAME.value] = created_games + updated_games

        logger.info(
            "Steam import completed for user %s: %s",
            self.user.username,
            imported_counts,
        )

        return imported_counts, "\n".join(self.warnings) if self.warnings else ""

    def _get_owned_games(self):
        """Fetch owned games from Steam API."""
        params = {
            "key": self.api_key,
            "steamid": self.steam_id,
            "include_appinfo": 1,
            "include_played_free_games": 1,
            "format": "json",
        }

        try:
            response = services.api_request(
                "STEAM", "GET", STEAM_OWNED_GAMES_URL, params=params
            )

            if "response" not in response:
                msg = "Invalid response from Steam API"
                raise MediaImportError(msg)

            if "games" not in response["response"]:
                # User might have private profile or no games
                logger.warning(
                    "No games found in Steam response for user %s",
                    self.steam_id,
                )
                return []

            games = response["response"]["games"]
            logger.info(
                "Found %d games for Steam user %s",
                len(games),
                self.steam_id,
            )
            return games  # noqa: TRY300

        except requests.HTTPError as error:
            if error.response.status_code == requests.codes.too_many_requests:
                msg = "Steam API rate limit exceeded. Please try again later."
                raise MediaImportError(msg) from error
            if error.response.status_code == requests.codes.forbidden:
                msg = "Steam profile is private or invalid"
                raise MediaImportError(msg) from error
            if error.response.status_code == requests.codes.bad_request:
                msg = "Bad request to Steam API. Please check the Steam ID."
                raise MediaImportError(msg) from error
            if error.response.status_code == requests.codes.unauthorized:
                msg = "Invalid Steam API key"
                raise MediaImportError(msg) from error
            msg = f"Steam API error: {error.response.status_code}"
            raise MediaImportError(msg) from error

    def _process_game(self, game_data):
        """Process a single game from Steam API response."""
        appid = str(game_data["appid"])
        name = game_data.get("name", f"Unknown Game {appid}")
        playtime_forever = game_data.get("playtime_forever") or 0  # in minutes
        playtime_2weeks = game_data.get("playtime_2weeks") or 0  # in minutes

        try:
            # Try to match with IGDB
            igdb_game = self._match_with_igdb(name, appid)

            if not igdb_game:
                # Skip games that can't be matched to IGDB
                logger.debug(
                    "Skipping Steam game %s (appid: %s) - no IGDB match found",
                    name,
                    appid,
                )
                self.warnings.append(
                    f"{name} ({appid}): Couldn't find a match in {Sources.IGDB.label}",
                )
                return

            achievements_progress = self._get_achievements_progress(game_data)

            media_id = str(igdb_game["media_id"])
            existing_game = self.existing_media[MediaTypes.GAME.value][
                Sources.IGDB.value
            ].get(media_id)

            if existing_game and self.mode == ImportMode.OVERWRITE.value:
                self._queue_existing_game_update(
                    existing_game,
                    playtime_forever,
                    playtime_2weeks,
                    achievements_progress,
                )
                return

            if not helpers.should_process_media(
                self.existing_media,
                self.to_delete,
                MediaTypes.GAME.value,
                Sources.IGDB.value,
                media_id,
                self.mode,
            ):
                return

            # Use IGDB data if found
            item, _ = app.models.Item.objects.get_or_create(
                media_id=str(igdb_game["media_id"]),
                source=Sources.IGDB.value,
                media_type=MediaTypes.GAME.value,
                defaults={
                    "title": igdb_game["title"],
                    "image": igdb_game["image"],
                },
            )

            # Determine status based on playtime and achievements
            status, start_date = self._determine_game_status(
                playtime_forever, playtime_2weeks, achievements_progress
            )

            # Create game object
            game = app.models.Game(
                item=item,
                user=self.user,
                status=status,
                score=None,
                progress=playtime_forever,
                notes=achievements_progress.note or BASE_NOTE,
                start_date=start_date,
            )

            self.bulk_media[MediaTypes.GAME.value].append(game)

        except services.ProviderAPIError as e:
            msg = str(e).lower()
            is_not_found = "game with id" in msg and "not found" in msg
            if not is_not_found:
                # still raise all other errors
                raise

            logger.debug(
                "Skipping Steam game %s (appid: %s) - IGDB not found: %s",
                name,
                appid,
                e,
            )
            self.warnings.append(
                f"{name} ({appid}): Couldn't find a match in {Sources.IGDB.label}"
            )

        except (ValueError, KeyError, TypeError) as e:
            logger.warning("Failed to process Steam game %s (%s): %s", name, appid, e)
            self.warnings.append(f"{name} ({appid}): {e!s}")

    def _get_achievements_progress(self, game_data):
        """Fetch game achievements from Steam API."""
        appid = str(game_data.get("appid"))
        playtime_forever = game_data.get("playtime_forever") or 0
        has_community_visible_stats = game_data.get("has_community_visible_stats")

        if (
            not self.achievements
            or playtime_forever < 1
            or not has_community_visible_stats
        ):
            return SteamAchievementsProgress()

        try:
            params = {
                "key": self.api_key,
                "steamid": self.steam_id,
                "appid": appid,
                "format": "json",
            }
            response = services.api_request(
                "STEAM", "GET", STEAM_ACHIEVEMENTS_URL, params=params
            )
            achievements = response.get("playerstats", {}).get("achievements") or []
            unlocked_achievements = [
                achievement.get("unlocktime")
                for achievement in achievements
                if achievement.get("achieved") == 1
            ]

            filtered_timestamps = sorted(ts for ts in unlocked_achievements if ts)
            first_achievement_date = None
            if len(filtered_timestamps) > 0:
                first_achievement_date = self._parse_timestamp_utc(
                    filtered_timestamps[0]
                )

            return SteamAchievementsProgress(
                len(unlocked_achievements),
                len(achievements),
                first_achievement_date,
            )

        except requests.RequestException as error:
            logger.debug(
                "Error fetching achievements for game id %s: %s",
                appid,
                error,
            )
            return SteamAchievementsProgress()

    @staticmethod
    def _parse_timestamp_utc(timestamp):
        """Parse Steam timestamp (UTC) into a datetime object."""
        if not timestamp:
            return None
        return datetime.fromtimestamp(timestamp, tz=UTC)

    def _queue_existing_game_update(
        self, game, playtime_forever, playtime_2weeks, achievements_progress
    ):
        """Queue updates for an existing game when Steam overwrite is used."""
        changed = False

        if game.progress != playtime_forever:
            game.progress = playtime_forever
            changed = True

        new_status, start_date = self._determine_game_status(
            playtime_forever, playtime_2weeks, achievements_progress
        )

        prevent_status_regression = game.status in (
            Status.COMPLETED.value,
            Status.DROPPED.value,
        ) and new_status in (
            Status.PLANNING.value,
            Status.IN_PROGRESS.value,
            Status.PAUSED.value,
        )

        if game.status != new_status and not prevent_status_regression:
            game.status = new_status
            changed = True

        achievements_notes = achievements_progress.note
        game_notes = game.notes or ""
        if achievements_notes:
            existing_achievements_notes = ACHIEVEMENTS_NOTE_REGEX.search(game_notes)
            if existing_achievements_notes:
                if achievements_notes not in game_notes:
                    game.notes = ACHIEVEMENTS_NOTE_REGEX.sub(
                        achievements_notes, game_notes
                    )
                    changed = True
            else:
                game.notes = (
                    f"{game.notes}\n\n{achievements_notes}"
                    if game.notes
                    else achievements_notes
                )
                changed = True

        if not game.start_date and start_date:
            game.start_date = start_date
            changed = True

        if changed:
            self.bulk_media_updates[MediaTypes.GAME.value].append(game)
            logger.debug("Queued Steam update for existing game %s", game)

    @staticmethod
    def _determine_game_status(
        playtime_forever, playtime_2weeks, achievements_progress
    ):
        """Determine game status based on Steam playtime data.

        Args:
            playtime_forever (int): Total playtime in minutes
            playtime_2weeks (int): Playtime in last 2 weeks in minutes
            achievements_progress (SteamAchievementsProgress)

        Returns:
            tuple: Status value from Status choices, start date
        """
        # Games with no playtime are considered "Planning"
        if playtime_forever == 0:
            return Status.PLANNING.value, None

        # Games played in the last 2 weeks are "In Progress"
        if playtime_2weeks > 0:
            return Status.IN_PROGRESS.value, achievements_progress.start_date

        # Games with total playtime but no recent activity are "On Hold"
        return Status.PAUSED.value, achievements_progress.start_date

    @staticmethod
    def _match_with_igdb(game_name, steam_appid):
        """Try to match Steam game with IGDB using External Game endpoint."""
        # Try to find IGDB game by Steam App ID using external_game endpoint
        igdb_game_id = external_game(steam_appid, ExternalGameSource.STEAM)

        if not igdb_game_id:
            return None

        # Get the game details using the IGDB ID
        game_details = services.get_media_metadata(
            MediaTypes.GAME.value,
            str(igdb_game_id),
            Sources.IGDB.value,
        )

        logger.debug(
            "Matched Steam game %s (appid: %s) with IGDB ID %s via external_game",
            game_name,
            steam_appid,
            igdb_game_id,
        )
        return {
            "media_id": igdb_game_id,
            "source": Sources.IGDB.value,
            "media_type": MediaTypes.GAME.value,
            "title": game_details.get("title", game_name),
            "image": game_details["image"],
        }
