# CredSSP pre-flight check. Run on a Windows DHCP server in the target
# environment, as an administrator. Reports whether policy would block the
# API from authenticating with CredSSP.
#
#   .\credssp-precheck.ps1
#   .\credssp-precheck.ps1 -ServiceAccount svc-dhcp   # also checks the account
#
# READ-ONLY. Safe to run on a production DHCP server.
#
# It calls only Get-Item, Get-ItemProperty, Get-ChildItem, Test-Path and an
# ADSI LDAP search. It changes no setting, writes no file, restarts no service,
# and does not touch the DHCP role or its database at all — it never even reads
# DHCP state, only WinRM configuration, two policy registry keys, and (with
# -ServiceAccount) one read-only directory lookup. Administrator rights are
# needed to *read* WSMan:\localhost\Service; the AD lookup needs none.
#
# Enabling CredSSP is a SEPARATE, deliberate step (Enable-WSManCredSSP -Role
# Server). This script never runs it. Verify before running:
#   Select-String -Path .\credssp-precheck.ps1 -Pattern '(Set|New|Remove|Enable|Disable|Add|Start|Stop|Restart)-'
# Any match inside quotes is printed guidance, not executed code.

param([string]$ServiceAccount = '')

'=== 1. WinRM Service: is CredSSP auth allowed? (THE DECIDING CHECK) ==='
try { 'effective WSMan setting : ' + (Get-Item WSMan:\localhost\Service\Auth\CredSSP).Value }
catch { 'effective WSMan setting : could not read -> ' + $_.Exception.Message }

$p = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WinRM\Service'
if (Test-Path $p) {
    $v = (Get-ItemProperty $p -ErrorAction SilentlyContinue).AllowCredSSP
    if ($null -eq $v) { 'GPO AllowCredSSP        : not set by policy (good - locally settable)' }
    elseif ($v -eq 1)  { 'GPO AllowCredSSP        : 1 - explicitly ALLOWED by policy (good)' }
    else               { 'GPO AllowCredSSP        : 0 - BLOCKED BY POLICY (this is the blocker)' }
} else { 'GPO AllowCredSSP        : no WinRM policy key (good - not managed by GPO)' }

'
=== 2. Encryption Oracle Remediation (CVE-2018-0886) ==='
$c = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System\CredSSP\Parameters'
if (Test-Path $c) {
    $o = (Get-ItemProperty $c -ErrorAction SilentlyContinue).AllowEncryptionOracle
    switch ($o) {
        0       { 'AllowEncryptionOracle   : 0 Force Updated Clients (secure; fine for a patched client)' }
        1       { 'AllowEncryptionOracle   : 1 Mitigated' }
        2       { 'AllowEncryptionOracle   : 2 Vulnerable (works, but insecure)' }
        default { 'AllowEncryptionOracle   : not set (defaults to Force Updated Clients)' }
    }
} else { 'AllowEncryptionOracle   : not set (defaults to Force Updated Clients)' }

'
=== 3. Service account restrictions ==='
# Queried over plain LDAP via ADSI: no RSAT, no ActiveDirectory module, no
# domain controller access and no admin rights. Reading group membership is
# something any authenticated domain user may do. Still read-only.
if (-not $ServiceAccount) {
    'skipped - re-run with the account the API will authenticate as, e.g.'
    '  .\credssp-precheck.ps1 -ServiceAccount svc-dhcp'
} else {
    try {
        $u = ([ADSISearcher]"(&(objectCategory=user)(sAMAccountName=$ServiceAccount))").FindOne()
        if (-not $u) {
            "account '$ServiceAccount' : NOT FOUND in this domain"
        } else {
            $inProtected = @($u.Properties.memberof) -match 'CN=Protected Users'
            if ($inProtected) {
                "account '$ServiceAccount' : IN 'Protected Users' - CredSSP delegation WILL be blocked"
            } else {
                "account '$ServiceAccount' : not in 'Protected Users' (good)"
            }
            $uac = [int]$u.Properties.useraccountcontrol[0]
            if ($uac -band 0x100000) {
                "  NOT_DELEGATED flag      : SET ('sensitive and cannot be delegated') - investigate"
            } else {
                "  NOT_DELEGATED flag      : clear (good)"
            }
        }
    } catch {
        'AD lookup failed (machine not domain-joined?) -> ' + $_.Exception.Message
    }
}

'
=== 4. RED HERRING - client-side delegation policy ==='
$d = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\CredentialsDelegation'
if (Test-Path $d) {
    $props = Get-ItemProperty $d -ErrorAction SilentlyContinue
    'AllowFreshCredentials   : ' + $props.AllowFreshCredentials
    'ConcatenateDefaults...  : ' + $props.ConcatenateDefaults_AllowFresh
    if (Test-Path "$d\AllowFreshCredentials") {
        'SPN allow-list entries  :'
        (Get-Item "$d\AllowFreshCredentials").Property | ForEach-Object {
            '  ' + (Get-ItemProperty "$d\AllowFreshCredentials").$_
        }
    }
} else { 'CredentialsDelegation   : no policy key present' }
'NOTE: this section governs the *Windows* CredSSP client. The API is a Linux'
'      client and never consults it. It matters only if you test with'
'      Enter-PSSession -Authentication CredSSP from a Windows box - that test'
'      can fail here while the API works fine. Do not read it as a blocker.'

'
=== 5. Current WinRM auth surface ==='
Get-ChildItem WSMan:\localhost\Service\Auth | Select-Object Name, Value | Format-Table -AutoSize | Out-String
