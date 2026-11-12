from datetime import timedelta

from django.core.checks import (  # pylint: disable=redefined-builtin
    Tags,
    Warning,
    register,
)
from django.utils.module_loading import import_string

from axes.backends import AxesStandaloneBackend
from axes.conf import LockoutTier, settings


class Messages:
    CACHE_INVALID = (
        "You are using the django-axes cache handler for login attempt tracking."
        " Your cache configuration is however invalid and will not work correctly with django-axes."
        " This can leave security holes in your login systems as attempts are not tracked correctly."
        " Reconfigure settings.AXES_CACHE and settings.CACHES per django-axes configuration documentation."
    )
    MIDDLEWARE_INVALID = (
        "You do not have 'axes.middleware.AxesMiddleware' in your settings.MIDDLEWARE."
    )
    BACKEND_INVALID = "You do not have 'axes.backends.AxesStandaloneBackend' or a subclass in your settings.AUTHENTICATION_BACKENDS."
    SETTING_DEPRECATED = "You have a deprecated setting {deprecated_setting} configured in your project settings"
    CALLABLE_INVALID = "{callable_setting} is not a valid callable."
    LOCKOUT_PARAMETERS_INVALID = (
        "AXES_LOCKOUT_PARAMETERS does not contain 'ip_address'."
        " This configuration allows attackers to bypass rate limits by rotating User-Agents or Cookies."
    )
    LOCKOUT_TIERS_CONFLICT = (
        "AXES_LOCKOUT_TIERS is set alongside AXES_COOLOFF_TIME."
        " At runtime the cool-off is resolved from the matching LockoutTier"
        " and AXES_COOLOFF_TIME is never used."
    )
    LOCKOUT_TIERS_FAILURE_LIMIT_CONFLICT = (
        "AXES_LOCKOUT_TIERS is set alongside AXES_FAILURE_LIMIT."
        " At runtime the lowest tier threshold ({failure_limit}) becomes the"
        " effective failure limit and AXES_FAILURE_LIMIT is never used."
    )
    LOCKOUT_TIERS_INVALID = (
        "AXES_LOCKOUT_TIERS must be a list of LockoutTier instances."
    )


class Hints:
    CACHE_INVALID = None
    MIDDLEWARE_INVALID = None
    BACKEND_INVALID = "AxesModelBackend was renamed to AxesStandaloneBackend in django-axes version 5.0."
    SETTING_DEPRECATED = None
    CALLABLE_INVALID = (
        "Expected signature for {callable_setting}: {expected_signature}."
        " Use a direct callable or an importable dotted path string."
        " The check only imports the path; it does not invoke the callback."
    )
    LOCKOUT_PARAMETERS_INVALID = "Add 'ip_address' to AXES_LOCKOUT_PARAMETERS."
    LOCKOUT_TIERS_CONFLICT = (
        "Remove AXES_COOLOFF_TIME; each tier already carries its own cooloff."
    )
    LOCKOUT_TIERS_FAILURE_LIMIT_CONFLICT = (
        "Remove AXES_FAILURE_LIMIT; tune the first LockoutTier.failures instead."
    )
    LOCKOUT_TIERS_INVALID = "Use: AXES_LOCKOUT_TIERS = [LockoutTier(failures=3, cooloff=timedelta(minutes=15)), ...]"


class Codes:
    CACHE_INVALID = "axes.W001"
    MIDDLEWARE_INVALID = "axes.W002"
    BACKEND_INVALID = "axes.W003"
    SETTING_DEPRECATED = "axes.W004"
    CALLABLE_INVALID = "axes.W005"
    LOCKOUT_PARAMETERS_INVALID = "axes.W006"
    LOCKOUT_TIERS_CONFLICT = "axes.W007"
    LOCKOUT_TIERS_INVALID = "axes.W008"
    LOCKOUT_TIERS_FAILURE_LIMIT_CONFLICT = "axes.W009"


