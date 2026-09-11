# Source this before running any `superset` CLI command by hand - it does
# not persist across shell sessions, so it needs re-sourcing every time you
# open a new root shell:
#
#   sudo -s
#   cd /opt/superset
#   source deployment/activate-env.sh
#   ./.venv/bin/superset ...
#
# Once systemd takes over (see systemd/superset.service), this stops
# mattering - EnvironmentFile= loads the same superset.env automatically
# for the actual running service. This is only for manual/interactive use.
export SUPERSET_CONFIG_PATH=/opt/superset/superset_config.py
set -a
source /etc/superset/superset.env
set +a
