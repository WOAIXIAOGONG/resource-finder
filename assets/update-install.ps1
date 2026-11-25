param([Parameter(Mandatory=$true)][string]$PlanPath)
$ErrorActionPreference = 'Stop'
$plan = Get-Content -LiteralPath $PlanPath -Raw -Encoding UTF8 | ConvertFrom-Json
$work = [IO.Path]::GetFullPath((Split-Path -Parent $PlanPath))
$target = [IO.Path]::GetFullPath($plan.target)
$stage = [IO.Path]::GetFullPath($plan.stage)
$backup = Join-Path $work 'backup'
$journal = Join-Path $work 'progress.json'
$moved = [Collections.Generic.List[string]]::new()
$installed = [Collections.Generic.List[string]]::new()
$lock = $null
$allowed = @('资源搜寻器.exe','_internal','app','tools','assets','browser-extension','download-login-helper','vendor','main.py','requirements.txt','README.md','AGENTS.md','CHANGELOG.md','使用说明.txt','详情介绍.md','.gitignore','update-manifest.json')
function Save-Json($Path,$Data) {
    [IO.Directory]::CreateDirectory((Split-Path -Parent $Path)) | Out-Null
    $temp = $Path + '.tmp'
    [IO.File]::WriteAllText($temp,($Data | ConvertTo-Json -Depth 6),[Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $temp -Destination $Path -Force
}
function Progress-State($State,$File='') { Save-Json $journal @{state=$State;file=$File;version=$plan.version} }
function File-Sha256($Path) {
    $stream=[IO.File]::OpenRead($Path);$hash=[Security.Cryptography.SHA256]::Create()
    try { return [BitConverter]::ToString($hash.ComputeHash($stream)).Replace('-','').ToLowerInvariant() }
    finally { $stream.Dispose();$hash.Dispose() }
}
try {
    if ($plan.schema -ne 1 -or (Split-Path -Leaf $work) -notlike '.resourcefinder-update-*' -or $stage -ne (Join-Path $work 'stage') -or (Split-Path -Parent $target) -ne (Split-Path -Parent $work) -or $target -eq $work) { throw 'Invalid update paths' }
    if ($plan.version -notmatch '^\d+\.\d+\.\d+$') { throw 'Invalid version' }
    Progress-State 'waiting'
    $deadline = (Get-Date).AddSeconds(90)
    while ($plan.parent -and $plan.parent.Count -eq 2) {
        $parentProcess = Get-Process -Id $plan.parent[0] -ErrorAction SilentlyContinue
        if (-not $parentProcess) { break }
        if ([string]$parentProcess.StartTime.ToFileTimeUtc() -ne [string]$plan.parent[1]) { break }
        if ((Get-Date) -gt $deadline) { throw 'Original application has not exited; no files replaced' }
        Start-Sleep -Milliseconds 200
    }
    $lock = [IO.File]::Open((Join-Path $target '.update.lock'),[IO.FileMode]::OpenOrCreate,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)
    Progress-State 'validating'
    $manifest = Get-Content -LiteralPath (Join-Path $stage 'update-manifest.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($manifest.schema -ne 1 -or $manifest.version -ne $plan.version) { throw 'Manifest version mismatch' }
    $expected = @{}
    foreach ($entry in $manifest.files.PSObject.Properties) {
        $name=$entry.Name; $path=[IO.Path]::GetFullPath((Join-Path $stage $name))
        if (-not $path.StartsWith($stage+'\',[StringComparison]::OrdinalIgnoreCase) -or $name.Contains('..') -or $name.Contains(':') -or $name.Contains('\') -or $allowed -notcontains $name.Split('/')[0] -or $expected.ContainsKey($name)) { throw 'Invalid manifest path' }
        $expected[$name]=$true
        $item=Get-Item -LiteralPath $path -Force
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -or $item.Length -ne $entry.Value.size -or (File-Sha256 $path) -ne $entry.Value.sha256) { throw ('File verification failed: '+$name) }
    }
    if (-not $expected.ContainsKey('资源搜寻器.exe')) { throw 'Executable missing' }
    foreach ($item in Get-ChildItem -LiteralPath $stage -Force -Recurse) {
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Links prohibited' }
        if (-not $item.PSIsContainer) {
            $name=$item.FullName.Substring($stage.Length+1).Replace('\','/')
            if ($name -ne 'update-manifest.json' -and -not $expected.ContainsKey($name)) { throw ('Unexpected file: '+$name) }
        }
    }
    if (Test-Path -LiteralPath $backup) { throw 'Backup already exists; refusing to overwrite' }
    [IO.Directory]::CreateDirectory($backup) | Out-Null
    foreach ($item in Get-ChildItem -LiteralPath $stage -Force) {
        if ($allowed -notcontains $item.Name) { throw 'Unexpected top-level directory' }
        $old=Join-Path $target $item.Name
        if (Test-Path -LiteralPath $old) {
            if ((Get-Item -LiteralPath $old -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Destination links prohibited' }
        }
    }
    foreach ($item in Get-ChildItem -LiteralPath $stage -Force) {
        $name=$item.Name; $old=Join-Path $target $name
        Progress-State 'replacing' $name
        if (Test-Path -LiteralPath $old) { Move-Item -LiteralPath $old -Destination (Join-Path $backup $name); $moved.Add($name) }
        $installed.Add($name)
        Copy-Item -LiteralPath $item.FullName -Destination $old -Recurse
    }
    Progress-State 'checking'
    $env:PYINSTALLER_RESET_ENVIRONMENT='1'
    $health=Start-Process -FilePath (Join-Path $target '资源搜寻器.exe') -ArgumentList @('--update-healthcheck',$plan.version) -WindowStyle Hidden -PassThru
    if (-not $health.WaitForExit(120000)) { $health.Kill(); throw 'New executable startup check timed out' }
    if ($health.ExitCode -ne 0) { throw 'New executable startup check failed' }
    Save-Json $plan.receipt @{ok=$true;version=$plan.version;backup=$backup}
    Progress-State 'verified'
    if ($plan.restart) { Start-Process -FilePath (Join-Path $target '资源搜寻器.exe') -WorkingDirectory $target -WindowStyle Normal | Out-Null }
    exit 0
} catch {
    $failure=$_.Exception.Message
    $rollbackError=''
    try {
        for ($i=$installed.Count-1;$i -ge 0;$i--) {
            $path=[IO.Path]::GetFullPath((Join-Path $target $installed[$i]))
            if (-not $path.StartsWith($target+'\',[StringComparison]::OrdinalIgnoreCase)) { throw 'Rollback path outside target' }
            if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Recurse -Force }
        }
        for ($i=$moved.Count-1;$i -ge 0;$i--) { Move-Item -LiteralPath (Join-Path $backup $moved[$i]) -Destination (Join-Path $target $moved[$i]) }
    } catch { $rollbackError=$_.Exception.Message }
    Save-Json $plan.receipt @{ok=$false;error=$failure;rollback_error=$rollbackError;backup=$backup}
    Progress-State 'failed'
    if ($plan.restart) {
        Add-Type -AssemblyName PresentationFramework
        [Windows.MessageBox]::Show(('更新未完成。'+$failure+"`n"+$rollbackError),'资源搜寻器更新') | Out-Null
    }
    exit 1
} finally { if ($lock) { $lock.Dispose() } }
