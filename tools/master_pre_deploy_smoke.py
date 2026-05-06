"""Master User pre-deploy smoke for production deepnote-api.

Drives §7.5 of ``deepnote-contracts/quality/backend-deploy-checksheet.md``
and the §5.2 dynamic check list of
``deepnote-contracts/migration/backend-p0-compat-fix-plan.md``.

Workflow
========
1. Mint a Firebase custom token for the master uid via Firebase Admin
   SDK (uses ``classnote-api-key.json`` SA file or ADC).
2. Exchange that custom token for an ID token via Identity Toolkit
   REST API using the iOS Firebase Web API key.
3. Hit the production endpoints relevant to this release unit.
4. Print a Readiness summary (PASS / PARTIAL / FAIL) — never the token.

Run::

    python tools/master_pre_deploy_smoke.py
    python tools/master_pre_deploy_smoke.py --base https://dev---deepnote-api-...

Hard rules
==========
- The Master ID Token MUST NEVER be printed, logged, or persisted.
  ``_redact()`` shows only the last 6 chars when echoing for debug.
- This script is read-only: it never POSTs to ``/users/me:delete``,
  never mutates Firestore. ``preflight`` and ``status`` only.
"""
from __future__ import annotations

import argparse
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

# Master constants — see CLAUDE.md (global) "Master User 固定情報"
MASTER_UID = "cfdXMsjPXfea8OsidGQtXrSZOfP2"
MASTER_ACCOUNT_ID = "Jwb9VwA4kkfOLQh7PVZ9"
MASTER_EMAIL = "horio.shuhei98@gmail.com"

FIREBASE_API_KEY = "AIzaSyB9ZJAYy39oCegV6ovK_dEXhL2w5Cy06nQ"  # iOS Firebase Web API key
DEFAULT_BASE = "https://deepnote-api-mur5rvqgga-an.a.run.app"


def _redact(token: str) -> str:
    if not token or len(token) < 12:
        return "***"
    return f"<id_token …{token[-6:]}>"


def _mint_id_token() -> str:
    import firebase_admin
    from firebase_admin import auth, credentials
    import requests

    if not firebase_admin._apps:
        try:
            cred = credentials.Certificate("classnote-api-key.json")
            firebase_admin.initialize_app(cred)
        except Exception:
            firebase_admin.initialize_app()

    custom_token = auth.create_custom_token(MASTER_UID)
    if isinstance(custom_token, bytes):
        custom_token = custom_token.decode("utf-8")

    url = (
        "https://identitytoolkit.googleapis.com/v1/accounts:"
        f"signInWithCustomToken?key={FIREBASE_API_KEY}"
    )
    resp = requests.post(url, json={"token": custom_token, "returnSecureToken": True}, timeout=15)
    resp.raise_for_status()
    return resp.json()["idToken"]


