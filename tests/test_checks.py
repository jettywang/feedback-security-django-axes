from django.core.checks import run_checks, Warning  # pylint: disable=redefined-builtin
from django.test import override_settings, modify_settings
from datetime import timedelta

from axes.backends import AxesStandaloneBackend
from axes.checks import Messages, Hints, Codes
from axes.conf import LockoutTier
from tests.base import AxesTestCase


class CacheCheckTestCase(AxesTestCase):
    @override_settings(
        AXES_HANDLER="axes.handlers.cache.AxesCacheHandler",
        CACHES={
            "default": {
                "BACKEND": "django.core.cache.backends.db.DatabaseCache",
                "LOCATION": "axes_cache",
            }
        },
    )
    def test_cache_check(self):
        warnings = run_checks()
        self.assertEqual(warnings, [])

    @override_settings(
        AXES_HANDLER="axes.handlers.cache.AxesCacheHandler",
        CACHES={
            "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}
        },
    )
    def test_cache_check_warnings(self):
        warnings = run_checks()
        warning = Warning(
            msg=Messages.CACHE_INVALID, hint=Hints.CACHE_INVALID, id=Codes.CACHE_INVALID
        )

        self.assertEqual(warnings, [warning])

    @override_settings(
        AXES_HANDLER="axes.handlers.database.AxesDatabaseHandler",
        CACHES={
            "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}
        },
    )
    def test_cache_check_does_not_produce_check_warnings_with_database_handler(self):
        warnings = run_checks()
        self.assertEqual(warnings, [])


class MiddlewareCheckTestCase(AxesTestCase):
    @modify_settings(MIDDLEWARE={"remove": ["axes.middleware.AxesMiddleware"]})
    def test_cache_check_warnings(self):
        warnings = run_checks()
        warning = Warning(
            msg=Messages.MIDDLEWARE_INVALID,
            hint=Hints.MIDDLEWARE_INVALID,
            id=Codes.MIDDLEWARE_INVALID,
        )

        self.assertEqual(warnings, [warning])


class AxesSpecializedBackend(AxesStandaloneBackend):
    pass


class BackendCheckTestCase(AxesTestCase):
    @modify_settings(
        AUTHENTICATION_BACKENDS={"remove": ["axes.backends.AxesStandaloneBackend"]}
    )
    def test_backend_missing(self):
        warnings = run_checks()
        warning = Warning(
            msg=Messages.BACKEND_INVALID,
            hint=Hints.BACKEND_INVALID,
            id=Codes.BACKEND_INVALID,
        )

        self.assertEqual(warnings, [warning])

    @override_settings(
        AUTHENTICATION_BACKENDS=["tests.test_checks.AxesSpecializedBackend"]
    )
    def test_specialized_backend(self):
        warnings = run_checks()
        self.assertEqual(warnings, [])

    @override_settings(
        AUTHENTICATION_BACKENDS=["tests.test_checks.AxesNotDefinedBackend"]
    )
    def test_import_error(self):
        with self.assertRaises(ImportError):
            run_checks()

    @override_settings(AUTHENTICATION_BACKENDS=["module.not_defined"])
    def test_module_not_found_error(self):
        with self.assertRaises(ModuleNotFoundError):
            run_checks()


class DeprecatedSettingsTestCase(AxesTestCase):
    def setUp(self):
        self.disable_success_access_log_warning = Warning(
            msg=Messages.SETTING_DEPRECATED.format(
                deprecated_setting="AXES_DISABLE_SUCCESS_ACCESS_LOG"
            ),
            hint=Hints.SETTING_DEPRECATED,
            id=Codes.SETTING_DEPRECATED,
        )

    @override_settings(AXES_DISABLE_SUCCESS_ACCESS_LOG=True)
    def test_deprecated_success_access_log_flag(self):
        warnings = run_checks()
        self.assertEqual(warnings, [self.disable_success_access_log_warning])