# Runtime signatures of the Axes callback settings, mirrored from axes.helpers.
# Only the dotted import path is resolved here; the callbacks themselves are
# never called, so the check cannot trigger any real authentication workflow.
# ``extra_allowed_types`` lists non-callable values the runtime accepts for
# settings that double as plain configuration values.
CALLABLE_SETTING_SIGNATURES = {
    "AXES_CLIENT_IP_CALLABLE": {
        "signature": "get_client_ip(request)",
        "extra_allowed_types": (),
    },
    "AXES_CLIENT_STR_CALLABLE": {
        "signature": (
            "get_client_str(username, ip_address, user_agent, path_info, request)"
        ),
        "extra_allowed_types": (),
    },
    "AXES_LOCKOUT_CALLABLE": {
        "signature": "lockout(request, response, credentials)",
        "extra_allowed_types": (),
    },
    "AXES_USERNAME_CALLABLE": {
        "signature": "get_username(request, credentials)",
        "extra_allowed_types": (),
    },
    "AXES_WHITELIST_CALLABLE": {
        "signature": "is_whitelisted(request, credentials)",
        "extra_allowed_types": (),
    },
    "AXES_FAILURE_LIMIT": {
        "signature": "get_failure_limit(request, credentials)",
        "extra_allowed_types": (int,),
    },
    "AXES_COOLOFF_TIME": {
        "signature": "get_cool_off(request)",
        "extra_allowed_types": (int, float, timedelta),
    },
    "AXES_LOCKOUT_PARAMETERS": {
        "signature": "get_lockout_parameters(request_or_attempt, credentials)",
        "extra_allowed_types": (list, tuple),
    },
}


@register(Tags.security, Tags.caches, Tags.compatibility)
def axes_cache_check(app_configs, **kwargs):  # pylint: disable=unused-argument
    axes_handler = getattr(settings, "AXES_HANDLER", "")

    axes_cache_key = getattr(settings, "AXES_CACHE", "default")
    axes_cache_config = settings.CACHES.get(axes_cache_key, {})
    axes_cache_backend = axes_cache_config.get("BACKEND", "")

    axes_cache_backend_incompatible = [
        "django.core.cache.backends.dummy.DummyCache",
        "django.core.cache.backends.locmem.LocMemCache",
        "django.core.cache.backends.filebased.FileBasedCache",
    ]

    warnings = []

    if axes_handler == "axes.handlers.cache.AxesCacheHandler":
        if axes_cache_backend in axes_cache_backend_incompatible:
            warnings.append(
                Warning(
                    msg=Messages.CACHE_INVALID,
                    hint=Hints.CACHE_INVALID,
                    id=Codes.CACHE_INVALID,
                )
            )

    return warnings


@register(Tags.security, Tags.compatibility)
def axes_middleware_check(app_configs, **kwargs):  # pylint: disable=unused-argument
    warnings = []

    if "axes.middleware.AxesMiddleware" not in settings.MIDDLEWARE:
        warnings.append(
            Warning(
                msg=Messages.MIDDLEWARE_INVALID,
                hint=Hints.MIDDLEWARE_INVALID,
                id=Codes.MIDDLEWARE_INVALID,
            )
        )

    return warnings


@register(Tags.security, Tags.compatibility)
def axes_backend_check(app_configs, **kwargs):  # pylint: disable=unused-argument
    warnings = []

    found = False
    for name in settings.AUTHENTICATION_BACKENDS:
        try:
            backend = import_string(name)
        except ModuleNotFoundError as e:
            raise ModuleNotFoundError(
                "Can not find module path defined in settings.AUTHENTICATION_BACKENDS"
            ) from e
        except ImportError as e:
            raise ImportError(
                "Can not import backend class defined in settings.AUTHENTICATION_BACKENDS"
            ) from e

        if issubclass(backend, AxesStandaloneBackend):
            found = True
            break

    if not found:
        warnings.append(
            Warning(
                msg=Messages.BACKEND_INVALID,
                hint=Hints.BACKEND_INVALID,
                id=Codes.BACKEND_INVALID,
            )
        )

    return warnings


@register(Tags.compatibility)
def axes_deprecation_check(app_configs, **kwargs):  # pylint: disable=unused-argument
    warnings = []

    deprecated_settings = [
        "AXES_DISABLE_SUCCESS_ACCESS_LOG",
        "AXES_LOGGER",
        # AXES_PROXY_ and AXES_META_ parameters were updated to more explicit
        # AXES_IPWARE_PROXY_ and AXES_IPWARE_META_ prefixes in version 6.x
        "AXES_PROXY_ORDER",
        "AXES_PROXY_COUNT",
        "AXES_PROXY_TRUSTED_IPS",
        "AXES_META_PRECEDENCE_ORDER",
        # AXES_ONLY_USER_FAILURES, AXES_USE_USER_AGENT and
        # AXES_LOCK_OUT parameters were replaced with AXES_LOCKOUT_PARAMETERS
        # in version 6.x
        "AXES_ONLY_USER_FAILURES",
        "AXES_LOCK_OUT_BY_USER_OR_IP",
        "AXES_LOCK_OUT_BY_COMBINATION_USER_AND_IP",
        "AXES_USE_USER_AGENT",
    ]

    for deprecated_setting in deprecated_settings:
        try:
            getattr(settings, deprecated_setting)
            warnings.append(
                Warning(
                    msg=Messages.SETTING_DEPRECATED.format(
                        deprecated_setting=deprecated_setting
                    ),
                    hint=None,
                    id=Codes.SETTING_DEPRECATED,
                )
            )
        except AttributeError:
            pass

    return warnings


