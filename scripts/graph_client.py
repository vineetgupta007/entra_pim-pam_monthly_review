"""Microsoft Graph client: authentication + paged GET with throttle handling.

Auth modes (config["auth"]["mode"], overridable with --auth-mode):

  "interactive"  - you sign in through your browser. DELEGATED permissions, so Graph
                   checks the signed-in user's directory role as well as the app's
                   consent. Default, and the right mode for a fresh workstation: there
                   is no secret to move between machines.
  "secret_env"   - unattended. Client secret read from the environment variable named
                   in config["auth"]["secret_env_var"]. APPLICATION permissions.
  "certificate"  - unattended, preferred over a secret. Needs PyJWT + cryptography.
                   The private key lives in a PEM file on disk that Python reads directly.
  "token_passthrough" - unattended, cert-backed like "certificate", but the private key
                   never touches disk or the Python process. A non-exportable certificate
                   stays in the Windows certificate store (Cert:\\CurrentUser\\My); a
                   PowerShell wrapper (scripts/Get-GraphToken.ps1) has Windows sign the
                   token request internally and exports only the resulting short-lived
                   bearer token via an environment variable. Python trusts that token as-is
                   and never sees the key. Test independently with:
                       .\\scripts\\Get-GraphToken.ps1 -Thumbprint <thumb> -ClientId <id> -TenantId <id>
                       python check_auth.py --auth-mode token_passthrough

The two permission types are not interchangeable. Interactive needs delegated
AuditLog.Read.All / Directory.Read.All / RoleManagement.Read.Directory plus a
'http://localhost' redirect URI registered under "Mobile and desktop applications".
Unattended needs the same three as application permissions. Both need admin consent.

Authentication failures are raised, never swallowed. Per CLAUDE.md, a failed auth is
reported as a failure rather than worked around.
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
LOGIN_ROOT = "https://login.microsoftonline.com"
SCOPE = "https://graph.microsoft.com/.default"

# Delegated scopes requested for interactive sign-in.
DELEGATED_SCOPES = [
    "https://graph.microsoft.com/AuditLog.Read.All",
    "https://graph.microsoft.com/Directory.Read.All",
    "https://graph.microsoft.com/RoleManagement.Read.Directory",
]

REQUIRED_APP_PERMISSIONS = [
    "AuditLog.Read.All",
    "Directory.Read.All",
    "RoleManagement.Read.Directory",
]

# Directory roles that let a signed-in user read audit logs. With delegated auth the
# user needs one of these even when the app's consent is in place - the commonest cause
# of a 403 that otherwise looks like a consent problem.
AUDIT_CAPABLE_ROLES = [
    "Global Reader", "Reports Reader", "Security Reader",
    "Security Administrator", "Security Operator", "Global Administrator",
]

UNATTENDED_MODES = ("secret_env", "certificate", "token_passthrough")


def default_token_cache_path() -> Path:
    """Outside the repo on purpose - the cache holds a refresh token."""
    if sys.platform.startswith("win"):
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / "entra-pim-review" / "msal_cache.json"


class GraphAuthError(RuntimeError):
    pass


class GraphPermissionError(RuntimeError):
    """403 from Graph - authentication worked but authorisation did not."""


class GraphClient:
    def __init__(self, config: dict, verbose: bool = True, auth_mode: str | None = None):
        self.tenant_id = config["tenant_id"]
        self.client_id = config["client_id"]
        self.auth_cfg = dict(config.get("auth", {}) or {})
        if auth_mode:  # --auth-mode overrides config for one-off runs
            self.auth_cfg["mode"] = auth_mode
        self.mode = self.auth_cfg.get("mode", "interactive")
        self.verbose = verbose
        self.signed_in_as = None  # populated by interactive sign-in
        # Overridable so publish_month.py can add Sites.ReadWrite.All for delegated
        # uploads without changing what the read-only export phase requests.
        self.delegated_scopes = list(DELEGATED_SCOPES)
        self._token = None
        self._expires_at = datetime.now(timezone.utc)
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "caqh-entra-pim-review/1.0"

    @property
    def is_delegated(self) -> bool:
        return self.mode == "interactive"

    # ------------------------------------------------------------------ auth

    def _token_cache(self):
        """MSAL token cache, persisted only if auth.cache_tokens is true."""
        import msal

        cache = msal.SerializableTokenCache()
        if not self.auth_cfg.get("cache_tokens"):
            return cache, None

        raw = self.auth_cfg.get("token_cache_path")
        path = Path(os.path.expandvars(os.path.expanduser(raw))) if raw \
            else default_token_cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            try:
                cache.deserialize(path.read_text(encoding="utf-8"))
            except Exception:
                if self.verbose:
                    print(f"  token cache unreadable, ignoring: {path}")
        return cache, path

    @staticmethod
    def _persist_cache(cache, path: Path | None) -> None:
        if path is None or not cache.has_state_changed:
            return
        path.write_text(cache.serialize(), encoding="utf-8")
        if not sys.platform.startswith("win"):
            try:
                os.chmod(path, 0o600)  # the cache holds a refresh token
            except OSError:
                pass

    def _token_interactive(self) -> dict:
        try:
            import msal
        except ImportError as exc:
            raise GraphAuthError(
                "Interactive sign-in needs MSAL: pip install msal "
                "(or pip install -r requirements.txt)"
            ) from exc

        cache, cache_path = self._token_cache()
        app = msal.PublicClientApplication(
            self.client_id,
            authority=f"{LOGIN_ROOT}/{self.tenant_id}",
            token_cache=cache,
        )

        result = None
        accounts = app.get_accounts()
        if accounts:  # silent renewal from the cached refresh token
            result = app.acquire_token_silent(self.delegated_scopes, account=accounts[0])
            if result and self.verbose:
                print(f"  reused cached sign-in for {accounts[0].get('username')}")

        if not result:
            if self.verbose:
                print("  opening your browser to sign in...")
            try:
                result = app.acquire_token_interactive(
                    self.delegated_scopes,
                    prompt="select_account",
                    success_template="<html><body><h2>Signed in. You can close this tab "
                                     "and return to the terminal.</h2></body></html>",
                )
            except Exception as exc:
                raise GraphAuthError(
                    f"Interactive sign-in could not start: {exc}\n"
                    f"This mode needs a desktop session with a browser, and a redirect URI "
                    f"of 'http://localhost' registered on app {self.client_id} under "
                    f"\"Mobile and desktop applications\". On a headless machine use "
                    f"--auth-mode secret_env instead."
                ) from exc

        self._persist_cache(cache, cache_path)

        if "access_token" not in result:
            err = result.get("error", "unknown")
            desc = result.get("error_description", "")
            hint = ""
            if err in ("invalid_client", "unauthorized_client"):
                hint = ("\nThe app registration is probably not configured as a public "
                        "client. Add a 'http://localhost' redirect URI under \"Mobile and "
                        "desktop applications\".")
            elif "consent" in desc.lower() or err == "invalid_grant":
                hint = ("\nDelegated permissions may not be consented. Grant admin consent "
                        f"for: {', '.join(x.rsplit('/', 1)[-1] for x in self.delegated_scopes)}.")
            raise GraphAuthError(f"Interactive sign-in failed ({err}): {desc}{hint}")

        claims = result.get("id_token_claims") or {}
        self.signed_in_as = claims.get("preferred_username") or claims.get("upn")
        if self.verbose and self.signed_in_as:
            print(f"  signed in as {self.signed_in_as}")
        return result

    def _client_assertion(self) -> str:
        """Build a signed JWT assertion for certificate auth."""
        try:
            import jwt  # PyJWT
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.x509 import load_pem_x509_certificate
        except ImportError as exc:
            raise GraphAuthError(
                "Certificate auth needs PyJWT and cryptography: pip install pyjwt cryptography"
            ) from exc

        pem_path = self.auth_cfg.get("certificate_pem_path")
        if not pem_path or not os.path.exists(pem_path):
            raise GraphAuthError(f"certificate_pem_path not found: {pem_path!r}")

        pem = open(pem_path, "rb").read()
        thumb = self.auth_cfg.get("certificate_thumbprint")
        if not thumb:
            cert = load_pem_x509_certificate(pem)
            thumb = cert.fingerprint(hashes.SHA1()).hex()

        import base64
        x5t = base64.urlsafe_b64encode(bytes.fromhex(thumb.replace(":", ""))).decode().rstrip("=")

        key = serialization.load_pem_private_key(pem, password=None)
        now = int(time.time())
        audience = f"{LOGIN_ROOT}/{self.tenant_id}/oauth2/v2.0/token"
        return jwt.encode(
            {
                "aud": audience,
                "iss": self.client_id,
                "sub": self.client_id,
                "jti": str(uuid.uuid4()),
                "nbf": now,
                "exp": now + 600,
            },
            key,
            algorithm="RS256",
            headers={"x5t": x5t},
        )

    def _token_passthrough(self) -> str:
        """Read a bearer token an external process (PowerShell + Windows cert store)
        already acquired. Python never sees the private key - only this short-lived
        token, which it treats as opaque and un-renewable: when it expires, the wrapper
        script must be re-run to mint a new one.
        """
        var = self.auth_cfg.get("token_env_var", "GRAPH_ACCESS_TOKEN")
        tok = os.environ.get(var)
        if not tok:
            raise GraphAuthError(
                f"Environment variable {var} is not set.\n"
                f"Run the wrapper first, in the SAME shell you'll launch Python from "
                f"(the token must be exported into THIS process's environment). It reads "
                f"client_id/tenant_id/thumbprint from config.json automatically:\n"
                f"  PowerShell:  .\\scripts\\Get-GraphToken.ps1\n"
                f"  git-bash:    export {var}=$(powershell.exe -NoProfile -File "
                f"./scripts/Get-GraphToken.ps1 -Raw)"
            )

        import base64
        try:
            payload_b64 = tok.split(".")[1]
            payload_b64 += "=" * (-len(payload_b64) % 4)  # restore stripped padding
            claims = json.loads(base64.urlsafe_b64decode(payload_b64))
            exp = claims.get("exp")
        except Exception as exc:
            raise GraphAuthError(
                f"{var} does not look like a JWT access token: {exc}"
            ) from exc

        if exp and datetime.now(timezone.utc).timestamp() >= exp:
            raise GraphAuthError(
                f"Token in {var} has expired. Re-run Get-GraphToken.ps1 to mint a fresh one - "
                f"tokens from this flow are short-lived (typically ~1 hour) and are never "
                f"auto-renewed by Python, since Python has no way to re-sign a request."
            )
        return tok

    def token(self) -> str:
        if self._token and datetime.now(timezone.utc) < self._expires_at:
            return self._token

        mode = self.mode

        if mode == "interactive":
            result = self._token_interactive()
            self._token = result["access_token"]
            self._expires_at = datetime.now(timezone.utc) + timedelta(
                seconds=int(result.get("expires_in", 3600)) - 120
            )
            return self._token

        if mode == "token_passthrough":
            self._token = self._token_passthrough()
            payload_b64 = self._token.split(".")[1]
            payload_b64 += "=" * (-len(payload_b64) % 4)
            import base64
            exp = json.loads(base64.urlsafe_b64decode(payload_b64)).get("exp")
            # 60s safety margin, matching the other modes' pattern of renewing early
            self._expires_at = (datetime.fromtimestamp(exp, tz=timezone.utc) -
                                timedelta(seconds=60)) if exp else \
                                datetime.now(timezone.utc) + timedelta(minutes=5)
            if self.verbose:
                print(f"  auth ok (mode={mode}, token supplied by Get-GraphToken.ps1)")
            return self._token

        data = {"client_id": self.client_id, "scope": SCOPE, "grant_type": "client_credentials"}

        if mode == "secret_env":
            var = self.auth_cfg.get("secret_env_var", "ENTRA_CLIENT_SECRET")
            secret = os.environ.get(var)
            if not secret:
                raise GraphAuthError(
                    f"Environment variable {var} is not set.\n"
                    f"  PowerShell:  $env:{var} = '<client-secret>'\n"
                    f"  bash:        export {var}='<client-secret>'"
                )
            data["client_secret"] = secret
        elif mode == "certificate":
            data["client_assertion_type"] = (
                "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
            )
            data["client_assertion"] = self._client_assertion()
        else:
            raise GraphAuthError(
                f"Unknown auth mode {mode!r}; use 'interactive', 'secret_env', "
                f"'certificate', or 'token_passthrough'.")

        url = f"{LOGIN_ROOT}/{self.tenant_id}/oauth2/v2.0/token"
        try:
            resp = self.session.post(url, data=data, timeout=30)
        except requests.RequestException as exc:
            raise GraphAuthError(
                f"Could not reach {LOGIN_ROOT} - network blocked or offline: {exc}"
            ) from exc

        if resp.status_code != 200:
            raise GraphAuthError(
                f"Token request failed (HTTP {resp.status_code}): {resp.text[:600]}\n"
                f"Check tenant_id, client_id, the secret/cert, and that these application "
                f"permissions are granted with admin consent: {', '.join(REQUIRED_APP_PERMISSIONS)}"
            )

        payload = resp.json()
        self._token = payload["access_token"]
        self._expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=int(payload.get("expires_in", 3600)) - 120
        )
        if self.verbose:
            print(f"  auth ok (mode={mode})")
        return self._token

    # ------------------------------------------------------------------ requests

    def _get(self, url: str, params: dict | None = None) -> dict:
        for attempt in range(6):
            resp = self.session.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {self.token()}"},
                timeout=120,
            )
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code in (429, 503, 504):
                wait = int(resp.headers.get("Retry-After", min(2 ** attempt, 60)))
                if self.verbose:
                    print(f"  throttled ({resp.status_code}); sleeping {wait}s")
                time.sleep(wait)
                continue
            if resp.status_code == 401 and attempt == 0:
                self._token = None  # force refresh once
                continue
            if resp.status_code == 403:
                raise GraphPermissionError(self._explain_403(resp))
            raise RuntimeError(f"Graph GET failed (HTTP {resp.status_code}): {resp.text[:600]}")
        raise RuntimeError(f"Graph GET gave up after retries: {url}")

    def _explain_403(self, resp) -> str:
        """A 403 means different things in each auth mode. Name the likely cause rather
        than leaving the reader to guess."""
        try:
            detail = (resp.json().get("error") or {}).get("message", "")
        except Exception:
            detail = resp.text[:300]

        if self.is_delegated:
            who = self.signed_in_as or "the signed-in user"
            return (
                f"Graph returned 403 Forbidden.\n"
                f"  Graph message: {detail}\n\n"
                f"  In interactive mode Graph checks TWO things, and this is the second one:\n"
                f"    1. the app's delegated permissions are consented - probably fine, since "
                f"sign-in succeeded\n"
                f"    2. {who} holds a directory role that can read audit logs - most likely "
                f"the problem\n\n"
                f"  Assign {who} one of: {', '.join(AUDIT_CAPABLE_ROLES)}.\n"
                f"  Consent alone is not enough for delegated access to audit logs."
            )
        return (
            f"Graph returned 403 Forbidden.\n"
            f"  Graph message: {detail}\n\n"
            f"  In {self.mode} mode this is the app's own authorisation. Confirm these are "
            f"granted as APPLICATION permissions with admin consent on app {self.client_id}: "
            f"{', '.join(REQUIRED_APP_PERMISSIONS)}.\n"
            f"  Delegated grants of the same names do not apply to unattended runs."
        )

    def whoami(self) -> dict:
        """Identify what we authenticated as. Used by check_auth.py."""
        if self.is_delegated:
            me = self._get(f"{GRAPH_ROOT}/me?$select=displayName,userPrincipalName,id")
            return {"kind": "user", "displayName": me.get("displayName"),
                    "upn": me.get("userPrincipalName"), "id": me.get("id")}
        return {"kind": "application", "client_id": self.client_id}

    def my_directory_roles(self) -> list[str]:
        """Directory roles held by the signed-in user (delegated mode only)."""
        if not self.is_delegated:
            return []
        try:
            payload = self._get(f"{GRAPH_ROOT}/me/transitiveMemberOf/microsoft.graph.directoryRole"
                                f"?$select=displayName")
            return [r.get("displayName", "") for r in payload.get("value", [])]
        except Exception:
            return []

    def tenant_name(self) -> str:
        try:
            payload = self._get(f"{GRAPH_ROOT}/organization?$select=displayName,id")
            orgs = payload.get("value") or []
            return orgs[0].get("displayName", "(unknown)") if orgs else "(unknown)"
        except Exception:
            return "(could not read - Organization.Read.All not granted)"

    def get_paged(self, path: str, params: dict | None = None, page_note: str = ""):
        """Yield every item across @odata.nextLink pages."""
        url = path if path.startswith("http") else f"{GRAPH_ROOT}{path}"
        page, total = 0, 0
        while url:
            payload = self._get(url, params)
            items = payload.get("value", [])
            total += len(items)
            page += 1
            if self.verbose:
                print(f"  page {page}: {len(items)} rows (total {total}) {page_note}")
            yield from items
            url = payload.get("@odata.nextLink")
            params = None  # nextLink already carries the query


def directory_audit_params(start, end, extra_filter: str | None = None) -> tuple[str, dict]:
    """Build the $filter for auditLogs/directoryAudits over a half-open period."""
    from common import graph_time

    parts = [
        f"activityDateTime ge {graph_time(start)}",
        f"activityDateTime lt {graph_time(end)}",
    ]
    if extra_filter:
        parts.append(extra_filter)
    flt = " and ".join(parts)
    return flt, {"$filter": flt, "$top": "1000"}
