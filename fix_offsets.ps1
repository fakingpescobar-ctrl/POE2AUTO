param(
    [switch]$Quick,
    [int]$HP,
    [int]$Mana,
    [switch]$Help
)

$ErrorActionPreference = "Stop"
$ProjectDir = "C:\Projects\POE2AUTO"

if ($Help) {
    Write-Host @"

PoE2 Offset Fix Tool
====================
Usage:
  .\fix_offsets.ps1              # full scan
  .\fix_offsets.ps1 -Quick       # chain only
  .\fix_offsets.ps1 -HP 1234     # value-scan with known HP (recommended)

What it does:
  1. Runs offset_scanner.py to discover current PoE2 memory offsets
  2. If InGameState/AreaInstance offsets changed -> updates game_offsets.py
  3. Enables use_memory: true in config.yaml
  4. Vital offsets shown for info only (use --HP for accurate detection)
"@
    exit 0
}

# --- 1. Run offset scanner ---
Write-Host "=== PoE2 Offset Scanner ===" -ForegroundColor Cyan

$argsList = @()
if ($Quick) { $argsList += "--quick" }
if ($HP) { $argsList += "--hp"; $argsList += $HP.ToString() }
if ($Mana) { $argsList += "--mana"; $argsList += $Mana.ToString() }

Write-Host "Running: python offset_scanner.py $($argsList -join ' ')" -ForegroundColor Gray

$output = & python offset_scanner.py @argsList | Out-String
$exitCode = $LASTEXITCODE
if ($exitCode -ne 0) {
    Write-Host "Scanner exited with code $exitCode" -ForegroundColor Red
}
Write-Host $output

# --- 2. Parse result ---
$changed = @{}
$offsets = @{
    "InGameState.AREA_INSTANCE" = @{ "name" = "InGameState"; "field" = "AREA_INSTANCE" }
    "InGameState.CAMERA" = @{ "name" = "InGameState"; "field" = "CAMERA" }
    "AreaInstance.LOCAL_PLAYER" = @{ "name" = "AreaInstance"; "field" = "LOCAL_PLAYER" }
    "AreaInstance.AWAKE_ENTITIES" = @{ "name" = "AreaInstance"; "field" = "AWAKE_ENTITIES" }
}

foreach ($line in $output -split "`n") {
    $line = $line.Trim()
    foreach ($key in $offsets.Keys) {
        if ($line -match "$($key)\s*=\s*0x([0-9A-Fa-f]+)\s*->\s*0x([0-9A-Fa-f]+)") {
            $oldVal = "0x" + $matches[1].ToUpper()
            $newVal = "0x" + $matches[2].ToUpper()
            $changed[$key] = @{ "old" = $oldVal; "new" = $newVal }
            Write-Host "  $($key): $oldVal -> $newVal" -ForegroundColor Yellow
        }
    }
}

if ($changed.Count -eq 0) {
    Write-Host "`nNo offset changes detected. All offsets are current." -ForegroundColor Green
    $configPath = Join-Path $ProjectDir "config.yaml"
    if (Test-Path $configPath) {
        $cfg = Get-Content $configPath -Raw
        if ($cfg -match "use_memory:\s*false") {
            $cfg = $cfg -replace "use_memory:\s*false", "use_memory: true"
            Set-Content $configPath $cfg
            Write-Host "Enabled use_memory: true in config.yaml" -ForegroundColor Yellow
        }
    }
    exit 0
}

# --- 3. Update game_offsets.py ---
$offsetsPath = Join-Path $ProjectDir "game_offsets.py"
if (-not (Test-Path $offsetsPath)) {
    Write-Host "ERROR: game_offsets.py not found!" -ForegroundColor Red
    exit 1
}

$content = Get-Content $offsetsPath -Raw
$modified = $false

foreach ($key in $changed.Keys) {
    $info = $offsets[$key]
    if (-not $info) { continue }
    $infoNewVal = $changed[$key]["new"]
    if (-not $infoNewVal) { continue }

    $oldPattern = "$($info['field'])\s*=\s*0x[0-9A-Fa-f]+"
    $newPattern = "$($info['field']) = $infoNewVal"
    if ($content -match $oldPattern) {
        $content = $content -replace $oldPattern, $newPattern
        $modified = $true
        Write-Host "  Updated $($info['name']).$($info['field']) to $infoNewVal" -ForegroundColor Green
    }
}

if ($modified) {
    Set-Content $offsetsPath $content
    Write-Host "`nUpdated game_offsets.py" -ForegroundColor Green
} else {
    Write-Host "`nNo changes needed in game_offsets.py" -ForegroundColor Gray
}

# --- 4. Enable use_memory in config.yaml ---
$configPath = Join-Path $ProjectDir "config.yaml"
if (Test-Path $configPath) {
    $cfg = Get-Content $configPath -Raw
    if ($cfg -match "use_memory:\s*false") {
        $cfg = $cfg -replace "use_memory:\s*false", "use_memory: true"
        Set-Content $configPath $cfg
        Write-Host "Enabled use_memory: true in config.yaml" -ForegroundColor Yellow
    }
}

Write-Host "`n=== Done ===" -ForegroundColor Cyan
Write-Host "Offsets updated. Run the bot to verify." -ForegroundColor Green
