param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot,

    [string]$FrameworkRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,

    [switch]$ForceInit,
    [switch]$SkipInit
)

$ErrorActionPreference = "Stop"

function Resolve-NormalizedPath {
    param([Parameter(Mandatory = $true)][string]$PathText)
    return [System.IO.Path]::GetFullPath((Resolve-Path $PathText).Path)
}

function Ensure-Directory {
    param([Parameter(Mandatory = $true)][string]$PathText)
    if (-not (Test-Path -LiteralPath $PathText)) {
        New-Item -ItemType Directory -Path $PathText | Out-Null
    }
}

if (-not (Test-Path -LiteralPath $ProjectRoot)) {
    New-Item -ItemType Directory -Path $ProjectRoot | Out-Null
}

$projectRootPath = Resolve-NormalizedPath -PathText $ProjectRoot
$frameworkRootPath = Resolve-NormalizedPath -PathText $FrameworkRoot

if ($projectRootPath -eq $frameworkRootPath) {
    throw "Refusing to bootstrap into the framework repository root. Use a separate project folder."
}

$frameworkRoadmap = Join-Path $frameworkRootPath ".roadmap"
$targetRoadmap = Join-Path $projectRootPath ".roadmap"

if (-not (Test-Path -LiteralPath $frameworkRoadmap)) {
    throw "Framework .roadmap folder not found at: $frameworkRoadmap"
}

Ensure-Directory -PathText $targetRoadmap

if (-not $SkipInit) {
    $initArgs = @("--root", $projectRootPath, "init")
    if ($ForceInit) {
        $initArgs += "--force"
    }
    Write-Host "[bootstrap-esaa] Running: esaa $($initArgs -join ' ')"
    & esaa @initArgs
}

$governanceFiles = @(
    "AGENT_CONTRACT.yaml",
    "ORCHESTRATOR_CONTRACT.yaml",
    "RUNTIME_POLICY.yaml",
    "STORAGE_POLICY.yaml",
    "PROJECTION_SPEC.md",
    "agent_result.schema.json",
    "roadmap.schema.json",
    "issues.schema.json",
    "lessons.schema.json",
    "agents_swarm.yaml",
    "PARCER_PROFILE.agent-spec.yaml",
    "PARCER_PROFILE.agent-impl.yaml",
    "PARCER_PROFILE.agent-qa.yaml",
    "PARCER_PROFILE.orchestrator-runtime.yaml",
    "PARCER_PROFILE_agent-docs.yaml"
)

foreach ($filename in $governanceFiles) {
    $source = Join-Path $frameworkRoadmap $filename
    $target = Join-Path $targetRoadmap $filename
    if (-not (Test-Path -LiteralPath $source)) {
        Write-Warning "[bootstrap-esaa] Missing in framework (skipped): $filename"
        continue
    }
    Copy-Item -LiteralPath $source -Destination $target -Force
    Write-Host "[bootstrap-esaa] Copied: .roadmap/$filename"
}

Write-Host "[bootstrap-esaa] Done."
Write-Host "[bootstrap-esaa] Project root: $projectRootPath"
Write-Host "[bootstrap-esaa] Next steps:"
Write-Host "  esaa --root `"$projectRootPath`" run --steps 1"
Write-Host "  esaa --root `"$projectRootPath`" verify"
