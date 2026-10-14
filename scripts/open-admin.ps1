param(
    [ValidateRange(1024,65535)][int]$LocalPort = 18000,
    [switch]$CheckOnly,
    [switch]$TestConnection
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'admin-tunnel.ps1')
$taskPlan = Get-BerestaAdminTunnelPlan -PreferredPort $LocalPort -SkipReuse:$TestConnection
$taskUrl = 'http://127.0.0.1:' + $taskPlan.Port + '/admin'
if ($CheckOnly) { $taskPlan; return }
if ($taskPlan.Reuse) {
    Write-Host ('Рабочий туннель уже запущен. Админка: ' + $taskUrl)
    if (!$TestConnection) { Start-Process $taskUrl }
    return
}
if ($taskPlan.Port -ne $LocalPort) {
    Write-Host ('Порт ' + $LocalPort + ' занят. Используем свободный порт ' + $taskPlan.Port + '.')
}
$taskOptions = @('-N','-o','IPQoS=none','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes',
    '-o','HostKeyAlias=178.217.98.101','-o','ConnectTimeout=20','-o','ForwardAgent=no',
    '-o','ExitOnForwardFailure=yes','-o','ServerAliveInterval=30','-o','ServerAliveCountMax=3',
    '-L',('127.0.0.1:' + $taskPlan.Port + ':10.77.0.1:18000'),'-J','root@185.106.94.251','root@10.77.0.1')
$taskSshProcess = Start-Process -FilePath (Get-BerestaSshPath) -ArgumentList $taskOptions -PassThru -WindowStyle Hidden
try {
    $taskReady = $false
    $taskDeadline = [DateTime]::UtcNow.AddSeconds(45)
    while ([DateTime]::UtcNow -lt $taskDeadline) {
        $taskSshProcess.Refresh()
        if ($taskSshProcess.HasExited) { throw 'Не удалось подключиться по SSH. Проверьте интернет и загруженный ключ в ssh-agent.' }
        if (Test-BerestaAdminEndpoint -Port $taskPlan.Port) { $taskReady = $true; break }
        Start-Sleep -Milliseconds 500
    }
    if (!$taskReady) { throw 'Туннель создан, но админка не отвечает. Попробуйте запустить ещё раз.' }
    Write-Host ('Админка доступна: ' + $taskUrl)
    if ($TestConnection) { return }
    Start-Process $taskUrl
    Read-Host 'Оставьте это окно открытым. Нажмите Enter, чтобы закрыть созданный туннель' | Out-Null
} finally {
    $taskSshProcess.Refresh()
    if (!$taskSshProcess.HasExited) { $taskSshProcess.Kill(); $taskSshProcess.WaitForExit(5000) | Out-Null }
    $taskSshProcess.Dispose()
}
