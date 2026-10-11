param([string]$KeyPath = (Join-Path $env:USERPROFILE '.ssh\beresta_admin'))
$ErrorActionPreference = 'Stop'
if (!(Test-Path -LiteralPath $KeyPath -PathType Leaf)) { throw 'Set -KeyPath to your own private SSH key file. Do not send that file to anyone.' }
$taskKeys = & ssh-add -l 2>$null
if ($LASTEXITCODE -ne 0 -or !(($taskKeys -join "`n").Contains('SHA256:0vCwybtT7kDLG0b5BKbep4WvvEMA8ls5V6P49x/LDXU'))) {
    throw 'Load your beresta_admin key into ssh-agent with ssh-add first. Enter the passphrase only on your computer.'
}
$taskTcp = New-Object Net.Sockets.TcpClient
$taskBusy = $false
try { $taskTcp.Connect('127.0.0.1',18000); $taskBusy = $true } catch {} finally { $taskTcp.Dispose() }
if ($taskBusy) { throw 'Local port 18000 is already in use. Close the previous admin tunnel.' }
$taskIdentity = (Resolve-Path -LiteralPath $KeyPath).Path.Replace('\','/')
$taskKnown = (Join-Path $PSScriptRoot 'beresta-admin-known-hosts').Replace('\','/')
if (($taskIdentity + $taskKnown) -match '["\r\n]') { throw 'Unsupported path characters.' }
$taskConfig = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'beresta-admin.conf') -Raw
$taskConfig = $taskConfig.Replace('~/.ssh/beresta_admin','"' + $taskIdentity + '"')
$taskConfig = $taskConfig.Replace('~/.ssh/beresta-admin-known-hosts','"' + $taskKnown + '"')
$taskTemp = [IO.Path]::GetTempFileName()
[IO.File]::WriteAllText($taskTemp,$taskConfig,(New-Object Text.UTF8Encoding($false)))
$taskProcess = $null
try {
    $taskOptions = @('-F',('"' + $taskTemp + '"'),'-N','-o','BatchMode=yes','-o','ConnectTimeout=20','beresta-admin')
    $taskProcess = Start-Process ssh.exe -ArgumentList $taskOptions -PassThru -WindowStyle Hidden
    $taskReady = $false
    for ($taskAttempt = 0; $taskAttempt -lt 40; $taskAttempt++) {
        if ($taskProcess.HasExited) { throw 'SSH connection failed. Check the loaded key and internet connection.' }
        $taskTcp = New-Object Net.Sockets.TcpClient
        try { $taskTcp.Connect('127.0.0.1',18000); $taskReady = $true } catch {} finally { $taskTcp.Dispose() }
        if ($taskReady) { break }
        Start-Sleep -Milliseconds 500
    }
    if (!$taskReady) { throw 'SSH tunnel did not become ready.' }
    Start-Process 'http://127.0.0.1:18000/admin'
    Read-Host 'Keep this window open while using the admin page. Press Enter to disconnect' | Out-Null
} finally {
    if ($null -ne $taskProcess) {
        if (!$taskProcess.HasExited) { $taskProcess.Kill() }
        $taskProcess.Dispose()
    }
    Remove-Item -LiteralPath $taskTemp -Force
}
