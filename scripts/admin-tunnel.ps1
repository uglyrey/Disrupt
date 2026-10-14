# Never terminate an existing listener or trust its HTTP page alone.
function Get-BerestaSshPath {
    $taskBinary = Join-Path $env:WINDIR 'System32\OpenSSH\ssh.exe'
    if (!(Test-Path -LiteralPath $taskBinary -PathType Leaf)) { throw 'Не найден клиент OpenSSH для Windows.' }
    return $taskBinary
}

function Test-BerestaAdminEndpoint {
    param([int]$Port)
    $taskResponse = $null
    $taskReader = $null
    try {
        $taskRequest = [Net.HttpWebRequest]::Create('http://127.0.0.1:' + $Port + '/admin')
        $taskRequest.Proxy = $null
        $taskRequest.AllowAutoRedirect = $false
        $taskRequest.Timeout = 2000
        $taskRequest.ReadWriteTimeout = 2000
        $taskResponse = $taskRequest.GetResponse()
        if ([int]$taskResponse.StatusCode -ne 200) { return $false }
        $taskReader = New-Object IO.StreamReader($taskResponse.GetResponseStream())
        $taskHtml = $taskReader.ReadToEnd()
        return ($taskHtml.Contains('id="admin-username"') -and $taskHtml.Contains('id="admin-code"') -and
            $taskHtml.Contains('/static/admin.auth.js'))
    } catch { return $false }
    finally {
        if ($null -ne $taskReader) { $taskReader.Dispose() }
        if ($null -ne $taskResponse) { $taskResponse.Close() }
    }
}

function Test-BerestaOwnedForward {
    param($Listener)
    if ($Listener.LocalAddress -ne '127.0.0.1') { return $false }
    try {
        $taskOwner = Get-CimInstance Win32_Process -Filter ('ProcessId = ' + $Listener.OwningProcess) -ErrorAction Stop
        if (!$taskOwner -or $taskOwner.Name -ne 'ssh.exe' -or $taskOwner.ExecutablePath -ne (Get-BerestaSshPath)) { return $false }
        $taskCommand = $taskOwner.CommandLine
        $taskForward = [regex]::Escape('127.0.0.1:' + $Listener.LocalPort + ':10.77.0.1:18000')
        return ($taskCommand -match ('(?:^|\s)-L\s+"?' + $taskForward + '"?(?:\s|$)') -and
            $taskCommand -match '(?:^|\s)-J\s+"?root@185\.106\.94\.251"?(?:\s|$)' -and
            $taskCommand -match '(?:^|\s)"?root@10\.77\.0\.1"?(?:\s|$)' -and
            $taskCommand -match '(?:^|\s)StrictHostKeyChecking=yes(?:\s|$)' -and
            $taskCommand -match '(?:^|\s)HostKeyAlias=178\.217\.98\.101(?:\s|$)' -and
            $taskCommand -match '(?:^|\s)ForwardAgent=no(?:\s|$)')
    } catch { return $false }
}

function Test-BerestaPortFree {
    param([int]$Port)
    $taskListener = New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback,$Port)
    $taskListener.Server.ExclusiveAddressUse = $true
    try { $taskListener.Start(); return $true } catch { return $false }
    finally { $taskListener.Stop() }
}

function Get-BerestaAdminTunnelPlan {
    param([int]$PreferredPort = 18000, [switch]$SkipReuse)
    $taskPorts = @($PreferredPort) + @(18000..18020) | Select-Object -Unique
    $taskListeners = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
        Where-Object { $taskPorts -contains $_.LocalPort })
    foreach ($taskPort in $taskPorts) {
        if ($SkipReuse) { break }
        foreach ($taskListener in @($taskListeners | Where-Object LocalPort -eq $taskPort)) {
            if ((Test-BerestaOwnedForward $taskListener) -and (Test-BerestaAdminEndpoint $taskPort)) {
                return [pscustomobject]@{Port=$taskPort;Reuse=$true;ProcessId=$taskListener.OwningProcess}
            }
        }
    }
    foreach ($taskPort in $taskPorts) {
        if (Test-BerestaPortFree $taskPort) { return [pscustomobject]@{Port=$taskPort;Reuse=$false;ProcessId=$null} }
    }
    throw 'Не удалось найти свободный локальный порт. Закройте лишние приложения или задайте -LocalPort.'
}
