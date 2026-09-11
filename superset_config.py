"""Superset application configuration for the Green Option dashboard.

Loaded via SUPERSET_CONFIG_PATH=/opt/superset/superset_config.py (see
deployment/systemd/superset.service). Every secret and environment-specific
value comes from the process environment, populated by
EnvironmentFile=/etc/superset/superset.env in that unit - nothing sensitive
is hardcoded here, so this file stays safe to commit.
"""

from __future__ import annotations

import os

# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------
SECRET_KEY = os.environ["SUPERSET_SECRET_KEY"]

# Superset unconditionally creates this at boot (pre_init() calls
# os.makedirs(DATA_DIR) before anything else runs), so it has to be a real,
# writable path - it defaults to something under $HOME, which is /nonexistent
# for the superset service user (see systemd/superset.service) and also
# outside ProtectSystem=strict's allowlist either way. See
# deployment/README.md for the matching ReadWritePaths= / mkdir step.
DATA_DIR = "/var/lib/superset"

# Metadata only - dashboards, users, roles, RLS rules, chart definitions.
# BigQuery holds every row of actual reporting data; that connection is
# configured separately, through the Superset UI, using the dedicated
# read-only service account (GOOGLE_APPLICATION_CREDENTIALS below).
SQLALCHEMY_DATABASE_URI = os.environ["SUPERSET_DATABASE_URI"]

# Nginx (deployment/nginx/dashboard.diggies.dev.conf) is the only intended
# client, proxying to 127.0.0.1:8088 - trust its forwarded protocol/host/IP
# headers so Superset generates correct https:// links and logs real client
# IPs instead of 127.0.0.1 for every request.
ENABLE_PROXY_FIX = True

# ---------------------------------------------------------------------------
# Redis cache
#
# Separate logical DB indices per cache purpose, per Superset's own example
# config - keeps them independently inspectable/flushable (e.g. `redis-cli
# -n 2 FLUSHDB` to clear only query-result cache without touching filter
# state) and leaves room for a future second Redis consumer on this host
# without key collisions.
# ---------------------------------------------------------------------------
REDIS_HOST = os.environ.get("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))


def _redis_cache_config(db: int, key_prefix: str, timeout: int) -> dict:
    return {
        "CACHE_TYPE": "RedisCache",
        "CACHE_DEFAULT_TIMEOUT": timeout,
        "CACHE_KEY_PREFIX": key_prefix,
        "CACHE_REDIS_HOST": REDIS_HOST,
        "CACHE_REDIS_PORT": REDIS_PORT,
        "CACHE_REDIS_DB": db,
    }


# General metadata/object cache.
CACHE_CONFIG = _redis_cache_config(db=1, key_prefix="superset_cache_", timeout=300)
# Chart query result cache - the one that matters most for perceived
# dashboard speed, since it's what avoids re-querying BigQuery on every view.
DATA_CACHE_CONFIG = _redis_cache_config(db=2, key_prefix="superset_data_cache_", timeout=3600)
FILTER_STATE_CACHE_CONFIG = _redis_cache_config(db=3, key_prefix="superset_filter_", timeout=86400)
EXPLORE_FORM_DATA_CACHE_CONFIG = _redis_cache_config(db=4, key_prefix="superset_explore_", timeout=86400)

# ---------------------------------------------------------------------------
# Auth - v1 is native Superset accounts only. No self-registration, no
# anonymous/public dashboard access. Both are already Superset's defaults;
# set explicitly so the intent is a decision on record, not an accident of
# what the defaults happened to be.
# ---------------------------------------------------------------------------
AUTH_USER_REGISTRATION = False
PUBLIC_ROLE_LIKE = None

# Exposes /api/v1/security/roles/ and /api/v1/rowlevelsecurity/ so the
# PROP_* roles and RLS filters can be bootstrapped/updated by
# scripts/setup_rbac.py instead of clicked through the UI by hand for every
# property. Requires `superset init` to be rerun once this is set (see
# deployment/README.md).
FAB_ADD_SECURITY_API = True

# ---------------------------------------------------------------------------
# Feature flags - keep this list intentional; every flag here is a decision
# made for this deployment, not a default left switched on.
# ---------------------------------------------------------------------------
FEATURE_FLAGS = {
    # Lets a dashboard be restricted to specific roles (GreenOptionViewer +
    # Admin) as a second, independent layer on top of RLS: someone with
    # Gamma but not GreenOptionViewer can't open the dashboard at all,
    # rather than opening it and seeing zero rows. This does not replace or
    # weaken RLS - it gates *access to the dashboard object*, RLS still
    # gates *which rows a query returns* regardless of how the chart was
    # reached. Verify both together during RLS acceptance testing (the
    # no-PROP_* fail-closed case) before relying on it.
    "DASHBOARD_RBAC": True,
}
