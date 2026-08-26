param(
    [Parameter(Mandatory = $true)]
    [string]$BindIP,

    [Parameter(Mandatory = $true)]
    [string]$RemoteAddress,

    [ValidateRange(1, 65535)]
    [int]$Port = 8787
)

$ErrorActionPreference = 'Stop'
$RuleName = "RepoMesh private API $BindIP`:$Port"

$ParsedBindIP = $null
if (-not [Net.IPAddress]::TryParse($BindIP, [ref]$ParsedBindIP)) {
    throw 'BindIP must be an exact IP address assigned to this Windows computer.'
}
if ($ParsedBindIP.Equals([Net.IPAddress]::Any) -or $ParsedBindIP.Equals([Net.IPAddress]::IPv6Any)) {
    throw 'Wildcard BindIP values are not allowed.'
}
if ($RemoteAddress -in @('*', 'Any', '0.0.0.0/0', '::/0')) {
    throw 'RemoteAddress must be the Mac Tailscale IP or a trusted private LAN IP/CIDR.'
}

$Assigned = Get-NetIPAddress -IPAddress $BindIP -ErrorAction SilentlyContinue
if (-not $Assigned) {
    throw "BindIP is not assigned to a local Windows interface: $BindIP"
}
if (Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue) {
    throw "Firewall rule already exists; inspect it before replacing: $RuleName"
}

New-NetFirewallRule `
    -DisplayName $RuleName `
    -Description 'RepoMesh API restricted to an exact private interface and Mac source address.' `
    -Direction Inbound `
    -Action Allow `
    -Protocol TCP `
    -LocalAddress $BindIP `
    -LocalPort $Port `
    -RemoteAddress $RemoteAddress `
    -Profile Any | Select-Object DisplayName, Enabled, Direction, Action, Profile
