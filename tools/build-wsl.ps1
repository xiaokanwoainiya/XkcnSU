param(
    [ValidateSet('doctor', 'bootstrap', 'lkm', 'kpatch', 'tests', 'check', 'clippy',
        'format', 'snapshot', 'ksuinit', 'ksud', 'manager', 'repack', 'matrix', 'all')]
    [string]$Stage = 'doctor',
    [string]$Kmi = 'android14-6.1',
    [ValidateSet('both', 'root', 'susfs', 'baseline')]
    [string]$Mode = 'both',
    [switch]$SourceReady,
    [ValidateRange(1, 2)]
    [int]$MatrixJobs = 1
)

$ErrorActionPreference = 'Stop'
$MilkRoot = Split-Path -Parent $PSScriptRoot
$MilkWslRoot = (& wsl.exe -d Ubuntu -u root --exec wslpath -u $MilkRoot).Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($MilkWslRoot)) {
    throw 'Cannot map XKCNSU to the Ubuntu WSL filesystem.'
}
$MilkScript = ''
$MilkArguments = @()
switch ($Stage) {
    'doctor' { $MilkScript = 'bootstrap-xkcnsu.sh'; $MilkArguments = @('doctor') }
    'bootstrap' { $MilkScript = 'bootstrap-xkcnsu.sh'; $MilkArguments = @('all') }
    'lkm' { $MilkScript = 'build-lkm.sh'; $MilkArguments = @($Kmi, $Mode) }
    'kpatch' { $MilkScript = 'build-kpatch.sh' }
    'all' {
        if (-not $SourceReady) { throw 'Full final builds require -SourceReady after source workers finish.' }
        $MilkScript = 'build-all.sh'; $MilkArguments = @('--source-ready')
    }
    'tests' { $MilkScript = 'build-rust.sh'; $MilkArguments = @('host-tests') }
    'check' { $MilkScript = 'build-rust.sh'; $MilkArguments = @('android-check') }
    'clippy' { $MilkScript = 'build-rust.sh'; $MilkArguments = @('android-clippy') }
    'format' { $MilkScript = 'build-rust.sh'; $MilkArguments = @('format-check') }
    'snapshot' {
        $MilkScript = 'build-release.sh'
        $MilkArguments = @('snapshot', $(if ($SourceReady) { '--source-ready' } else { '--preview' }))
    }
    'matrix' {
        & wsl.exe -d Ubuntu -u root --exec env "XKCNSU_ROOT=$MilkWslRoot" `
            python3 "$MilkWslRoot/tools/build-matrix.py" --jobs $MatrixJobs --stage-assets
        exit $LASTEXITCODE
    }
    default { $MilkScript = 'build-release.sh'; $MilkArguments = @($Stage) }
}
& wsl.exe -d Ubuntu -u root --exec env "XKCNSU_ROOT=$MilkWslRoot" `
    bash "$MilkWslRoot/tools/$MilkScript" @MilkArguments
exit $LASTEXITCODE
