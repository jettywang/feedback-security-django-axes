"""
Tests for authentication entry point scopes.

Employee and customer login entry points can share the same username format,
but their failure counts must be tracked independently when the application
supplies an entry point identifier through ``AXES_SCOPE_CALLABLE``. When no
callable is configured the shared default scope keeps the legacy behaviour.
"""
from io import StringIO

from django.core.management import call_command
from django.test import override_settings

from axes.helpers import get_client_parameters, get_client_scope
from axes.handlers.proxy import AxesProxyHandler
from axes.models import AccessAttempt, AccessFailureLog, AccessLog
from axes.utils import reset
from tests.base import AxesTestCase

EMPLOYEE_SCOPE = "employee"
CUSTOMER_SCOPE = "customer"


def get_scope_from_path(request, credentials=None):
    path = getattr(request, "path", "") or ""
    if path.startswith("/employee-login/"):
        return EMPLOYEE_SCOPE
    if path.startswith("/customer-login/"):
        return CUSTOMER_SCOPE
    return ""


SCOPE_LOCKOUT_PARAMETERS = ["scope", ["username", "scope"]]


class ScopeTestCase(AxesTestCase):
    def scoped_request(self, scope):
        request = self.request.__class__()
        request.method = "POST"
        request.META = self.request.META.copy()
        request.path = f"/{scope}-login/"
        request.META["PATH_INFO"] = request.path
        request.POST = {}
        return request

    def failed(self, request, username=None):
        credentials = {"username": username or self.username, "password": "wrong"}
        AxesProxyHandler.user_login_failed(
            sender=None, request=request, credentials=credentials
        )
        return request

    def failures(self, request, username=None):
        credentials = {"username": username or self.username, "password": "wrong"}
        return AxesProxyHandler.get_failures(request, credentials)

    def success(self, request):
        AxesProxyHandler.user_logged_in(
            sender=None, request=request, user=self.user
        )


