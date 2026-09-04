<#
.SYNOPSIS
    Acquires a Microsoft Graph access token using a certificate stored in the Windows
    certificate store, and exports ONLY the resulting short-lived token - never the
    private key - into the current shell's environment.

.DESCRIPTION
    This exists so the app's private key can be marked non-exportable at creation and
    never sit in a PEM file on disk. Windows (via CNG) performs the signing internally;
    this script never reads, exports, or has access to the raw key material - it only
    asks Windows to sign a token request with a key it already holds, then captures the
    resulting bearer token.

    The token is written to $env:GRAPH_ACCESS_TOKEN (or -EnvVarName) in THIS PowerShell
    process only. It is inherited by any child process launched from this same shell -
    including python run_month.py - but is not visible to other terminals, other users,
    or written to disk anywhere.

    Tokens from this flow are short-lived (~1 hour, set by Entra, not this script) and
    are not auto-renewed. Re-run this script when graph_client.py reports the token has
    expired.

.PARAMETER Thumbprint
    SHA-1 thumbprint of the certificate in Cert:\CurrentUser\My (or Cert:\LocalMachine\My
    with -StoreLocation LocalMachine). If omitted, read from config.json's
    auth.token_passthrough_thumbprint. Find a thumbprint with:
        Get-ChildItem Cert:\CurrentUser\My | Select Subject, Thumbprint

.PARAMETER ClientId
    The app registration's Application (client) ID. If omitted, read from config.json's
    client_id - the same value run_month.py already uses.

.PARAMETER TenantId
    The Entra tenant ID. If omitted, read from config.json's tenant_id.

.PARAMETER ConfigPath
    Where to read the three values above from when not passed explicitly. Defaults to
    config.json next to this script (scripts\config.json), matching where
    run_month.py/check_auth.py already look for it. Resolved at runtime rather than
    shown as a fixed default, since it depends on where this script actually is.

.PARAMETER StoreLocation
    "CurrentUser" (default) or "LocalMachine".

.PARAMETER EnvVarName
    Name of the environment variable to set. Defaults to GRAPH_ACCESS_TOKEN, matching
    config.json's auth.token_env_var default.

.NOTES
    MSAL.PS is auto-installed (CurrentUser scope) on first run if missing. For a
    scheduled/unattended machine, installing it once ahead of time is still worth doing
    so the FIRST real run doesn't depend on PSGallery being reachable at that moment:
        Install-Module MSAL.PS -Scope CurrentUser -Force
    The certificate's private key must be present and accessible (non-exportable is fine
    and preferred - MSAL.PS asks Windows to sign with it, it never needs to read it out).

.PARAMETER Raw
    Print ONLY the bare access token to stdout, with all informational/status text sent
    to stderr instead - and do not attempt to set $env: at all. Use this when the wrapper
    is invoked from a shell other than PowerShell (git-bash, cmd.exe), since Set-Item
    Env: only ever modifies the environment of the powershell.exe process itself. That
    process is a CHILD of git-bash/cmd, and anything it sets in its own environment is
    gone the moment it exits - it can never reach back into the parent shell. -Raw sidesteps
    this: the calling shell captures the clean stdout output itself, in its own syntax.

.EXAMPLE
    # From PowerShell, run from the repo root - client_id/tenant_id/thumbprint all come
    # from scripts\config.json, nothing to type. Sets $env: directly in this session.
    .\scripts\Get-GraphToken.ps1

.EXAMPLE
    # From git-bash - bash captures the token itself via command substitution. Values
    # still self-discovered from config.json; only -Raw is needed.
    export GRAPH_ACCESS_TOKEN=$(powershell.exe -NoProfile -File ./scripts/Get-GraphToken.ps1 -Raw)
    python check_auth.py --auth-mode token_passthrough

.EXAMPLE
    # From cmd.exe
    for /f "delims=" %T in ('powershell -NoProfile -File scripts\Get-GraphToken.ps1 -Raw') do set GRAPH_ACCESS_TOKEN=%T

.EXAMPLE
    # Explicit override - a different cert/app than what's in config.json
    .\scripts\Get-GraphToken.ps1 -Thumbprint "AB12CD34..." -ClientId "..." -TenantId "..."
#>

[CmdletBinding()]
param(
    [string]$Thumbprint,

    [string]$ClientId,

    [string]$TenantId,

    [string]$ConfigPath,

    [ValidateSet("CurrentUser", "LocalMachine")]
    [string]$StoreLocation = "CurrentUser",

    [string]$EnvVarName = "GRAPH_ACCESS_TOKEN",

    [switch]$Raw
)

# In -Raw mode, stdout must contain ONLY the token - any status text goes to stderr so
# a calling shell's command substitution ($(), backticks, for /f) captures a clean value.
function Info($msg, [string]$Color = "Cyan") {
    if ($Raw) { [Console]::Error.WriteLine($msg) }
    else { Write-Host $msg -ForegroundColor $Color }
}
function Fail($msg) {
    if ($Raw) { [Console]::Error.WriteLine("ERROR: $msg") } else { Write-Error $msg }
    exit 1   # explicit nonzero exit so a calling shell's own error handling (set -e,
             # errorlevel checks, $?) can detect failure - a bare `return` from a script
             # does not reliably propagate as process exit code 1.
}

