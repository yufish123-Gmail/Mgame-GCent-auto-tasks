param(
    [switch]$Execute,
    [ValidateSet('all','recommend','editor','hot')][string]$Section = 'all',
    [ValidateRange(0,2147483647)][int]$Limit = 0,
    [string]$Serial,
    [string]$Python,
    [ValidateSet('foreground','desktop')][string]$Timing = 'foreground'
)
$ErrorActionPreference = 'Stop'
$env:PYTHONIOENCODING = 'utf-8'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$candidates = if ($Python) { @($Python) } else {
    @(
        (Join-Path $PSScriptRoot '.venv/Scripts/python.exe'),
        (Join-Path $PSScriptRoot '../Mgame-FCash-auto-tasks/.venv/Scripts/python.exe'),
        (Get-Command python.exe -ErrorAction SilentlyContinue).Source
    )
}
foreach ($candidate in $candidates) {
    if (!$candidate -or !(Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
    & $candidate -c 'import cv2, numpy'
    if ($LASTEXITCODE -ne 0) { continue }
    $arguments = @((Join-Path $PSScriptRoot 'gcenter.py'), '--section', $Section, '--limit', $Limit,
                   '--timing', $Timing.ToLowerInvariant())
    if ($Serial) { $arguments += @('--serial', $Serial) }
    if ($Execute) { $arguments += '--execute' }
    & $candidate @arguments
    exit $LASTEXITCODE
}
throw 'Supply a Python interpreter with OpenCV and NumPy using -Python.'