@override_settings(
    AXES_SCOPE_CALLABLE="tests.test_scope.get_scope_from_path",
    AXES_LOCKOUT_PARAMETERS=SCOPE_LOCKOUT_PARAMETERS,
    AXES_RESET_ON_SUCCESS=True,
    AXES_FAILURE_LIMIT=3,
)
class DatabaseScopeTestCase(ScopeTestCase):
    def test_scope_is_resolved_from_request(self):
        employee = self.scoped_request(EMPLOYEE_SCOPE)
        customer = self.scoped_request(CUSTOMER_SCOPE)
        self.assertEqual(get_client_scope(employee), EMPLOYEE_SCOPE)
        self.assertEqual(get_client_scope(customer), CUSTOMER_SCOPE)

    def test_scope_is_part_of_client_parameters(self):
        employee = self.scoped_request(EMPLOYEE_SCOPE)
        AxesProxyHandler.update_request(employee)
        parameters = get_client_parameters(
            self.username,
            employee.axes_ip_address,
            employee.axes_user_agent,
            employee,
            self.credentials,
            employee.axes_scope,
        )
        self.assertEqual(
            parameters,
            [
                {"scope": EMPLOYEE_SCOPE},
                {"username": self.username, "scope": EMPLOYEE_SCOPE},
            ],
        )

    def test_failures_are_counted_per_scope(self):
        employee = self.scoped_request(EMPLOYEE_SCOPE)
        customer = self.scoped_request(CUSTOMER_SCOPE)

        self.failed(employee)
        self.failed(employee)
        self.failed(customer)

        self.assertEqual(self.failures(employee), 2)
        self.assertEqual(self.failures(customer), 1)

        self.assertEqual(
            AccessAttempt.objects.get(scope=EMPLOYEE_SCOPE).failures_since_start, 2
        )
        self.assertEqual(
            AccessAttempt.objects.get(scope=CUSTOMER_SCOPE).failures_since_start, 1
        )

    def test_lockout_is_scoped(self):
        employee = self.scoped_request(EMPLOYEE_SCOPE)
        customer = self.scoped_request(CUSTOMER_SCOPE)

        for _ in range(3):
            self.failed(employee)

        self.assertTrue(AxesProxyHandler.is_locked(employee, self.credentials))
        self.assertFalse(AxesProxyHandler.is_locked(customer, self.credentials))

    def test_success_in_one_scope_does_not_reset_other_scope(self):
        employee = self.scoped_request(EMPLOYEE_SCOPE)
        customer = self.scoped_request(CUSTOMER_SCOPE)

        self.failed(employee)
        self.failed(employee)
        self.failed(customer)

        self.success(employee)

        self.assertEqual(self.failures(employee), 0)
        self.assertEqual(self.failures(customer), 1)
        self.assertFalse(AccessAttempt.objects.filter(scope=EMPLOYEE_SCOPE).exists())
        self.assertTrue(AccessAttempt.objects.filter(scope=CUSTOMER_SCOPE).exists())
        self.assertEqual(AccessLog.objects.get().scope, EMPLOYEE_SCOPE)

    def test_interleaved_failures_and_successes_stay_isolated(self):
        employee = self.scoped_request(EMPLOYEE_SCOPE)
        customer = self.scoped_request(CUSTOMER_SCOPE)

        self.failed(employee)
        self.failed(customer)
        self.failed(employee)
        self.success(customer)
        self.failed(customer)
        self.failed(employee)

        self.assertEqual(self.failures(employee), 3)
        self.assertEqual(self.failures(customer), 1)
        self.assertTrue(AxesProxyHandler.is_locked(employee, self.credentials))
        self.assertFalse(AxesProxyHandler.is_locked(customer, self.credentials))

    @override_settings(AXES_ENABLE_ACCESS_FAILURE_LOG=True)
    def test_failure_logs_keep_the_scope(self):
        employee = self.scoped_request(EMPLOYEE_SCOPE)
        self.failed(employee)
        self.assertEqual(AccessFailureLog.objects.get().scope, EMPLOYEE_SCOPE)

    def test_admin_reset_username_only_affects_given_scope(self):
        employee = self.scoped_request(EMPLOYEE_SCOPE)
        customer = self.scoped_request(CUSTOMER_SCOPE)
        self.failed(employee)
        self.failed(customer)

        count = reset(username=self.username, scope=EMPLOYEE_SCOPE)

        self.assertEqual(count, 1)
        self.assertFalse(AccessAttempt.objects.filter(scope=EMPLOYEE_SCOPE).exists())
        self.assertTrue(AccessAttempt.objects.filter(scope=CUSTOMER_SCOPE).exists())

    def test_admin_reset_without_scope_still_resets_every_scope(self):
        employee = self.scoped_request(EMPLOYEE_SCOPE)
        customer = self.scoped_request(CUSTOMER_SCOPE)
        self.failed(employee)
        self.failed(customer)

        count = reset(username=self.username)

        self.assertEqual(count, 2)
        self.assertFalse(AccessAttempt.objects.exists())

    def test_admin_reset_scope_command_only_affects_given_scope(self):
        employee = self.scoped_request(EMPLOYEE_SCOPE)
        customer = self.scoped_request(CUSTOMER_SCOPE)
        self.failed(employee)
        self.failed(customer)

        out = StringIO()
        call_command("axes_reset_scope", EMPLOYEE_SCOPE, stdout=out)

        self.assertEqual("1 attempts removed.\n", out.getvalue())
        self.assertFalse(AccessAttempt.objects.filter(scope=EMPLOYEE_SCOPE).exists())
        self.assertTrue(AccessAttempt.objects.filter(scope=CUSTOMER_SCOPE).exists())

    def test_axes_reset_username_command_supports_scope(self):
        employee = self.scoped_request(EMPLOYEE_SCOPE)
        customer = self.scoped_request(CUSTOMER_SCOPE)
        self.failed(employee)
        self.failed(customer)

        out = StringIO()
        call_command(
            "axes_reset_username",
            self.username,
            scope=EMPLOYEE_SCOPE,
            stdout=out,
        )

        self.assertEqual("1 attempts removed.\n", out.getvalue())
        self.assertTrue(AccessAttempt.objects.filter(scope=CUSTOMER_SCOPE).exists())