# Resolved here in the body, not as a param() default - $PSScriptRoot is not guaranteed
# to be populated yet when a param block's default value expressions are evaluated,
# depending on how the script was invoked (e.g. "Run Selection" in an editor rather than
# the whole file, or some dot-sourcing patterns). Falls back through the options that
# still identify the script's own folder, then finally the current directory.
if (-not $ConfigPath) {
    $scriptDir = $PSScriptRoot
    if (-not $scriptDir -and $MyInvocation.MyCommand.Path) {
        $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
    }
    if (-not $scriptDir) {
        $scriptDir = (Get-Location).Path
        Info "Could not determine this script's own folder (unusual invocation method) - falling back to the current directory ($scriptDir) to look for config.json."
    }
    $ConfigPath = Join-Path $scriptDir "config.json"
}

if (-not (Get-Module -ListAvailable -Name MSAL.PS)) {
    Info "MSAL.PS module not found - installing for the current user..."
    try {
        # -Force suppresses the "install from untrusted repository" prompt for PSGallery,
        # which matters for unattended use (Task Scheduler has no one to answer a prompt).
        # If PSGallery itself is blocked by network/org policy, this throws and we fail
        # loudly below rather than the run hanging indefinitely waiting for input.
        Install-Module -Name MSAL.PS -Scope CurrentUser -Force -AllowClobber -ErrorAction Stop
        Info "MSAL.PS installed."
    }
    catch {
        Fail "MSAL.PS is not installed and automatic install failed: $_`nInstall it manually: Install-Module MSAL.PS -Scope CurrentUser -Force`nIf that also fails, PSGallery may be blocked by network/org policy - check with whoever manages this machine's PowerShell Gallery access."
    }
}
Import-Module MSAL.PS

# Fill in anything not passed explicitly from config.json, so client_id/tenant_id/the
# store thumbprint only have to live in one place. Explicit -ClientId/-TenantId/
# -Thumbprint on the command line still override this, for testing against a
# different app or cert without touching config.json.
if (-not $ClientId -or -not $TenantId -or -not $Thumbprint) {
    if (-not (Test-Path $ConfigPath)) {
        Fail "config.json not found at $ConfigPath, and -ClientId/-TenantId/-Thumbprint were not all supplied explicitly. Either pass all three directly, or run this from a location where $ConfigPath exists."
    }
    try {
        $config = Get-Content $ConfigPath -Raw | ConvertFrom-Json
    }
    catch {
        Fail "Could not parse $ConfigPath as JSON: $_"
    }
    if (-not $ClientId) { $ClientId = $config.client_id }
    if (-not $TenantId) { $TenantId = $config.tenant_id }
    if (-not $Thumbprint) { $Thumbprint = $config.auth.token_passthrough_thumbprint }
}

foreach ($pair in @(@("ClientId", $ClientId), @("TenantId", $TenantId), @("Thumbprint", $Thumbprint))) {
    if (-not $pair[1]) {
        Fail "$($pair[0]) is empty. Pass -$($pair[0]) explicitly, or set the corresponding value in $ConfigPath (client_id / tenant_id / auth.token_passthrough_thumbprint)."
    }
}
if ($ClientId -notmatch '^[0-9a-fA-F-]{36}$') { Fail "ClientId '$ClientId' doesn't look like a GUID." }
if ($TenantId -notmatch '^[0-9a-fA-F-]{36}$') { Fail "TenantId '$TenantId' doesn't look like a GUID." }

$certPath = "Cert:\$StoreLocation\My\$Thumbprint"
$cert = Get-Item -Path $certPath -ErrorAction SilentlyContinue
if (-not $cert) {
    Fail "No certificate found at $certPath. List available certs with: Get-ChildItem Cert:\$StoreLocation\My | Select Subject, Thumbprint"
}
if (-not $cert.HasPrivateKey) {
    Fail "Certificate $Thumbprint has no private key available to this account/store. Nothing to sign with."
}

Info "Requesting token using cert '$($cert.Subject)' (private key stays in $StoreLocation store)..."

try {
    $result = Get-MsalToken -ClientId $ClientId -TenantId $TenantId `
        -ClientCertificate $cert -Scopes "https://graph.microsoft.com/.default"
}
catch {
    Fail "Token request failed: $_ -- check that this app registration has the certificate uploaded under Certificates & secrets, and that the required application permissions are admin-consented."
}

if (-not $result -or -not $result.AccessToken) {
    Fail "Get-MsalToken returned no access token."
}

$expiresIn = [int]($result.ExpiresOn - (Get-Date)).TotalMinutes

if ($Raw) {
    # stdout gets ONLY the token - this is what a calling shell's $()/backticks/for-f
    # captures. Everything else above already went to stderr via Info/Fail.
    [Console]::Error.WriteLine("Token acquired, expires in ~$expiresIn minutes.")
    Write-Output $result.AccessToken
}
else {
    # Native PowerShell use: set it directly in THIS session for convenience.
    Set-Item -Path "Env:$EnvVarName" -Value $result.AccessToken
    Info "Token acquired, expires in ~$expiresIn minutes. Set `$env:$EnvVarName in this shell." "Green"
    Info "Run Python commands from THIS SAME shell so the token is inherited, e.g.:" "Yellow"
    Info "  python check_auth.py --auth-mode token_passthrough"
}
