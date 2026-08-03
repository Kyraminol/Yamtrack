import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django_celery_beat.models import PeriodicTask

STEAM_ID = "76561198000000000"


def form_data(**overrides):
    """Build a valid Steam import payload, overriding individual fields."""
    data = {
        "steam_id": STEAM_ID,
        "frequency": "once",
        "mode": "new",
        "achievements": "false",
    }
    data.update(overrides)
    return data


@patch("integrations.views.tasks.import_steam.delay")
class SteamViewTests(TestCase):
    """Test the Steam import view."""

    def setUp(self):
        """Create and log in a user."""
        credentials = {"username": "testuser", "password": "testpass123"}
        self.user = get_user_model().objects.create_user(**credentials)
        self.client.login(**credentials)

    def _post(self, **overrides):
        return self.client.post(reverse("import_steam"), form_data(**overrides))

    def test_once_queues_task(self, mock_delay):
        """Test 'once' queues the task with the steam_id and decoded checkbox."""
        # the template posts the literal strings "false"/"true" for the checkbox
        for posted, expected in (("false", False), ("true", True)):
            with self.subTest(achievements=posted):
                self.assertRedirects(
                    self._post(achievements=posted), reverse("import_data")
                )
                self.assertEqual(
                    mock_delay.call_args.kwargs,
                    {
                        "username": STEAM_ID,
                        "user_id": self.user.id,
                        "mode": "new",
                        "achievements": expected,
                    },
                )

    def test_recurring_schedules_periodic_task(self, mock_delay):
        """Test a recurring frequency schedules a task instead of queuing one."""
        self.assertRedirects(
            self._post(frequency="daily", time="14:30", achievements="true"),
            reverse("import_data"),
        )
        mock_delay.assert_not_called()
        task = PeriodicTask.objects.get(task="Import from Steam")
        stored = json.loads(task.kwargs)
        self.assertEqual(stored["username"], STEAM_ID)
        self.assertTrue(stored["achievements"])

    def test_invalid_payloads_import_nothing(self, mock_delay):
        """Test payloads the form rejects neither queue nor schedule a task."""
        payloads = {
            # the field was named "user" before it became "steam_id"
            "legacy user field": {
                "user": STEAM_ID,
                "frequency": "once",
                "mode": "new",
            },
            "not a 17-digit id": form_data(steam_id="gaben"),
            "recurring without time": form_data(frequency="daily"),
        }
        for label, payload in payloads.items():
            with self.subTest(payload=label):
                response = self.client.post(reverse("import_steam"), payload)
                self.assertRedirects(response, reverse("import_data"))
                mock_delay.assert_not_called()
                self.assertFalse(PeriodicTask.objects.exists())