def _hit(method: str, base: str, path: str, *, token: str,
         body: Optional[Dict[str, Any]] = None,
         expect_status: Tuple[int, ...] = (200,),
         must_contain: Optional[List[str]] = None,
         json_check: Optional[Callable[[Any], Optional[str]]] = None) -> Tuple[bool, str]:
    """Return (ok, summary). ``json_check`` is a callable receiving the
    decoded JSON; it should return None on success or a string on
    failure that becomes the test summary."""
    import requests
    headers = {"Authorization": f"Bearer {token}"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    try:
        r = requests.request(method, base + path, headers=headers,
                             json=body, timeout=30)
    except Exception as e:
        return False, f"{method} {path} REQUEST_ERROR: {e}"
    if r.status_code not in expect_status:
        return False, (f"{method} {path} HTTP {r.status_code} "
                       f"(expected {expect_status}): {r.text[:120]}")
    if must_contain or json_check:
        try:
            data = r.json()
        except Exception:
            return False, f"{method} {path} non-JSON body"
        if must_contain:
            missing = [k for k in must_contain if k not in data]
            if missing:
                return False, f"{method} {path} missing keys: {missing}"
        if json_check:
            err = json_check(data)
            if err:
                return False, f"{method} {path} {err}"
    return True, f"{method} {path} HTTP {r.status_code}"


def main() -> int:
    desc = (__doc__ or "Master pre-deploy smoke").split("\n\n")[0]
    p = argparse.ArgumentParser(description=desc)
    p.add_argument("--base", default=DEFAULT_BASE,
                   help=f"API base URL (default: {DEFAULT_BASE})")
    ns = p.parse_args()

    print(f"# master pre-deploy smoke against {ns.base}")
    print(f"# master uid={MASTER_UID[:8]}…  accountId={MASTER_ACCOUNT_ID[:8]}…")
    print()

    t0 = time.time()
    print("Minting custom token + exchanging for ID token …")
    try:
        id_token = _mint_id_token()
    except Exception as e:
        print(f"FAIL: could not mint master ID token: {e}")
        return 1
    print(f"  got {_redact(id_token)} ({(time.time()-t0)*1000:.0f}ms)")
    print()

    cases = []

    # 0. /version (no auth) — confirm revision
    cases.append((
        "version",
        lambda: _hit("GET", ns.base, "/version", token=id_token,
                     must_contain=["service", "cloudRunRevision"])
    ))

    # P0 #1 — bootstrap
    def _check_bootstrap(d):
        if not isinstance(d.get("plan"), str):
            return "missing/invalid `plan`"
        gates = d.get("featureGates") or {}
        required = ("cloudStt", "summarization", "quiz", "cloudSync", "export", "share")
        for k in required:
            if k not in gates or not isinstance(gates[k], bool):
                return f"featureGates.{k} missing or not bool"
        return None
    cases.append((
        "bootstrap",
        lambda: _hit("POST", ns.base, "/users/bootstrap", token=id_token,
                     body={}, must_contain=["uid", "accountId", "plan", "featureGates"],
                     json_check=_check_bootstrap)
    ))

    # P0 #2 — system_status / system_config
    cases.append((
        "system_status mode",
        lambda: _hit("GET", ns.base, "/system/status?platform=ios", token=id_token,
                     must_contain=["mode"])
    ))
    cases.append((
        "system_config status+generatedAt",
        lambda: _hit("GET", ns.base, "/system/config?platform=ios", token=id_token,
                     must_contain=["status", "generatedAt", "platform", "maintenance"])
    ))

    # /users/me — canonical on this backend (no /v1/ prefix today)
    cases.append((
        "users/me",
        lambda: _hit("GET", ns.base, "/users/me", token=id_token,
                     must_contain=["uid"])
    ))

    # /v1/folders + legacy /folders
    cases.append((
        "v1 folders",
        lambda: _hit("GET", ns.base, "/v1/folders", token=id_token)
    ))
    cases.append((
        "legacy /folders is array",
        lambda: _hit("GET", ns.base, "/folders", token=id_token,
                     json_check=lambda d: None if isinstance(d, list)
                                          else f"expected list, got {type(d).__name__}")
    ))

    # /sessions — canonical on this backend
    cases.append((
        "sessions list",
        lambda: _hit("GET", ns.base, "/sessions?limit=5", token=id_token)
    ))

    # P0 #4 — users/me:delete preflight (BOTH colon and slash)
    cases.append((
        "users/me:delete preflight (canonical)",
        lambda: _hit("GET", ns.base, "/users/me:delete/preflight", token=id_token)
    ))
    cases.append((
        "users/me/delete/preflight (slash alias)",
        lambda: _hit("GET", ns.base, "/users/me/delete/preflight", token=id_token)
    ))
    cases.append((
        "users/me:delete status (canonical)",
        lambda: _hit("GET", ns.base, "/users/me:delete/status", token=id_token)
    ))
    cases.append((
        "users/me/delete/status (slash alias)",
        lambda: _hit("GET", ns.base, "/users/me/delete/status", token=id_token)
    ))

    # Run them
    results: List[Tuple[str, bool, str]] = []
    for name, fn in cases:
        ok, summary = fn()
        results.append((name, ok, summary))
        flag = "✅" if ok else "❌"
        print(f"  {flag} {name:42} — {summary}")

    print()
    n_pass = sum(1 for _, ok, _ in results if ok)
    n_fail = len(results) - n_pass

    # Readiness verdict (per backend-deploy-checksheet §8)
    if n_fail == 0:
        verdict = "PASS"
        rc = 0
    elif n_fail <= 2:
        verdict = "PARTIAL"
        rc = 1
    else:
        verdict = "FAIL"
        rc = 2

    print(f"# Result: {n_pass}/{len(results)} cases pass  →  Readiness = {verdict}")
    if rc != 0:
        print("# (PARTIAL → staging only / FAIL → deploy禁止 per CLAUDE.md H0)")
    return rc


if __name__ == "__main__":
    sys.exit(main())
