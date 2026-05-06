# `tests/contract/` — P0 Compatibility Contract Tests

> Status: **skeletons only — most tests are red on purpose**.
> They lock the *done-criteria* for the P0 hotfix; backend implementation
> lands in follow-up PRs (see `deepnote-contracts/migration/backend-p0-compat-fix-plan.md` §9).

## Purpose

These tests pin the **wire shape** that iOS / Desktop already in users' hands
expect. Their job is to fail loudly the moment a backend change drops a
required key, removes a legacy alias path, or alters the response shape of
any of the 5 P0 endpoints.

They are **not** behavioural / integration tests — Firestore is mocked,
Firebase Auth is bypassed via FastAPI `dependency_overrides`. The point is
the response shape, not the data.

## Source-of-truth

| File | Pinned spec section |
|---|---|
| `test_p0_compat_bootstrap.py` | hotfix spec `BootstrapResponse` + `FeatureGates` schemas; plan §6.1 |
| `test_p0_compat_system_status.py` | hotfix spec `SystemStatus` + `AppConfig` schemas; plan §6.2 |
| `test_p0_compat_share_link.py` | hotfix spec `ShareLinkResponse` schema; plan §6.3 |
| `test_p0_compat_users_delete.py` | hotfix spec `/users/me:delete*` + `/users/me/delete*`; plan §6.4 |
| `test_p0_compat_folders.py` | hotfix spec `Folder` + `FolderSessionSnapshot`; plan §6.5 |

The hotfix OpenAPI lives at
`~/Projects/deepnote-contracts/specs/openapi.v1.hotfix.yaml`. When the
checkout is reachable, `loaded_hotfix_spec` (in `conftest.py`) parses it so
tests can compare required-key sets to the spec.

## What each test pins

### `test_p0_compat_bootstrap.py` — P0 #1

- `plan` always present in `POST /users/bootstrap` response.
- `plan == "free"` when `accounts/{id}.plan` is null.
- `featureGates` dict present, with all 6 required Booleans
  (`cloudStt`, `summarization`, `quiz`, `cloudSync`, `export`, `share`).
- The 10 canonical top-level keys (`uid`, `accountId`, `plan`, `hasUsername`,
  `providers`, `needsPhoneVerification`, `needsSnsLogin`, `suspended`,
  `canonicalized`, `claimsRefreshRequired`) never disappear.
- Optional/legacy keys (`displayName`, `username`, `photoUrl`, `provider`,
  `previousAccountId`, `cacheValidUntil`) never disappear (add-only rule).
- The required-key set in this test matches the hotfix OpenAPI
  `BootstrapResponse.required` array (drift-detection test).

### `test_p0_compat_system_status.py` — P0 #2

- `GET /system/status` always returns `mode`.
- Default `mode == "normal"` when no env override is set.
- `SYSTEM_STATUS_MODE=drain` surfaces as `mode == "drain"`
  (**EXPECTED FAIL today** — `drain` is not in the current whitelist).
- `mode` falls back to `"normal"` for unknown values.
- `/system/status` works **without** Authorization.
- `GET /system/config` always returns `status` and `generatedAt`
  (**EXPECTED FAIL today** — current handler returns
  `{platform, maintenance, minSupportedVersion, features}` only).
- `/system/config` legacy keys never disappear.

### `test_p0_compat_share_link.py` — P0 #3

- `GET/POST /sessions/{id}/share_link` always returns canonical `url`
  (non-empty string).
- The 6 URL-equivalent alias keys
  (`shareUrl`, `share_url`, `shareLink`, `share_link`, `link`, `publicUrl`)
  are emitted in parallel and share the same value
  (**EXPECTED FAIL today** — current handler emits only `url`).
- GET and POST return identical key sets.
- Identifier/token aliases (`token`, `shareToken`, `share_token`, `code`,
  `shareCode`, `share_code`) are tolerated; semantics-decision is `TODO` per
  plan §10 #3 (test marked accordingly with an in-file `TODO(P1)`).
- Canonical `url` can never be removed (tripwire).

### `test_p0_compat_users_delete.py` — P0 #4

- Canonical colon paths exist in the FastAPI route table:
  `POST /users/me:delete`, `GET /users/me:delete/preflight`,
  `GET /users/me:delete/status`.
- Slash aliases exist (**EXPECTED FAIL today** — to be added in
  `app/routes/compat_aliases.py`):
  `POST /users/me/delete`, `GET /users/me/delete/preflight`,
  `GET /users/me/delete/status`.
- Legacy `DELETE /users/me` (deprecated 204) still exists and DOES NOT
  collide with the new slash aliases (different methods/paths).
- Canonical and alias responses share the same key set (handler parity).

### `test_p0_compat_folders.py` — P0 #5

- `/folders` legacy alias is registered (tripwire — removal is forbidden
  until 2027-11-06 per hotfix spec).
- `/v1/folders` canonical is registered.
- `/folders/{id}/sessions` and `/v1/folders/{id}/sessions` both registered.
- `GET /folders` returns a JSON ARRAY (iOS Decodable target is
  `[FolderResponse]`, NOT `{folders: [...]}`).
- `GET /v1/folders` returns the `{folders: [...]}` wrapper.
- Folder items always carry `id` and `name`.
- `FolderSessionSnapshot` carries `ownerUid` / `ownerAccountId` /
  `ownerUsername` — required for iOS `isMine` decision (skipped when the
  fixture cannot supply a snapshot row; expanded in P0 fix PR).

