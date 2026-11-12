from datetime import timedelta
from io import StringIO
from unittest.mock import patch, Mock

from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone

from axes.models import AccessAttempt, AccessLog
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


class CleanExpiredAttemptsBatchManagementCommandTestCase(AxesTestCase):
    """
    Batched cleanup of expired access attempts on the temporary test database.

    Records are created straddling the cool off expiry boundary, two batches
    are run with new failure events inserted in between, and the reported
    deleted/remaining counts are asserted so operators know when to stop.
    """

    def create_attempt(self, username, attempt_time):
        with patch("django.utils.timezone.now", Mock(return_value=attempt_time)):
            return AccessAttempt.objects.create(
                username=username,
                ip_address="10.0.0.1",
                user_agent=username,
                failures_since_start=1,
            )

    def run_batch(self, batch_size=2):
        out = StringIO()
        call_command("axes_clean_expired_attempts", batch_size=batch_size, stdout=out)
        return out.getvalue()

    @override_settings(AXES_COOLOFF_TIME=timedelta(hours=1))
    def test_batched_cleanup_with_concurrent_failures(self):
        now = timezone.now()
        expired_time = now - timedelta(hours=2)
        cooling_time = now - timedelta(minutes=30)

        for i in range(5):
            self.create_attempt(f"expired-{i}", expired_time)
        for i in range(2):
            self.create_attempt(f"cooling-{i}", cooling_time)

        self.assertEqual(
            "2 expired attempts removed. 3 expired attempts remaining.\n",
            self.run_batch(),
        )

        # new failure events recorded in parallel between batches
        self.create_attempt("fresh-1", now)
        self.create_attempt("fresh-2", now)

        self.assertEqual(
            "2 expired attempts removed. 1 expired attempts remaining.\n",
            self.run_batch(),
        )
        self.assertEqual(
            "1 expired attempts removed. 0 expired attempts remaining.\n",
            self.run_batch(),
        )
        self.assertEqual("No expired attempts found.\n", self.run_batch())

        # only the cooling and freshly recorded attempts survive
        self.assertEqual(
            {"cooling-0", "cooling-1", "fresh-1", "fresh-2"},
            set(AccessAttempt.objects.values_list("username", flat=True)),
        )

    @override_settings(AXES_COOLOFF_TIME=None)
    def test_no_cooloff_configured(self):
        self.create_attempt("expired-0", timezone.now() - timedelta(days=30))
        self.assertEqual("No expired attempts found.\n", self.run_batch())
        self.assertEqual(1, AccessAttempt.objects.count())


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
