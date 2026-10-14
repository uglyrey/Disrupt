$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '../scripts/admin-tunnel.ps1')
function Assert-True($Value,$Message) { if (!$Value) { throw $Message } }
$taskFixturePort = 18090
$taskOccupiedListener = New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback,$taskFixturePort)
$taskOccupiedListener.Start()
try {
    $script:taskFakeListener = [pscustomobject]@{LocalAddress='127.0.0.1';LocalPort=$taskFixturePort;OwningProcess=12345}
    $script:taskFakeProcess = [pscustomobject]@{Name='ssh.exe';ExecutablePath=(Get-BerestaSshPath);CommandLine=(
        'ssh.exe -N -o StrictHostKeyChecking=yes -o HostKeyAlias=178.217.98.101 -o ForwardAgent=no -L 127.0.0.1:' + $taskFixturePort + ':10.77.0.1:18000 -J root@185.106.94.251 root@10.77.0.1')}
    function Get-NetTCPConnection { return $script:taskFakeListener }
    function Get-CimInstance { return $script:taskFakeProcess }
    $script:taskEndpointHealthy=$true
    $script:taskEndpointChecks=0
    function Test-BerestaAdminEndpoint { $script:taskEndpointChecks++;return $script:taskEndpointHealthy }
    Assert-True (Test-BerestaOwnedForward $taskFakeListener) 'Expected SSH route rejected'
    $taskPlan=Get-BerestaAdminTunnelPlan -PreferredPort $taskFixturePort
    Assert-True ($taskPlan.Reuse -and $taskPlan.Port -eq $taskFixturePort) 'Working SSH tunnel was not reused'
    $taskFakeProcess.Name='unrelated.exe'
    $script:taskEndpointChecks=0
    $taskPlan=Get-BerestaAdminTunnelPlan -PreferredPort $taskFixturePort
    Assert-True (!$taskPlan.Reuse -and $taskPlan.Port -ne $taskFixturePort) 'Unrelated listener was reused'
    Assert-True ($taskEndpointChecks -eq 0) 'Unknown HTTP listener was trusted'
    Assert-True (!(Test-BerestaPortFree $taskFixturePort)) 'Occupied listener was terminated'
    $taskFakeProcess.Name='ssh.exe'
    $taskOriginalCommand=$taskFakeProcess.CommandLine
    $taskFakeProcess.CommandLine=$taskOriginalCommand.Replace('10.77.0.1:18000','10.77.0.1:19000')
    Assert-True (!(Test-BerestaOwnedForward $taskFakeListener)) 'Wrong SSH destination accepted'
    $taskFakeProcess.CommandLine=$taskOriginalCommand.Replace('StrictHostKeyChecking=yes','StrictHostKeyChecking=no')
    Assert-True (!(Test-BerestaOwnedForward $taskFakeListener)) 'Unchecked host key accepted'
    $taskFakeProcess.CommandLine=$taskOriginalCommand
    $script:taskEndpointHealthy=$false
    $taskPlan=Get-BerestaAdminTunnelPlan -PreferredPort $taskFixturePort
    Assert-True (!$taskPlan.Reuse -and $taskPlan.Port -ne $taskFixturePort) 'Unhealthy tunnel was reused'
    $script:taskEndpointHealthy=$true
    $taskPlan=Get-BerestaAdminTunnelPlan -PreferredPort $taskFixturePort -SkipReuse
    Assert-True (!$taskPlan.Reuse) 'Diagnostic did not create an isolated test connection'
    Write-Output 'Passed: reuse, unrelated listener, free port, wrong destination, host key, unhealthy tunnel, diagnostic isolation'
} finally { $taskOccupiedListener.Stop() }