## Status today (skeleton phase, measured)

`pytest tests/contract/` on baseline `fix/bot-prod-snapshot-and-summary-429`
(commit `7e1b3ff1`) produces: **31 passed, 7 failed, 3 skipped**.

### Expected failures (= P0 done-criteria)

| Test | Locks |
|---|---|
| `test_p0_compat_share_link.py::test_share_link_post_emits_url_alias_keys` | P0 #3 — canonical `url` is emitted today, but aliases (`shareUrl`, `share_url`, `shareLink`, `share_link`, `link`, `publicUrl`) are missing. |
| `test_p0_compat_system_status.py::test_system_status_drain_mode_surfaces` | P0 #2 — `SYSTEM_STATUS_MODE=drain` collapses to `"normal"` today; whitelist must add `"drain"`. |
| `test_p0_compat_system_status.py::test_system_config_has_status_and_generated_at` | P0 #2 — `/system/config` does not yet emit `status` / `generatedAt`. |
| `test_p0_compat_users_delete.py::test_slash_alias_paths_registered` | P0 #4 — slash-form alias routes are not registered. |
| `test_p0_compat_users_delete.py::test_slash_alias_delete_post_returns_2xx` | P0 #4 — `POST /users/me/delete` returns 404. |
| `test_p0_compat_users_delete.py::test_slash_alias_preflight_returns_2xx` | P0 #4 — `GET /users/me/delete/preflight` returns 404. |
| `test_p0_compat_users_delete.py::test_slash_alias_status_returns_2xx` | P0 #4 — `GET /users/me/delete/status` returns 404. |

### Expected skips today

| Test | Reason |
|---|---|
| `test_p0_compat_users_delete.py::test_canonical_and_alias_status_share_shape` | Pre-req: both endpoints must return 200; alias is 404 today. |
| `test_p0_compat_users_delete.py::test_canonical_and_alias_preflight_share_shape` | Same pre-req as above. |
| `test_p0_compat_folders.py::test_folder_session_snapshot_has_ownership_keys` | Skeleton fixture does not seed a `FolderSessionSnapshot` row; the P0-fix PR should extend the fixture. |

If the actual run differs from these tables, update this section in the
same PR — it is the contract for what "P0 done" means.

## Done criteria for the P0 implementation PR

The P0 implementation is "done" when **every test in this folder passes**
without `xfail` or `skip` (except the deliberate skips noted: spec-drift
when `deepnote-contracts/` is not present, and `FolderSessionSnapshot`
when no snapshot row is fixtured).

Specifically:

1. `alias_users_bootstrap` already returns `plan` and a 6-Bool
   `featureGates` — no change needed to satisfy the bootstrap suite.
2. `alias_system_status` adds `"drain"` to the valid-mode whitelist.
3. `alias_system_config` adds `status` and `generatedAt` to its dict
   (must keep all existing keys).
4. `share.create_share_link` returns a `ShareLinkResponse` enriched with
   `shareUrl`, `share_url`, `shareLink`, `share_link`, `link`, `publicUrl`
   (all = canonical `url`). `token`/`code` semantics decision is *not*
   gated by these tests — see in-file `TODO(P1)`.
5. `compat_aliases.py` gains three slash-form alias routes that forward to
   the canonical `/users/me:delete*` handlers.
6. `folders.py` keeps both routers registered; ownership keys stay in
   `FolderSessionSnapshot`.

## How to run

These tests live alongside the existing pytest suite in `tests/`. The
parent `tests/conftest.py` mocks Google Cloud SDK modules before
`app.main` is imported; `tests/contract/conftest.py` then installs the
auth-bypass and Firestore-mock fixtures.

From the repo root, with the project's Python 3.13 virtualenv active:

```bash
# All P0 contract tests
pytest tests/contract/ -v

# Single file
pytest tests/contract/test_p0_compat_bootstrap.py -v

# Only one test
pytest tests/contract/test_p0_compat_folders.py::test_legacy_folders_returns_json_array -v
```

To run the entire test suite (existing + contract):

```bash
pytest tests/ -v
```

Pytest discovery: `tests/contract/` is a regular pytest folder with a
`conftest.py`; no extra config is required. There is no `pytest.ini` /
`pyproject.toml` test config in this repo; pytest defaults pick everything
up.

## When `deepnote-contracts/` is unavailable

`test_p0_compat_bootstrap.py::test_bootstrap_spec_required_keys_match`
SKIPS gracefully if neither `~/Projects/deepnote-contracts/` nor a sibling
checkout is reachable, or if `pyyaml` is not installed. Treat the skip as
a soft-warning: when feasible, install `pyyaml` and clone
`deepnote-contracts` next to `classnote-api` so the spec-drift check runs
in CI.

## CI integration (planned, not yet wired)

Per `backend-p0-compat-fix-plan.md` §8.3, after the P0 fix lands these
tests are promoted to "merge-blocking" via CI. Until then they run on
demand. **Do not skip or xfail** them as a workaround for a real
shape regression — that defeats the entire purpose of the suite.

## Out-of-scope for this folder

- Behavioural / integration / end-to-end testing.
- Auth, account-resolution, ownership semantics — covered by
  `tests/test_auth_spec.py`.
- Billing, plan limits, usage logging — covered by other `tests/test_*` files.
- Anything beyond the 5 P0 endpoints. P1+ alias regression tests will live
  under `tests/contract/p1/` (folder added later).
