# Deploying the Green Option dashboard (Superset)

Native install on the existing server, matching how `scylla`, `diggies-api`,
and `morning-shift` already run there - a venv + a process manager under
systemd, proxied by nginx. No Docker: the original brief assumed a
docker-compose stack, but this host has no Docker anywhere on it today, and
everything about that model (port-binding defaults, reaching a host-native
Postgres from a container network, a second log/process paradigm, `docker`
group members being root-equivalent) works against the conventions already
in place here. See `../.env.example`, `../superset_config.py`,
`systemd/superset.service`, and `nginx/dashboard.diggies.dev.conf`.

Postgres is already running on this host as `postgresql.service` (confirmed
via `diggies-api`, which connects to it the same way this deployment does).
Redis is not yet used by anything here - it's a new, small addition.

## 1. System user

```bash
sudo useradd --system --user-group --home-dir /nonexistent \
  --shell /usr/sbin/nologin superset
sudo mkdir -p /opt/superset /etc/superset /var/lib/superset
sudo chown root:superset /etc/superset
sudo chmod 750 /etc/superset
sudo chown superset:superset /var/lib/superset
sudo chmod 750 /var/lib/superset
```

`/var/lib/superset` is Superset's `DATA_DIR` (see `superset_config.py`) - it
creates this unconditionally at boot regardless of the `/nonexistent` home
dir above, so it needs to exist and be writable before the first `superset
db upgrade` in step 4, not just before the systemd service in step 5.

## 2. PostgreSQL - dedicated database and role

Run as a Postgres superuser. This mirrors the per-service database + role
pattern `diggies-api` already uses (its own `diggies_api` role scoped to its
own `diggies_api` database) - `superset` gets no access to any other
service's data, and no other service's role gets access to Superset's
metadata.

```sql
CREATE ROLE superset WITH LOGIN PASSWORD 'CHANGE_ME__STRONG_RANDOM_PASSWORD';
CREATE DATABASE superset_metadata OWNER superset;
REVOKE ALL ON DATABASE superset_metadata FROM PUBLIC;
GRANT ALL PRIVILEGES ON DATABASE superset_metadata TO superset;
```

Put the resulting connection string in `/etc/superset/superset.env` as
`SUPERSET_DATABASE_URI` (URL-encode the password if it contains reserved
characters). No `pg_hba.conf` change should be needed if it already permits
password auth for local/loopback connections the way it does for
`diggies-api` and `morning-shift` - confirm rather than assume.

## 3. Redis

```bash
sudo apt install redis-server
```

Confirm `/etc/redis/redis.conf` has `bind 127.0.0.1 -::1` and
`protected-mode yes` (both are the package default) - Redis should never be
reachable from outside this host, and nothing else here needs to reach it
but Superset. `systemctl enable --now redis-server` if the package didn't
already do so.

## 4. Application

```bash
# deploy the repo to /opt/superset (owned root:superset, matching the other
# services' WorkingDirectory convention), then:
cd /opt/superset
python3 -m venv .venv
# gunicorn (with its gthread worker class) is a declared dependency of
# apache-superset itself - nothing extra to install for that part.
#
# Flask-Caching<2.5.0 is pinned deliberately: 2.5.0 (released 2026-08-24,
# after Superset 6.1.0 shipped) broke Superset's cache backend init -
# TypeError: ...got an unexpected keyword argument 'timeout' (or
# 'ignore_delete_many_errors') when running `superset db upgrade`/`init`.
# Open upstream as apache/superset#43860 - drop this pin once that's fixed
# and a newer Superset patch release picks it up.
#
# rich and cachetools: both genuinely required - Superset's CLI (rich) and
# db_engine_specs/aws_iam.py, imported eagerly while cataloging every
# available database engine (cachetools) - but neither is pulled in by a
# bare `pip install apache-superset`. Confirmed live: `rich`'s absence broke
# every `superset` CLI invocation; `cachetools`'s absence 500'd every page
# (common_bootstrap_payload -> get_available_engine_specs runs on every
# request, not just ones using AWS IAM auth). If a *third* distinct
# ModuleNotFoundError turns up, stop adding packages one at a time here and
# install from Superset's pinned requirements/base.txt instead - evidently
# more complete than what setup.py alone declares.
./.venv/bin/pip install "apache-superset==6.1.*" "Flask-Caching<2.5.0" \
  rich cachetools sqlalchemy-bigquery psycopg2-binary redis

sudo cp .env.example /etc/superset/superset.env
sudo chown root:superset /etc/superset/superset.env
sudo chmod 640 /etc/superset/superset.env
# now edit /etc/superset/superset.env and replace every CHANGE_ME value
```

