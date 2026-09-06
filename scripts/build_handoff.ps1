$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$DistRoot = Join-Path $ProjectRoot "dist"
$Stage = Join-Path $DistRoot "hindsight-agent-teammate"
$Zip = Join-Path $DistRoot "hindsight-agent-teammate.zip"

if (-not $Stage.StartsWith($DistRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Unexpected staging path: $Stage"
}

New-Item -ItemType Directory -Force -Path $DistRoot | Out-Null
if (Test-Path -LiteralPath $Stage) {
    Remove-Item -LiteralPath $Stage -Recurse -Force
}
if (Test-Path -LiteralPath $Zip) {
    Remove-Item -LiteralPath $Zip -Force
}
New-Item -ItemType Directory -Force -Path $Stage | Out-Null

foreach ($directory in @("src", "tests", "scripts", "docs", ".streamlit", ".github")) {
    Copy-Item -LiteralPath (Join-Path $ProjectRoot $directory) -Destination $Stage -Recurse
}
foreach ($file in @(
    "README.md", "pyproject.toml", "docker-compose.yml", ".env.example",
    "LICENSE", ".gitignore", "STANDARD_ALIGNMENT_ZH.md", "DATABASE_DICTIONARY_ZH.md"
)) {
    Copy-Item -LiteralPath (Join-Path $ProjectRoot $file) -Destination $Stage
}

$FactoryTarget = Join-Path $Stage "data\factory"
$IncidentTarget = Join-Path $Stage "data\incidents"
$RawTarget = Join-Path $Stage "data\raw"
New-Item -ItemType Directory -Force -Path $FactoryTarget, $IncidentTarget, $RawTarget | Out-Null
Copy-Item -Path (Join-Path $ProjectRoot "data\factory\*.db") -Destination $FactoryTarget
Copy-Item -LiteralPath (Join-Path $ProjectRoot "data\incidents\incidents.db") -Destination $IncidentTarget
Copy-Item -Path (Join-Path $ProjectRoot "data\raw\*") -Destination $RawTarget -Recurse

Get-ChildItem -LiteralPath $Stage -Directory -Recurse -Force |
    Where-Object { $_.Name -eq "__pycache__" } |
    ForEach-Object {
        $CachePath = $_.FullName
        if (-not $CachePath.StartsWith($Stage, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Unexpected cache path: $CachePath"
        }
        Remove-Item -LiteralPath $CachePath -Recurse -Force
    }

Compress-Archive -Path (Join-Path $Stage "*") -DestinationPath $Zip -CompressionLevel Optimal
Get-Item -LiteralPath $Zip | Select-Object FullName, Length, LastWriteTime