@register(Tags.security)
def axes_lockout_params_check(app_configs, **kwargs):  # pylint: disable=unused-argument
    warnings = []

    lockout_params = getattr(settings, "AXES_LOCKOUT_PARAMETERS", None)

    if isinstance(lockout_params, (list, tuple)):
        has_ip = False
        for param in lockout_params:
            if param == "ip_address":
                has_ip = True
                break
            if isinstance(param, (list, tuple)) and "ip_address" in param:
                has_ip = True
                break

        if not has_ip:
            warnings.append(
                Warning(
                    msg=Messages.LOCKOUT_PARAMETERS_INVALID,
                    hint=Hints.LOCKOUT_PARAMETERS_INVALID,
                    id=Codes.LOCKOUT_PARAMETERS_INVALID,
                )
            )

    return warnings


@register(Tags.security)
def axes_lockout_tiers_check(app_configs, **kwargs):  # pylint: disable=unused-argument
    warnings = []
    tiers = getattr(settings, "AXES_LOCKOUT_TIERS", None)
    if tiers is None:
        return warnings

    if not is_valid_tiers_list(tiers):
        warnings.append(
            Warning(
                msg=Messages.LOCKOUT_TIERS_INVALID,
                hint=Hints.LOCKOUT_TIERS_INVALID,
                id=Codes.LOCKOUT_TIERS_INVALID,
            )
        )
        return warnings

    if is_non_default_setting("AXES_COOLOFF_TIME"):
        warnings.append(
            Warning(
                msg=Messages.LOCKOUT_TIERS_CONFLICT,
                hint=Hints.LOCKOUT_TIERS_CONFLICT,
                id=Codes.LOCKOUT_TIERS_CONFLICT,
            )
        )

    if is_non_default_setting("AXES_FAILURE_LIMIT"):
        effective_failure_limit = min(tier.failures for tier in tiers)
        warnings.append(
            Warning(
                msg=Messages.LOCKOUT_TIERS_FAILURE_LIMIT_CONFLICT.format(
                    failure_limit=effective_failure_limit
                ),
                hint=Hints.LOCKOUT_TIERS_FAILURE_LIMIT_CONFLICT,
                id=Codes.LOCKOUT_TIERS_FAILURE_LIMIT_CONFLICT,
            )
        )

    return warnings


def is_valid_tiers_list(tiers) -> bool:
    if not isinstance(tiers, (list, tuple)):
        return False
    return all(isinstance(t, LockoutTier) for t in tiers)


def is_non_default_setting(setting_name: str) -> bool:
    """
    Return whether *setting_name* holds a value other than the axes.conf
    default and would therefore take part in runtime resolution.

    The value comparison is used instead of Django's private settings
    bookkeeping so the result is identical under plain settings modules and
    test helpers such as ``override_settings``.
    """

    default_values = {
        "AXES_FAILURE_LIMIT": 3,
        "AXES_COOLOFF_TIME": None,
    }
    return (
        getattr(settings, setting_name, None) != default_values.get(setting_name)
    )


@register
def axes_conf_check(app_configs, **kwargs):  # pylint: disable=unused-argument
    warnings = []

    for callable_setting, spec in CALLABLE_SETTING_SIGNATURES.items():
        value = getattr(settings, callable_setting, None)
        if not is_valid_callable(value, spec["extra_allowed_types"]):
            warnings.append(
                Warning(
                    msg=Messages.CALLABLE_INVALID.format(
                        callable_setting=callable_setting
                    ),
                    hint=Hints.CALLABLE_INVALID.format(
                        callable_setting=callable_setting,
                        expected_signature=spec["signature"],
                    ),
                    id=Codes.CALLABLE_INVALID,
                )
            )

    return warnings


def is_valid_callable(value, extra_allowed_types=()) -> bool:
    if value is None:
        return True

    if isinstance(value, extra_allowed_types):
        return True

    if callable(value):
        return True

    if isinstance(value, str):
        try:
            imported = import_string(value)
        except ImportError:
            return False
        return callable(imported)

    return False