Place the BigQuery service-account key at the path
`GOOGLE_APPLICATION_CREDENTIALS` points to (`/etc/superset/bq-service-account.json`
by default), owned `root:superset`, mode `640`. It must be a **read-only**
service account scoped only to the reporting datasets/views this dashboard
needs - create it as its own dedicated account, not a reuse of a
broader-scoped one. See [bigquery-service-account.md](bigquery-service-account.md)
for the exact setup, including scoping it to `reporting` only via an
authorized view rather than granting it direct access to raw event data.

```bash
source deployment/activate-env.sh   # re-run this after opening any new shell - it doesn't persist

./.venv/bin/superset db upgrade
./.venv/bin/superset fab create-admin   # first admin account, interactive
./.venv/bin/superset init
```

## 5. systemd + nginx

```bash
sudo cp deployment/systemd/superset.service /etc/systemd/system/superset.service
sudo systemctl daemon-reload
sudo systemctl enable --now superset
journalctl -u superset -f   # confirm it comes up clean before touching nginx

sudo cp deployment/nginx/dashboard.diggies.dev.conf \
  /etc/nginx/sites-available/dashboard.diggies.dev
sudo ln -s /etc/nginx/sites-available/dashboard.diggies.dev /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d dashboard.diggies.dev
```

DNS: `dashboard.diggies.dev` needs an A/AAAA record pointing at this server,
same as `scylla.diggies.dev` and `api.diggies.dev` already do.

## 6. RBAC / RLS bootstrap

`superset init` (step 4) needs to have run *after* `FAB_ADD_SECURITY_API =
True` is in effect (already set in `../superset_config.py`) for the roles/RLS
API endpoints below to exist. Then, from a machine that can reach the
dashboard (doesn't need to be the server itself):

```bash
pip install -r ../scripts/requirements.txt
export SUPERSET_ADMIN_USERNAME=admin
export SUPERSET_ADMIN_PASSWORD='...'   # the account from `superset fab create-admin`
python ../scripts/setup_rbac.py --base-url https://dashboard.diggies.dev \
  --properties ../scripts/properties.yaml
```

This creates the `GreenOptionViewer` role and one `PROP_<code>` role per
property in `properties.yaml` (seeded from diggies-api's property list -
confirm it's the right set for Green Option before relying on it). Rerun
the same command with `--datasets voucher_lifecycle,voucher_cleaning_forecast`
(the two `diggies-event-streaming.reporting.*` views - Superset names a
dataset after the view by default when you add it) once the BigQuery
connection/datasets exist in Superset, to also create the per-property RLS
filters and the fail-closed base filter. See the script's own docstring for
the full detail - it's safe to rerun any time (e.g. after adding a
property).

Both views already carry `property_code`, so no schema changes are needed
for RLS to attach cleanly. `voucher_lifecycle`'s `property_timezone` column
is currently hardcoded to only recognize `CGNEL` (everything else falls
back to UTC) - that affects every `*_date`/`*_local` column it derives, and
`voucher_cleaning_forecast` inherits all of them, including its 14-day
forecast window. Worth fixing at the source (a real property→timezone
lookup) before trusting the housekeeping forecast for non-CGNEL properties.

## 7. Not covered by this scaffold

Creating the BigQuery connection/datasets inside Superset, assigning roles
to individual users, and building the dashboard itself - that's the next
piece of work, done through the Superset UI once this is up and
`setup_rbac.py` has run.

## Verification checklist

- [ ] `https://dashboard.diggies.dev` reachable over HTTPS; `curl 127.0.0.1:8088` works locally but the port is not reachable from outside the host
- [ ] `systemctl status superset redis-server postgresql` all healthy
- [ ] Superset's Postgres connection uses the dedicated `superset` role/`superset_metadata` database, not a shared/admin credential
- [ ] BigQuery connection in Superset uses the dedicated read-only service account
- [ ] `/etc/superset/superset.env` and the BigQuery key are root-owned, mode 640/off, and **not** present anywhere under `/opt/superset`'s git checkout
- [ ] A user with no `PROP_*` role and `GreenOptionViewer` sees zero rows, not all rows (test this explicitly - see the brief's fail-closed requirement)