class ConfCheckTestCase(AxesTestCase):
    @override_settings(AXES_USERNAME_CALLABLE="module.not_defined")
    def test_invalid_import_path(self):
        warnings = run_checks()
        warning = Warning(
            msg=Messages.CALLABLE_INVALID.format(
                callable_setting="AXES_USERNAME_CALLABLE"
            ),
            hint=Hints.CALLABLE_INVALID.format(
                callable_setting="AXES_USERNAME_CALLABLE",
                expected_signature="get_username(request, credentials)",
            ),
            id=Codes.CALLABLE_INVALID,
        )
        self.assertEqual(warnings, [warning])

    @override_settings(
        AXES_USERNAME_CALLABLE="tests.test_checks.valid_username_callback"
    )
    def test_valid_import_path_is_imported_but_not_called(self):
        warnings = run_checks()
        self.assertEqual(warnings, [])
        self.assertFalse(valid_username_callback.called)

    @override_settings(AXES_COOLOFF_TIME=lambda request: 1)
    def test_valid_callable(self):
        warnings = run_checks()
        self.assertEqual(warnings, [])

    @override_settings(AXES_COOLOFF_TIME="module.not_defined")
    def test_cooloff_invalid_import_path(self):
        warnings = run_checks()
        warning = Warning(
            msg=Messages.CALLABLE_INVALID.format(
                callable_setting="AXES_COOLOFF_TIME"
            ),
            hint=Hints.CALLABLE_INVALID.format(
                callable_setting="AXES_COOLOFF_TIME",
                expected_signature="get_cool_off(request)",
            ),
            id=Codes.CALLABLE_INVALID,
        )
        self.assertEqual(warnings, [warning])

    @override_settings(AXES_COOLOFF_TIME=timedelta(minutes=30))
    def test_cooloff_timedelta_is_not_treated_as_callable(self):
        warnings = run_checks()
        self.assertEqual(warnings, [])

    @override_settings(AXES_FAILURE_LIMIT=7)
    def test_failure_limit_integer_is_not_treated_as_callable(self):
        warnings = run_checks()
        self.assertEqual(warnings, [])

    @override_settings(AXES_LOCKOUT_CALLABLE="tests.test_checks.not_a_callable")
    def test_import_path_pointing_at_non_callable(self):
        warnings = run_checks()
        warning = Warning(
            msg=Messages.CALLABLE_INVALID.format(
                callable_setting="AXES_LOCKOUT_CALLABLE"
            ),
            hint=Hints.CALLABLE_INVALID.format(
                callable_setting="AXES_LOCKOUT_CALLABLE",
                expected_signature="lockout(request, response, credentials)",
            ),
            id=Codes.CALLABLE_INVALID,
        )
        self.assertEqual(warnings, [warning])

    @override_settings(AXES_LOCKOUT_PARAMETERS=["ip_address"])
    def test_lockout_parameters_list_is_not_treated_as_callable(self):
        warnings = run_checks()
        self.assertEqual(warnings, [])

    def test_missing_settings_no_error(self):
        warnings = run_checks()
        self.assertEqual(warnings, [])


def valid_username_callback(request, credentials):
    valid_username_callback.called = True
    return credentials.get("username")


valid_username_callback.called = False

not_a_callable = "I am a string, not a callable"



class LockoutParametersCheckTestCase(AxesTestCase):
    @override_settings(AXES_LOCKOUT_PARAMETERS=["ip_address", "username"])
    def test_valid_configuration(self):
        warnings = run_checks()
        self.assertEqual(warnings, [])

    @override_settings(AXES_LOCKOUT_PARAMETERS=["username", "user_agent"])
    def test_invalid_configuration(self):
        warnings = run_checks()
        warning = Warning(
            msg=Messages.LOCKOUT_PARAMETERS_INVALID,
            hint=Hints.LOCKOUT_PARAMETERS_INVALID,
            id=Codes.LOCKOUT_PARAMETERS_INVALID,
        )
        self.assertEqual(warnings, [warning])


