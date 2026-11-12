from datetime import timedelta
from io import StringIO
from json import loads
from unittest.mock import patch, Mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings
from django.utils import timezone

from axes.conf import LockoutTier
from axes.models import AccessAttempt, AccessAttemptExpiration, AccessLog
from tests.base import AxesTestCase


class ResetAccessLogsManagementCommandTestCase(AxesTestCase):
    def setUp(self):
        self.msg_not_found = "No logs found.\n"
        self.msg_num_found = "{} logs removed.\n"

        days_3 = timezone.now() - timedelta(days=3)
        with patch("django.utils.timezone.now", Mock(return_value=days_3)):
            AccessLog.objects.create()

        days_13 = timezone.now() - timedelta(days=9)
        with patch("django.utils.timezone.now", Mock(return_value=days_13)):
            AccessLog.objects.create()

        days_30 = timezone.now() - timedelta(days=27)
        with patch("django.utils.timezone.now", Mock(return_value=days_30)):
            AccessLog.objects.create()

    def test_axes_delete_access_logs_default(self):
        out = StringIO()
        call_command("axes_reset_logs", stdout=out)
        self.assertEqual(self.msg_not_found, out.getvalue())

    def test_axes_delete_access_logs_older_than_2_days(self):
        out = StringIO()
        call_command("axes_reset_logs", age=2, stdout=out)
        self.assertEqual(self.msg_num_found.format(3), out.getvalue())

    def test_axes_delete_access_logs_older_than_4_days(self):
        out = StringIO()
        call_command("axes_reset_logs", age=4, stdout=out)
        self.assertEqual(self.msg_num_found.format(2), out.getvalue())

    def test_axes_delete_access_logs_older_than_16_days(self):
        out = StringIO()
        call_command("axes_reset_logs", age=16, stdout=out)
        self.assertEqual(self.msg_num_found.format(1), out.getvalue())


class ManagementCommandTestCase(AxesTestCase):
    def setUp(self):
        AccessAttempt.objects.create(
            username="jane.doe", ip_address="10.0.0.1", failures_since_start="4"
        )

        AccessAttempt.objects.create(
            username="john.doe", ip_address="10.0.0.2", failures_since_start="15"
        )

        AccessAttempt.objects.create(
            username="richard.doe", ip_address="10.0.0.4", failures_since_start="12"
        )

    def test_axes_list_attempts(self):
        out = StringIO()
        call_command("axes_list_attempts", stdout=out)

        expected = "10.0.0.1\tjane.doe\t4\n10.0.0.2\tjohn.doe\t15\n10.0.0.4\trichard.doe\t12\n"
        self.assertEqual(expected, out.getvalue())

    def test_axes_reset(self):
        out = StringIO()
        call_command("axes_reset", stdout=out)

        expected = "3 attempts removed.\n"
        self.assertEqual(expected, out.getvalue())

    def test_axes_reset_not_found(self):
        out = StringIO()
        call_command("axes_reset", stdout=out)

        out = StringIO()
        call_command("axes_reset", stdout=out)

        expected = "No attempts found.\n"
        self.assertEqual(expected, out.getvalue())

    def test_axes_reset_ip(self):
        out = StringIO()
        call_command("axes_reset_ip", "10.0.0.1", stdout=out)

        expected = "1 attempts removed.\n"
        self.assertEqual(expected, out.getvalue())

    def test_axes_reset_ip_username(self):
        out = StringIO()
        call_command("axes_reset_ip_username", "10.0.0.4", "richard.doe", stdout=out)

        expected = "1 attempts removed.\n"
        self.assertEqual(expected, out.getvalue())

    def test_axes_reset_ip_not_found(self):
        out = StringIO()
        call_command("axes_reset_ip", "10.0.0.3", stdout=out)

        expected = "No attempts found.\n"
        self.assertEqual(expected, out.getvalue())

    def test_axes_reset_username(self):
        out = StringIO()
        call_command("axes_reset_username", "john.doe", stdout=out)

        expected = "1 attempts removed.\n"
        self.assertEqual(expected, out.getvalue())

    def test_axes_reset_username_not_found(self):
        out = StringIO()
        call_command("axes_reset_username", "ivan.renko", stdout=out)

        expected = "No attempts found.\n"
        self.assertEqual(expected, out.getvalue())


