#!/usr/bin/env python3
"""Idempotently bootstrap the Green Option RBAC/RLS scaffolding in Superset.

Creates, via Superset's REST API:

  - one PROP_<code> role per hotel property, from properties.yaml. These
    are pure marker roles with no permissions of their own - a hotel or
    cluster manager's actual data scope comes entirely from which PROP_*
    roles they hold, never from a per-combination role.
  - the GreenOptionViewer role (also a pure marker - combined with
    DASHBOARD_RBAC, it gates whether a user can open the dashboard at all;
    combine it with Gamma for the baseline chart/dashboard-viewing
    permissions Gamma already grants).
  - one Regular RLS filter per property (`property_code = '<code>'`,
    group_key=property_scope, same group key on every one of them so
    Superset ORs them together for a multi-property cluster manager)
    attached to every dataset named in --datasets, assigned to that
    property's PROP_* role.
  - one Base RLS filter (`1 = 0`) on the same datasets, exempting every
    PROP_* role plus Admin. This is the fail-closed rule from the brief:
    anyone with GreenOptionViewer but no PROP_* role gets zero rows, not
    every row.

Safe to rerun: every create step checks by name first and updates in place
rather than duplicating, so this can be rerun after adding a property or a
new dataset without side effects on what's already there.

Requires FAB_ADD_SECURITY_API = True in superset_config.py, and `superset
init` run once after that's set - see ../superset_config.py and
../deployment/README.md.

This talks to Superset's documented REST API shape, but wasn't run against
a live instance to confirm it end-to-end (none exists yet at the time this
was written) - treat the first run as a dry run: read its output, and spot
check the created roles/filters in the Superset UI before trusting it for
every property.

Usage:
    export SUPERSET_ADMIN_USERNAME=admin
    export SUPERSET_ADMIN_PASSWORD='...'
    python setup_rbac.py --base-url https://dashboard.diggies.dev \
        --properties properties.yaml

    # Once the BigQuery connection/datasets exist in Superset, rerun with
    # --datasets to also create the RLS filters (comma separated Superset
    # dataset names, i.e. what they're called in Data > Datasets):
    python setup_rbac.py --base-url https://dashboard.diggies.dev \
        --properties properties.yaml \
        --datasets voucher_lifecycle,voucher_cleaning_forecast
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

import requests
import yaml

GROUP_KEY = "property_scope"
BASE_FILTER_NAME = "green_option_base_deny"
VIEWER_ROLE_NAME = "GreenOptionViewer"


class SupersetClient:
    def __init__(self, base_url: str, username: str, password: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self._login(username, password)

    def _login(self, username: str, password: str) -> None:
        resp = self.session.post(
            f"{self.base_url}/api/v1/security/login",
            json={"username": username, "password": password, "provider": "db", "refresh": True},
            timeout=30,
        )
        resp.raise_for_status()
        token = resp.json()["access_token"]
        self.session.headers["Authorization"] = f"Bearer {token}"

        # The write endpoints below also require a CSRF token bound to the
        # session cookie this call sets - requests.Session() carries that
        # cookie automatically for every request after this one.
        csrf_resp = self.session.get(f"{self.base_url}/api/v1/security/csrf_token/", timeout=30)
        csrf_resp.raise_for_status()
        self.session.headers["X-CSRFToken"] = csrf_resp.json()["result"]
        self.session.headers["Referer"] = self.base_url

    def _get_all(self, path: str) -> list[dict[str, Any]]:
        """Page through a standard Superset/Flask-AppBuilder list endpoint."""
        results: list[dict[str, Any]] = []
        page = 0
        while True:
            resp = self.session.get(
                f"{self.base_url}{path}",
                params={"q": f"(page:{page},page_size:100)"},
                timeout=30,
            )
            resp.raise_for_status()
            body = resp.json()
            results.extend(body["result"])
            if len(results) >= body["count"]:
                return results
            page += 1

    def get_role_by_name(self, name: str) -> dict[str, Any] | None:
        return next((r for r in self._get_all("/api/v1/security/roles/") if r["name"] == name), None)

    def ensure_role(self, name: str) -> int:
        existing = self.get_role_by_name(name)
        if existing:
            print(f"  role {name!r} already exists (id={existing['id']})")
            return existing["id"]
        resp = self.session.post(f"{self.base_url}/api/v1/security/roles/", json={"name": name}, timeout=30)
        resp.raise_for_status()
        role_id = resp.json()["id"]
        print(f"  created role {name!r} (id={role_id})")
        return role_id

    def get_dataset_id_by_name(self, table_name: str) -> int | None:
        resp = self.session.get(
            f"{self.base_url}/api/v1/dataset/",
            params={"q": f"(filters:!((col:table_name,opr:eq,value:'{table_name}')))"},
            timeout=30,
        )
        resp.raise_for_status()
        results = resp.json()["result"]
        return results[0]["id"] if results else None

    def get_rls_by_name(self, name: str) -> dict[str, Any] | None:
        return next((r for r in self._get_all("/api/v1/rowlevelsecurity/") if r["name"] == name), None)

    def ensure_rls_filter(
        self,
        *,
        name: str,
        clause: str,
        filter_type: str,
        group_key: str | None,
        role_ids: list[int],
        dataset_ids: list[int],
    ) -> None:
        payload = {
            "name": name,
            "clause": clause,
            "filter_type": filter_type,
            "group_key": group_key,
            "roles": role_ids,
            "tables": dataset_ids,
        }
        existing = self.get_rls_by_name(name)
        if existing:
            resp = self.session.put(
                f"{self.base_url}/api/v1/rowlevelsecurity/{existing['id']}", json=payload, timeout=30
            )
            resp.raise_for_status()
            print(f"  updated RLS filter {name!r}")
        else:
            resp = self.session.post(f"{self.base_url}/api/v1/rowlevelsecurity/", json=payload, timeout=30)
            resp.raise_for_status()
            print(f"  created RLS filter {name!r}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--base-url", required=True, help="e.g. https://dashboard.diggies.dev")
    parser.add_argument("--properties", required=True, help="path to properties.yaml")
    parser.add_argument(
        "--datasets",
        help="Comma-separated Superset dataset names to attach RLS to. Omit to only "
        "create/update roles - rerun with this filled in once the BigQuery datasets "
        "exist in Superset (Data > Datasets).",
    )
    args = parser.parse_args()

    username = os.environ.get("SUPERSET_ADMIN_USERNAME")
    password = os.environ.get("SUPERSET_ADMIN_PASSWORD")
    if not username or not password:
        print("Set SUPERSET_ADMIN_USERNAME and SUPERSET_ADMIN_PASSWORD in the environment "
              "(not as CLI args - keeps credentials out of shell history/process list).",
              file=sys.stderr)
        return 1

    with open(args.properties, encoding="utf-8") as f:
        properties: list[str] = yaml.safe_load(f)["properties"]

    client = SupersetClient(args.base_url, username, password)

    print("Ensuring GreenOptionViewer role...")
    client.ensure_role(VIEWER_ROLE_NAME)

    print(f"Ensuring {len(properties)} PROP_* roles...")
    prop_role_ids: dict[str, int] = {code: client.ensure_role(f"PROP_{code}") for code in properties}

    if not args.datasets:
        print("\nNo --datasets given - stopping after role creation.")
        print("Rerun with --datasets once the BigQuery connection/datasets exist in Superset.")
        return 0

    dataset_names = [d.strip() for d in args.datasets.split(",") if d.strip()]
    dataset_ids: list[int] = []
    for name in dataset_names:
        dataset_id = client.get_dataset_id_by_name(name)
        if dataset_id is None:
            print(f"  WARNING: no dataset named {name!r} found in Superset - skipping it", file=sys.stderr)
            continue
        dataset_ids.append(dataset_id)

    if not dataset_ids:
        print("None of the given --datasets were found in Superset. Nothing to attach RLS to.",
              file=sys.stderr)
        return 1

    admin_role = client.get_role_by_name("Admin")
    if admin_role is None:
        print("Could not find the built-in Admin role - aborting before creating the base filter.",
              file=sys.stderr)
        return 1

    print(f"\nEnsuring one Regular RLS filter per property on {dataset_names}...")
    for code, role_id in prop_role_ids.items():
        client.ensure_rls_filter(
            name=f"green_option_prop_{code.lower()}",
            clause=f"property_code = '{code}'",
            filter_type="Regular",
            group_key=GROUP_KEY,
            role_ids=[role_id],
            dataset_ids=dataset_ids,
        )

    print(f"\nEnsuring the fail-closed Base filter ({BASE_FILTER_NAME!r})...")
    exempt_role_ids = [*prop_role_ids.values(), admin_role["id"]]
    client.ensure_rls_filter(
        name=BASE_FILTER_NAME,
        clause="1 = 0",
        filter_type="Base",
        group_key=None,
        role_ids=exempt_role_ids,
        dataset_ids=dataset_ids,
    )

    print(
        "\nDone. Reminder: a user needs Gamma + GreenOptionViewer to open the dashboard "
        "at all (via DASHBOARD_RBAC), plus at least one PROP_* role to see any rows in it. "
        "Test the zero-PROP_* case explicitly before rolling this out."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
