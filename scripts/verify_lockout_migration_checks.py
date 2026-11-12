"""
Verify the Axes lockout-migration system checks against three temporary
settings modules:

1. legacy   - old AXES_FAILURE_LIMIT / AXES_COOLOFF_TIME configuration only
2. mixed    - legacy options left behind while migrating to AXES_LOCKOUT_TIERS
3. tiered   - valid AXES_LOCKOUT_TIERS configuration, including callbacks
              referenced by dotted import paths

Each scenario runs ``manage.py check`` in its own subprocess with a throwaway
settings package written under $TMPDIR. The checks only import callback paths;
the callbacks raise if they are ever invoked, proving the preflight never
executes a real authentication callback.

The temporary settings modules never import the axes package while the Django
settings object is being built. Importing axes.conf during that window rebuilds
the settings object and drops the axes defaults; the axes AppConfig loads
axes.conf itself once the app registry is ready. LockoutTier instances are
therefore built lazily through a SimpleLazyObject helper.
"""

import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

BASE_SETTINGS = textwrap.dedent(
    '''
    DATABASES = {
        "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}
    }
    CACHES = {
        "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}
    }

    SITE_ID = 1
    SECRET_KEY = "temporary-migration-check-secret"
    USE_I18N = False
    USE_TZ = False
    ROOT_URLCONF = "tests.urls"
    DEFAULT_AUTO_FIELD = "django.db.models.AutoField"
    PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

    INSTALLED_APPS = [
        "django.contrib.auth",
        "django.contrib.contenttypes",
        "django.contrib.sessions",
        "django.contrib.sites",
        "django.contrib.messages",
        "django.contrib.admin",
        "axes",
    ]

    MIDDLEWARE = [
        "django.middleware.common.CommonMiddleware",
        "django.contrib.sessions.middleware.SessionMiddleware",
        "django.contrib.auth.middleware.AuthenticationMiddleware",
        "django.contrib.messages.middleware.MessageMiddleware",
        "axes.middleware.AxesMiddleware",
    ]

    AUTHENTICATION_BACKENDS = [
        "axes.backends.AxesStandaloneBackend",
        "django.contrib.auth.backends.ModelBackend",
    ]

    TEMPLATES = [
        {
            "BACKEND": "django.template.backends.django.DjangoTemplates",
            "DIRS": [],
            "APP_DIRS": True,
            "OPTIONS": {
                "context_processors": [
                    "django.template.context_processors.debug",
                    "django.template.context_processors.request",
                    "django.contrib.auth.context_processors.auth",
                    "django.contrib.messages.context_processors.messages",
                ]
            },
        }
    ]
    '''
)

CALLBACKS_MODULE = textwrap.dedent(
    '''
    """Temporary callbacks for the migration-check verification."""


    def explode_if_called(*args, **kwargs):
        raise AssertionError(
            "System checks must never invoke an Axes authentication callback"
        )


    def get_client_ip(request):
        explode_if_called()


    def get_username(request, credentials):
        explode_if_called()
    '''
)

# Builds LockoutTier instances without importing axes while the settings object
# is still being constructed; the lazy object is resolved only after startup.
TIERS_MODULE = textwrap.dedent(
    '''
    from django.utils.functional import SimpleLazyObject


    def build_tiers():
        from datetime import timedelta

        from axes.conf import LockoutTier

        return [
            LockoutTier(failures=3, cooloff=timedelta(minutes=15)),
            LockoutTier(failures=6, cooloff=timedelta(hours=2)),
            LockoutTier(failures=10, cooloff=timedelta(days=1)),
        ]


    TIERS = SimpleLazyObject(build_tiers)
    '''
)

SCENARIOS = {
    "legacy": textwrap.dedent(
        """
        from datetime import timedelta

        # Pre-tier configuration: still fully supported, nothing to rewrite.
        AXES_FAILURE_LIMIT = 5
        AXES_COOLOFF_TIME = timedelta(minutes=30)
        AXES_LOCKOUT_PARAMETERS = ["ip_address"]

        # Import-path callbacks are imported (not called) during the checks.
        AXES_CLIENT_IP_CALLABLE = "tmp_axes_callbacks.get_client_ip"
        AXES_USERNAME_CALLABLE = "tmp_axes_callbacks.get_username"
        """
    ),
    "mixed": textwrap.dedent(
        """
        from datetime import timedelta

        # Half-finished migration: tiers were added but the legacy options and
        # a stale import path were left behind.
        from tmp_axes_tiers import TIERS

        AXES_LOCKOUT_TIERS = TIERS
        AXES_FAILURE_LIMIT = 5
        AXES_COOLOFF_TIME = timedelta(minutes=30)

        # Broken dotted path: without the check this only surfaces at first login.
        AXES_LOCKOUT_CALLABLE = "tmp_axes_callbacks.does_not_exist"
        """
    ),
    "tiered": textwrap.dedent(
        """
        # Valid post-migration configuration: no warnings expected.
        from tmp_axes_tiers import TIERS

        AXES_LOCKOUT_TIERS = TIERS
        AXES_LOCKOUT_PARAMETERS = ["ip_address"]
        AXES_CLIENT_IP_CALLABLE = "tmp_axes_callbacks.get_client_ip"
        AXES_USERNAME_CALLABLE = "tmp_axes_callbacks.get_username"
        """
    ),
}

# Warning ids each scenario is expected to produce from the Axes checks.
EXPECTED_WARNINGS = {
    "legacy": set(),
    "mixed": {"axes.W005", "axes.W007", "axes.W009"},
    "tiered": set(),
}

ALL_AXES_WARNING_IDS = {f"axes.W00{n}" for n in range(1, 10)}


def run_scenario(name, body, workdir):
    settings_dir = workdir / f"settings_{name}"
    settings_dir.mkdir()
    (settings_dir / "__init__.py").touch()
    (settings_dir / "tmp_axes_callbacks.py").write_text(CALLBACKS_MODULE)
    (settings_dir / "tmp_axes_tiers.py").write_text(TIERS_MODULE)
    (settings_dir / "settings_module.py").write_text(
        BASE_SETTINGS + "\n" + body
    )

    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_ROOT), str(settings_dir), env.get("PYTHONPATH", "")]
    )
    env["DJANGO_SETTINGS_MODULE"] = "settings_module"

    result = subprocess.run(
        [sys.executable, "manage.py", "check"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    output = result.stdout + result.stderr
    found = {code for code in ALL_AXES_WARNING_IDS if code in output}
    return result, output, found


def main():
    failures = []
    with tempfile.TemporaryDirectory(prefix="axes-migration-check-") as tmp:
        workdir = Path(tmp)
        for name, body in SCENARIOS.items():
            result, output, found = run_scenario(name, body, workdir)
            expected = EXPECTED_WARNINGS[name]

            print(f"=== scenario: {name} ===")
            print(output.strip() or "(no check output)")
            print(f"expected warnings: {sorted(expected) or 'none'}")
            print(f"found warnings:    {sorted(found) or 'none'}")
            print()

            if result.returncode != 0 and not found:
                failures.append(f"{name}: check command failed to start")
            if found != expected:
                failures.append(
                    f"{name}: expected {sorted(expected)}, got {sorted(found)}"
                )
            if "AssertionError" in output and "never invoke" in output:
                failures.append(
                    f"{name}: an authentication callback was executed"
                )

    if failures:
        print("MIGRATION CHECK VERIFICATION FAILED")
        for failure in failures:
            print(f" - {failure}")
        return 1

    print("All three temporary settings scenarios behaved as expected.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
