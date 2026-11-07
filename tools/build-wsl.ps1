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
$XkcnsuRoot = Split-Path -Parent $PSScriptRoot
$XkcnsuWslRoot = (& wsl.exe -d Ubuntu -u root --exec wslpath -u $XkcnsuRoot).Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($XkcnsuWslRoot)) {
    throw 'Cannot map XKCNSU to the Ubuntu WSL filesystem.'
}
$XkcnsuScript = ''
$XkcnsuArguments = @()
switch ($Stage) {
    'doctor' { $XkcnsuScript = 'bootstrap-xkcnsu.sh'; $XkcnsuArguments = @('doctor') }
    'bootstrap' { $XkcnsuScript = 'bootstrap-xkcnsu.sh'; $XkcnsuArguments = @('all') }
    'lkm' { $XkcnsuScript = 'build-lkm.sh'; $XkcnsuArguments = @($Kmi, $Mode) }
    'kpatch' { $XkcnsuScript = 'build-kpatch.sh' }
    'all' {
        if (-not $SourceReady) { throw 'Full final builds require -SourceReady after source workers finish.' }
        $XkcnsuScript = 'build-all.sh'; $XkcnsuArguments = @('--source-ready')
    }
    'tests' { $XkcnsuScript = 'build-rust.sh'; $XkcnsuArguments = @('host-tests') }
    'check' { $XkcnsuScript = 'build-rust.sh'; $XkcnsuArguments = @('android-check') }
    'clippy' { $XkcnsuScript = 'build-rust.sh'; $XkcnsuArguments = @('android-clippy') }
    'format' { $XkcnsuScript = 'build-rust.sh'; $XkcnsuArguments = @('format-check') }
    'snapshot' {
        $XkcnsuScript = 'build-release.sh'
        $XkcnsuArguments = @('snapshot', $(if ($SourceReady) { '--source-ready' } else { '--preview' }))
    }
    'matrix' {
        & wsl.exe -d Ubuntu -u root --exec env "XKCNSU_ROOT=$XkcnsuWslRoot" `
            python3 "$XkcnsuWslRoot/tools/build-matrix.py" --jobs $MatrixJobs --stage-assets
        exit $LASTEXITCODE
    }
    default { $XkcnsuScript = 'build-release.sh'; $XkcnsuArguments = @($Stage) }
}
& wsl.exe -d Ubuntu -u root --exec env "XKCNSU_ROOT=$XkcnsuWslRoot" `
    bash "$XkcnsuWslRoot/tools/$XkcnsuScript" @XkcnsuArguments
exit $LASTEXITCODE