@override_settings(
    AXES_HANDLER="axes.handlers.cache.AxesCacheHandler",
    AXES_SCOPE_CALLABLE="tests.test_scope.get_scope_from_path",
    AXES_LOCKOUT_PARAMETERS=SCOPE_LOCKOUT_PARAMETERS,
    AXES_RESET_ON_SUCCESS=True,
    AXES_FAILURE_LIMIT=3,
)
class CacheScopeTestCase(ScopeTestCase):
    def test_failures_are_counted_per_scope(self):
        employee = self.scoped_request(EMPLOYEE_SCOPE)
        customer = self.scoped_request(CUSTOMER_SCOPE)

        self.failed(employee)
        self.failed(employee)
        self.failed(customer)

        self.assertEqual(self.failures(employee), 2)
        self.assertEqual(self.failures(customer), 1)
        self.assertFalse(AccessAttempt.objects.exists())

    def test_lockout_is_scoped(self):
        employee = self.scoped_request(EMPLOYEE_SCOPE)
        customer = self.scoped_request(CUSTOMER_SCOPE)

        for _ in range(3):
            self.failed(employee)

        self.assertTrue(AxesProxyHandler.is_locked(employee, self.credentials))
        self.assertFalse(AxesProxyHandler.is_locked(customer, self.credentials))

    def test_success_in_one_scope_does_not_reset_other_scope(self):
        employee = self.scoped_request(EMPLOYEE_SCOPE)
        customer = self.scoped_request(CUSTOMER_SCOPE)

        self.failed(employee)
        self.failed(employee)
        self.failed(customer)

        self.success(employee)

        self.assertEqual(self.failures(employee), 0)
        self.assertEqual(self.failures(customer), 1)

    def test_interleaved_failures_and_successes_stay_isolated(self):
        employee = self.scoped_request(EMPLOYEE_SCOPE)
        customer = self.scoped_request(CUSTOMER_SCOPE)

        self.failed(employee)
        self.failed(customer)
        self.failed(employee)
        self.success(customer)
        self.failed(customer)
        self.failed(employee)

        self.assertEqual(self.failures(employee), 3)
        self.assertEqual(self.failures(customer), 1)
        self.assertTrue(AxesProxyHandler.is_locked(employee, self.credentials))
        self.assertFalse(AxesProxyHandler.is_locked(customer, self.credentials))


class DefaultScopeTestCase(ScopeTestCase):
    def test_unconfigured_callable_uses_shared_empty_scope(self):
        employee = self.scoped_request(EMPLOYEE_SCOPE)
        self.assertEqual(get_client_scope(employee), "")

    @override_settings(AXES_LOCKOUT_PARAMETERS=["username"])
    def test_failures_are_shared_without_scope_callable(self):
        employee = self.scoped_request(EMPLOYEE_SCOPE)
        customer = self.scoped_request(CUSTOMER_SCOPE)

        self.failed(employee)
        self.failed(customer)

        self.assertEqual(self.failures(employee), 2)
        self.assertEqual(self.failures(customer), 2)
        self.assertEqual(AccessAttempt.objects.count(), 1)
        self.assertEqual(AccessAttempt.objects.get().scope, "")


@override_settings(
    ROOT_URLCONF="tests.urls_scoped",
    AXES_SCOPE_CALLABLE="tests.test_scope.get_scope_from_path",
    AXES_LOCKOUT_PARAMETERS=SCOPE_LOCKOUT_PARAMETERS,
    AXES_RESET_ON_SUCCESS=True,
    AXES_FAILURE_LIMIT=3,
)
class HttpScopeTestCase(ScopeTestCase):
    def post_login(self, scope, *, valid):
        url = f"/{scope}-login/"
        data = {
            "username": self.username,
            "password": self.VALID_PASSWORD if valid else "wrong-password",
        }
        return self.client.post(
            url,
            data,
            REMOTE_ADDR=self.ip_address,
            headers={"user-agent": self.user_agent},
        )

    def test_scoped_lockout_over_http(self):
        for _ in range(2):
            response = self.post_login(EMPLOYEE_SCOPE, valid=False)
            self.assertEqual(response.status_code, 200)

        employee_locked = self.post_login(EMPLOYEE_SCOPE, valid=False)
        self.assertEqual(employee_locked.status_code, 429)

        employee_locked = self.post_login(EMPLOYEE_SCOPE, valid=True)
        self.assertEqual(employee_locked.status_code, 429)

        customer_allowed = self.post_login(CUSTOMER_SCOPE, valid=True)
        self.assertEqual(customer_allowed.status_code, 302)

        self.assertEqual(
            AccessAttempt.objects.filter(scope=EMPLOYEE_SCOPE).count(), 1
        )
        self.assertFalse(
            AccessAttempt.objects.filter(scope=CUSTOMER_SCOPE).exists()
        )

    def test_success_on_customer_entry_keeps_employee_lockout(self):
        for _ in range(3):
            self.post_login(EMPLOYEE_SCOPE, valid=False)

        employee_attempt = AccessAttempt.objects.get(scope=EMPLOYEE_SCOPE)
        employee_failures_before = employee_attempt.failures_since_start
        self.assertEqual(employee_failures_before, 3)

        customer_response = self.post_login(CUSTOMER_SCOPE, valid=True)
        self.assertEqual(customer_response.status_code, 302)

        employee_attempt.refresh_from_db()
        self.assertEqual(
            employee_attempt.failures_since_start, employee_failures_before
        )
        self.assertFalse(
            AccessAttempt.objects.filter(scope=CUSTOMER_SCOPE).exists()
        )
        employee_still_locked = self.post_login(EMPLOYEE_SCOPE, valid=True)
        self.assertEqual(employee_still_locked.status_code, 429)

