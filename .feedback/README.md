# Feedback task starting environment

Upstream: https://github.com/django-commons/django-axes
Upstream commit: `ba9926171dd93e1006c23bb54cb6bfc274b21fd6`
Prepared on macOS with CPython 3.12.11. No external database, queue, or cloud account is required.

The dependency file pins the installed runtime and test packages for the selected baseline. It is not a claim that all optional upstream test suites are installed.

From the repository root:

```sh
export NO_INTERNET=1 TZ=UTC UV_NO_CONFIG=1

uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r .feedback/requirements.txt -e .
.venv/bin/python -m pytest -q -o addopts= tests/test_checks.py::ConfCheckTestCase tests/test_checks.py::LockoutParametersCheckTestCase tests/test_checks.py::LockoutTiersCheckTestCase tests/test_handlers.py::AxesHandlerTestCase::test_is_allowed_no_lock_out
```

Scope note: The scope is the command below, not all optional upstream integrations.

Observed baseline (2026-11-12): `10 passed in 0.15s`
Whole-command wall time: 0.424 seconds, exit code 0. This is one local measurement, not a runtime guarantee. The first install is excluded.

This seed adds preparation metadata only. Application sources and upstream tests are unchanged; there is no task solution in this commit.