class LockoutTiersCheckTestCase(AxesTestCase):
    SAMPLE_TIERS = [
        LockoutTier(failures=3, cooloff=timedelta(minutes=15)),
        LockoutTier(failures=6, cooloff=timedelta(hours=2)),
    ]

    @override_settings(
        AXES_LOCKOUT_TIERS=SAMPLE_TIERS,
        AXES_COOLOFF_TIME=None,
        AXES_FAILURE_LIMIT=3,
    )
    def test_tiers_alone_no_warning(self):
        warnings = run_checks()
        self.assertEqual(warnings, [])

    @override_settings(AXES_LOCKOUT_TIERS=SAMPLE_TIERS, AXES_COOLOFF_TIME=1)
    def test_tiers_with_cooloff_time_warns(self):
        warnings = run_checks()
        warning = Warning(
            msg=Messages.LOCKOUT_TIERS_CONFLICT,
            hint=Hints.LOCKOUT_TIERS_CONFLICT,
            id=Codes.LOCKOUT_TIERS_CONFLICT,
        )
        self.assertIn(warning, warnings)

    @override_settings(AXES_LOCKOUT_TIERS=SAMPLE_TIERS, AXES_FAILURE_LIMIT=5)
    def test_tiers_with_failure_limit_warns(self):
        warnings = run_checks()
        warning = Warning(
            msg=Messages.LOCKOUT_TIERS_FAILURE_LIMIT_CONFLICT.format(
                failure_limit=3
            ),
            hint=Hints.LOCKOUT_TIERS_FAILURE_LIMIT_CONFLICT,
            id=Codes.LOCKOUT_TIERS_FAILURE_LIMIT_CONFLICT,
        )
        self.assertIn(warning, warnings)

    @override_settings(AXES_LOCKOUT_TIERS=SAMPLE_TIERS, AXES_FAILURE_LIMIT=3)
    def test_tiers_keep_default_failure_limit_silent(self):
        # AXES_FAILURE_LIMIT equal to the axes.conf default (3) is treated as
        # an unset value, so it does not conflict with the tier thresholds.
        warnings = run_checks()
        ids = {warning.id for warning in warnings}
        self.assertNotIn(Codes.LOCKOUT_TIERS_FAILURE_LIMIT_CONFLICT, ids)

    @override_settings(
        AXES_LOCKOUT_TIERS=SAMPLE_TIERS,
        AXES_COOLOFF_TIME=timedelta(minutes=30),
        AXES_FAILURE_LIMIT=5,
    )
    def test_mixed_legacy_and_tiers_reports_both_conflicts(self):
        warnings = run_checks()
        ids = {warning.id for warning in warnings}
        self.assertEqual(
            ids,
            {
                Codes.LOCKOUT_TIERS_CONFLICT,
                Codes.LOCKOUT_TIERS_FAILURE_LIMIT_CONFLICT,
            },
        )

    @override_settings(AXES_LOCKOUT_TIERS="not a list")
    def test_tiers_invalid_format_warns(self):
        warnings = run_checks()
        warning = Warning(
            msg=Messages.LOCKOUT_TIERS_INVALID,
            hint=Hints.LOCKOUT_TIERS_INVALID,
            id=Codes.LOCKOUT_TIERS_INVALID,
        )
        self.assertIn(warning, warnings)

    @override_settings(AXES_LOCKOUT_TIERS=[(3, timedelta(minutes=15))])
    def test_tiers_plain_tuples_warns(self):
        warnings = run_checks()
        warning = Warning(
            msg=Messages.LOCKOUT_TIERS_INVALID,
            hint=Hints.LOCKOUT_TIERS_INVALID,
            id=Codes.LOCKOUT_TIERS_INVALID,
        )
        self.assertIn(warning, warnings)