@override_settings(
    AXES_LOCKOUT_TIERS=[
        LockoutTier(failures=3, cooloff=timedelta(minutes=15)),
        LockoutTier(failures=6, cooloff=timedelta(hours=2)),
        LockoutTier(failures=10, cooloff=timedelta(days=1)),
    ],
    AXES_COOLOFF_TIME=None,
)
class LockoutStatisticsManagementCommandTestCase(AxesTestCase):
    def setUp(self):
        super().setUp()

        # Ordinary failure below the first tier threshold.
        AccessAttempt.objects.create(
            username="failed.user", ip_address="10.0.0.1", failures_since_start=2
        )
        # Locked on the first tier, cooloff still running.
        AccessAttempt.objects.create(
            username="locked.user", ip_address="10.0.0.2", failures_since_start=3
        )
        # Locked on the second tier, cooloff still running.
        AccessAttempt.objects.create(
            username="tiered.user", ip_address="10.0.0.3", failures_since_start=7
        )
        # First tier attempt whose cooloff has already elapsed.
        expired_time = timezone.now() - timedelta(hours=1)
        with patch("django.utils.timezone.now", Mock(return_value=expired_time)):
            expired_attempt = AccessAttempt.objects.create(
                username="expired.user",
                ip_address="10.0.0.4",
                failures_since_start=3,
            )
        AccessAttemptExpiration.objects.create(
            access_attempt=expired_attempt,
            expires_at=expired_time + timedelta(minutes=15),
        )

    def test_statistics_text_groups_by_effective_tier(self):
        out = StringIO()
        call_command("axes_list_attempts", "--statistics", stdout=out)
        output = out.getvalue()

        self.assertIn("2 locked, 1 expired, 1 failed", output)
        self.assertIn("tier 3", output)
        self.assertIn("locked=1", output)
        self.assertIn("expired=1", output)
        self.assertIn("failed=1", output)
        self.assertIn("tier 6", output)
        self.assertIn("0:15:00", output)
        self.assertIn("2:00:00", output)

    def test_statistics_text_omits_account_identifiers(self):
        out = StringIO()
        call_command("axes_list_attempts", "--statistics", stdout=out)
        output = out.getvalue()

        for identifier in (
            "failed.user",
            "locked.user",
            "tiered.user",
            "expired.user",
            "10.0.0.1",
            "10.0.0.2",
            "10.0.0.3",
            "10.0.0.4",
        ):
            self.assertNotIn(identifier, output)

    def test_statistics_json_is_machine_readable(self):
        out = StringIO()
        call_command("axes_list_attempts", "--statistics", "--json", stdout=out)
        data = loads(out.getvalue())

        self.assertEqual(
            data["summary"], {"locked": 2, "expired": 1, "failed": 1}
        )
        groups = data["groups"]
        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[0]["rule"]["type"], "tier")
        self.assertEqual(groups[0]["rule"]["threshold"], 3)
        self.assertEqual(groups[0]["rule"]["cooloff_seconds"], 900)
        self.assertEqual(
            groups[0]["counts"], {"locked": 1, "expired": 1, "failed": 1}
        )
        self.assertEqual(groups[1]["rule"]["threshold"], 6)
        self.assertEqual(groups[1]["rule"]["cooloff_seconds"], 7200)
        self.assertEqual(
            groups[1]["counts"], {"locked": 1, "expired": 0, "failed": 0}
        )
        self.assertNotIn("username", out.getvalue())
        self.assertNotIn("ip_address", out.getvalue())

    def test_statistics_json_generated_at_is_iso8601(self):
        from datetime import datetime

        out = StringIO()
        call_command("axes_list_attempts", "--statistics", "--json", stdout=out)
        data = loads(out.getvalue())
        self.assertEqual(
            datetime.fromisoformat(data["generated_at"]).date(),
            timezone.now().date(),
        )

    def test_statistics_filters_from_command_input(self):
        out = StringIO()
        call_command(
            "axes_list_attempts",
            "--statistics",
            "--json",
            "--username",
            "locked.user",
            stdout=out,
        )
        data = loads(out.getvalue())
        self.assertEqual(
            data["summary"], {"locked": 1, "expired": 0, "failed": 0}
        )
        self.assertEqual(len(data["groups"]), 1)

        out = StringIO()
        call_command(
            "axes_list_attempts",
            "--statistics",
            "--json",
            "--ip-address",
            "10.0.0.4",
            stdout=out,
        )
        data = loads(out.getvalue())
        self.assertEqual(
            data["summary"], {"locked": 0, "expired": 1, "failed": 0}
        )

    def test_json_without_statistics_raises(self):
        out = StringIO()
        with self.assertRaises(CommandError):
            call_command("axes_list_attempts", "--json", stdout=out)

    def test_default_text_mode_still_lists_identifiers(self):
        out = StringIO()
        call_command("axes_list_attempts", stdout=out)
        output = out.getvalue()

        self.assertIn("10.0.0.2\tlocked.user\t3", output)
        self.assertIn("10.0.0.4\texpired.user\t3", output)

    def test_default_text_mode_supports_filters(self):
        out = StringIO()
        call_command(
            "axes_list_attempts", "--ip-address", "10.0.0.3", stdout=out
        )
        output = out.getvalue()

        self.assertEqual("10.0.0.3\ttiered.user\t7\n", output)


@override_settings(AXES_FAILURE_LIMIT=5, AXES_COOLOFF_TIME=None)
class PermanentLockoutStatisticsTestCase(AxesTestCase):
    def test_permanent_lockout_is_reported_as_locked(self):
        AccessAttempt.objects.create(
            username="locked.user", ip_address="10.0.0.9", failures_since_start=5
        )
        AccessAttempt.objects.create(
            username="failed.user", ip_address="10.0.0.8", failures_since_start=1
        )

        out = StringIO()
        call_command("axes_list_attempts", "--statistics", "--json", stdout=out)
        data = loads(out.getvalue())

        self.assertEqual(
            data["summary"], {"locked": 1, "expired": 0, "failed": 1}
        )
        group = data["groups"][0]
        self.assertEqual(group["rule"]["type"], "failure_limit")
        self.assertEqual(group["rule"]["threshold"], 5)
        self.assertIsNone(group["rule"]["cooloff_seconds"])


@override_settings(AXES_FAILURE_LIMIT=3, AXES_COOLOFF_TIME=1)
class CooloffExpirationStatisticsTestCase(AxesTestCase):
    def test_attempt_time_derived_expiration_is_classified(self):
        expired_time = timezone.now() - timedelta(hours=2)
        with patch("django.utils.timezone.now", Mock(return_value=expired_time)):
            AccessAttempt.objects.create(
                username="expired.user",
                ip_address="10.0.0.7",
                failures_since_start=3,
            )

        out = StringIO()
        call_command("axes_list_attempts", "--statistics", "--json", stdout=out)
        data = loads(out.getvalue())

        self.assertEqual(
            data["summary"], {"locked": 0, "expired": 1, "failed": 0}
        )
        self.assertEqual(data["groups"][0]["rule"]["cooloff_seconds"], 3600)