class ScopeCallableEdgeCaseTestCase(ScopeTestCase):
    @override_settings(AXES_SCOPE_CALLABLE="tests.test_scope.get_scope_from_path")
    def test_scope_is_truncated_to_field_length(self):
        from axes.models import AccessBase

        max_length = AccessBase._meta.get_field("scope").max_length
        long_scope = "x" * (max_length + 10)
        self.request.path = "/employee-login/"

        with override_settings(
            AXES_SCOPE_CALLABLE=lambda request, credentials=None: long_scope
        ):
            scope = get_client_scope(self.request)

        self.assertEqual(len(scope), max_length)

    @override_settings(
        AXES_SCOPE_CALLABLE=lambda request, credentials=None: 42
    )
    def test_non_string_scope_is_coerced_to_string(self):
        self.assertEqual(get_client_scope(self.request), "42")

    @override_settings(
        AXES_SCOPE_CALLABLE=lambda request, credentials=None: None
    )
    def test_none_scope_falls_back_to_default_scope(self):
        self.assertEqual(get_client_scope(self.request), "")

    def test_invalid_scope_callable_type_raises(self):
        with override_settings(AXES_SCOPE_CALLABLE=42):
            with self.assertRaises(TypeError):
                get_client_scope(self.request)

    @override_settings(AXES_SCOPE_CALLABLE="tests.test_scope.missing_callable")
    def test_missing_scope_callable_path_raises(self):
        with self.assertRaises(ImportError):
            get_client_scope(self.request)


@override_settings(AXES_SCOPE_CALLABLE="tests.test_scope.get_scope_from_path")
class ScopeClientStringTestCase(ScopeTestCase):
    @override_settings(AXES_VERBOSE=False)
    @override_settings(AXES_LOCKOUT_PARAMETERS=SCOPE_LOCKOUT_PARAMETERS)
    def test_non_verbose_client_string_contains_scope(self):
        from axes.helpers import get_client_str

        request = self.scoped_request(EMPLOYEE_SCOPE)
        AxesProxyHandler.update_request(request)
        client_str = get_client_str(
            self.username,
            self.ip_address,
            self.user_agent,
            request.path,
            request,
        )
        self.assertIn('scope: "employee"', client_str)
        self.assertIn("username:", client_str)

    @override_settings(AXES_VERBOSE=True)
    def test_verbose_client_string_contains_scope(self):
        from axes.helpers import get_client_str

        request = self.scoped_request(CUSTOMER_SCOPE)
        AxesProxyHandler.update_request(request)
        client_str = get_client_str(
            self.username,
            self.ip_address,
            self.user_agent,
            request.path,
            request,
        )
        self.assertIn('scope: "customer"', client_str)

    @override_settings(AXES_VERBOSE=True)
    def test_verbose_client_string_without_scope_callable_omits_scope(self):
        from axes.helpers import get_client_str

        with override_settings(AXES_SCOPE_CALLABLE=None):
            client_str = get_client_str(
                self.username,
                self.ip_address,
                self.user_agent,
                self.path_info,
                self.request,
            )
        self.assertNotIn("scope", client_str)


@override_settings(
    AXES_SCOPE_CALLABLE="tests.test_scope.get_scope_from_path",
    AXES_LOCKOUT_PARAMETERS=SCOPE_LOCKOUT_PARAMETERS,
    AXES_FAILURE_LIMIT=3,
)
class ResetRequestScopeTestCase(ScopeTestCase):
    def test_reset_request_only_clears_the_request_scope(self):
        from axes.utils import reset_request

        employee = self.scoped_request(EMPLOYEE_SCOPE)
        customer = self.scoped_request(CUSTOMER_SCOPE)
        AxesProxyHandler.update_request(employee)
        AxesProxyHandler.update_request(customer)

        self.failed(employee)
        self.failed(customer)

        employee.GET = {"username": self.username}
        count = reset_request(employee)

        self.assertEqual(count, 1)
        self.assertFalse(AccessAttempt.objects.filter(scope=EMPLOYEE_SCOPE).exists())
        self.assertTrue(AccessAttempt.objects.filter(scope=CUSTOMER_SCOPE).exists())

    def test_axes_list_attempts_reports_scope(self):
        employee = self.scoped_request(EMPLOYEE_SCOPE)
        self.failed(employee)

        out = StringIO()
        call_command("axes_list_attempts", stdout=out)

        self.assertIn(
            f"{self.ip_address}\t{self.username}\t{EMPLOYEE_SCOPE}\t1\n",
            out.getvalue(),
        )
