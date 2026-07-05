# Auto-sync hook: commits + pushes meaningful changes.
# Called by Claude Code Stop hook after each response.
# Cross-platform path detection — works on any machine regardless of username.

$ErrorActionPreference = "Continue"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

function Find-MasterRoot {
    $candidate = Split-Path -Parent $ProjectRoot
    if ((Test-Path "$candidate\CLAUDE.md") -or (Test-Path "$candidate/CLAUDE.md")) { return $candidate }
    if ($env:CLAUDE_MASTER_ROOT -and (Test-Path $env:CLAUDE_MASTER_ROOT)) { return $env:CLAUDE_MASTER_ROOT }
    return $null
}
$MasterRoot = Find-MasterRoot

function Send-Alert {
    param([string]$Message)
    Write-Host $Message -ForegroundColor Red
    $alertFile = $null
    if (Test-Path ".alert.env") { $alertFile = ".alert.env" }
    elseif ($MasterRoot -and (Test-Path "$MasterRoot\.alert.env")) { $alertFile = "$MasterRoot\.alert.env" }
    if (-not $alertFile) { return }
    $env = Get-Content $alertFile -Raw
    $tok = if ($env -match 'BOT_TOKEN=(.+)') { $matches[1].Trim() } else { $null }
    $cid = if ($env -match 'CHAT_ID=(.+)') { $matches[1].Trim() } else { $null }
    if ($tok -and $cid) {
        try { Invoke-RestMethod -Uri "https://api.telegram.org/bot$tok/sendMessage" -Method Post -Body @{chat_id=$cid; text=$Message} -TimeoutSec 5 | Out-Null } catch {}
    }
}

# 0. Git repo?
if (-not (Test-Path ".git")) { exit 0 }

# 1. Any changes?
$changes = git status --porcelain 2>$null
if ([string]::IsNullOrWhiteSpace("$changes")) { exit 0 }

# 2. Hard block on suspect files
$suspect = $changes | Where-Object { $_ -match '\.env$|\.alert\.env|wallet\.json|\.key$|\.pem$|id_rsa|mnemonic|\.exe$' -and $_ -notmatch '\.example$' }
if ($suspect) {
    Send-Alert "[SYNC-ABORT] $(Split-Path $ProjectRoot -Leaf) - suspect file:`n$($suspect -join "`n")"
    exit 1
}

# 3. Stage specific patterns (no `git add .`)
foreach ($pattern in @('CLAUDE.md', 'PROGRESS.md', 'DEFERRED.md', 'CHANGELOG.md', 'MEMORY.md', '.gitignore', 'README.md', '*.py', '*.ts', '*.tsx', '*.js', '*.rs', '*.toml', '*.yaml', '*.yml', '*.md')) {
    git add $pattern 2>$null
}
foreach ($dir in @('scripts', 'src', 'tests', 'docs', 'app', 'static', 'public', 'lib', 'feeds', 'detectors', 'wallet')) {
    if (Test-Path $dir) { git add $dir 2>$null }
}

# 4. Anything staged?
$staged = git diff --cached --name-only 2>$null
if ([string]::IsNullOrWhiteSpace("$staged")) { exit 0 }

# 5. Commit
$ts = Get-Date -Format "MM-dd HH:mm"
$proj = Split-Path $ProjectRoot -Leaf
git commit -qm "auto-sync ${proj} ${ts}" 2>$null
if ($LASTEXITCODE -ne 0) { exit 0 }

# 6. Push (upstream 있는 repo만 — 로컬 전용 repo는 조용히 스킵)
git rev-parse --abbrev-ref --symbolic-full-name "@{u}" 2>$null | Out-Null
if ($LASTEXITCODE -eq 0) {
    $push = git push 2>&1
    if ($LASTEXITCODE -ne 0) {
        Send-Alert "[SYNC-FAIL] $proj at $(Get-Date -Format 'yyyy-MM-dd HH:mm')`n$($push -join "`n")"
        exit 1
    }
}

# 7. Success: stamp .last-sync
Get-Date -Format "yyyy-MM-ddTHH:mm:ssK" | Out-File -FilePath ".last-sync" -Encoding UTF8 -NoNewline
exit 0