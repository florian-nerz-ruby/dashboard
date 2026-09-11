# BigQuery service account for Superset

Run this yourself, in a terminal where `gcloud auth login` can complete an
interactive reauth if needed (it couldn't in the sandbox this was drafted
in). Every command below names `diggies-event-streaming` explicitly - your
gcloud default project is `staff-stays`, which is unrelated to this and
should not be touched by any of this.

Goal: a service account that can query exactly two views
(`reporting.voucher_lifecycle`, `reporting.voucher_cleaning_forecast`) and
nothing else - not the raw `event_streaming` dataset those views read from,
not any other dataset in the project.

## 1. Create the service account

```
gcloud iam service-accounts create superset-bq-reader --project=diggies-event-streaming --display-name="Superset (Green Option dashboard) - read only"
```

This creates `superset-bq-reader@diggies-event-streaming.iam.gserviceaccount.com`
- that full email is what you'll grant access to and reference from
Superset's connection.

## 2. Grant BigQuery Job User - project level (unavoidable)

Running a query at all requires `roles/bigquery.jobUser` on the *billing*
project, and that role can't be scoped narrower than project level - there's
no dataset-level equivalent. It only lets the SA run jobs and consume
project quota, not read anything by itself; actual read access is entirely
controlled by step 3.

```
gcloud projects add-iam-policy-binding diggies-event-streaming --member="serviceAccount:superset-bq-reader@diggies-event-streaming.iam.gserviceaccount.com" --role="roles/bigquery.jobUser"
```

## 3. Grant read access to the `reporting` dataset only (Console UI)

Do this in the BigQuery Console rather than scripted, since it's a
read-modify-write on IAM policy that's easy to get wrong blind:

1. BigQuery Console -> `diggies-event-streaming` -> the **`reporting`**
   dataset -> **Sharing** -> **Permissions** -> **Add principal**.
2. Principal: `superset-bq-reader@diggies-event-streaming.iam.gserviceaccount.com`
3. Role: **BigQuery Data Viewer** (`roles/bigquery.dataViewer`).
4. Save.

Do **not** grant anything on the `event_streaming` dataset itself - step 4
is what lets the SA query the views without ever touching the raw data
directly.

## 4. Authorize `reporting`'s views to read from `event_streaming`

`voucher_lifecycle` is a standard (non-materialized) view that reads
`event_streaming.voucher_events`. Standard views run with the *querying
principal's* permissions on the tables they reference - so without this
step, the SA would get a permission error reading through the view even
though it can see the view itself, because it has no grant on
`event_streaming`. An authorized view fixes that by making the view itself
carry the read authorization, so the SA never needs (and never gets) direct
access to the raw event data:

1. BigQuery Console -> `diggies-event-streaming` -> the **`event_streaming`**
   dataset -> **Sharing** -> **Authorized views / Authorized datasets**.
2. Add the **`reporting`** dataset as an authorized dataset (covers both
   views in it, including `voucher_cleaning_forecast`'s indirect read of
   `voucher_lifecycle` - though that one's same-dataset and wouldn't need
   this anyway).
3. Save.

Confirm this actually worked before moving on - see the test in step 6.

## 5. Create and download the key

```
gcloud iam service-accounts keys create bq-service-account.json --iam-account=superset-bq-reader@diggies-event-streaming.iam.gserviceaccount.com --project=diggies-event-streaming
```

This downloads a JSON key to your current directory. Treat it as live
production credentials from the moment it exists:

- Do **not** put it anywhere inside a git working directory (not even
  briefly) - the `dashboard` repo's `.gitignore` blocks `bq-*.json` as a
  backstop, but don't rely on that instead of just not putting it there.
- `scp` it directly to the server at `/etc/superset/bq-service-account.json`
  (the path `GOOGLE_APPLICATION_CREDENTIALS` already points to in
  `.env.example`), then `chown root:superset` + `chmod 640` it there.
- Delete the local copy once it's on the server:
  `rm bq-service-account.json` (bash) or `Remove-Item bq-service-account.json`
  (PowerShell).

If key creation is blocked by an org policy
(`iam.disableServiceAccountKeyCreation`), that means this GCP org has
disabled long-lived SA keys org-wide - come back and we'll figure out
Workload Identity Federation instead, which is a bigger change than this
runbook covers.

## 6. Verify least-privilege actually holds

From a machine with the key (or `gcloud auth activate-service-account
--key-file=bq-service-account.json`), confirm all three of these:

```
# Should work - the two views this SA is meant to read
bq query --use_legacy_sql=false "SELECT COUNT(*) FROM \`diggies-event-streaming.reporting.voucher_lifecycle\`"
bq query --use_legacy_sql=false "SELECT COUNT(*) FROM \`diggies-event-streaming.reporting.voucher_cleaning_forecast\`"

# Should FAIL with a permission error - confirms the SA has no direct
# access to the raw dataset, only through the authorized view
bq query --use_legacy_sql=false "SELECT COUNT(*) FROM \`diggies-event-streaming.event_streaming.voucher_events\`"
```

If the third query succeeds, something granted broader access than
intended somewhere upstream - worth tracking down before wiring this into
Superset.
