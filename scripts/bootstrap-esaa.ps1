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

$requiredFiles = @(
    "agent_result.schema.json",
    "roadmap.schema.json",
    "issues.schema.json",
    "lessons.schema.json",
    "AGENT_CONTRACT.yaml",
    "ORCHESTRATOR_CONTRACT.yaml",
    "RUNTIME_POLICY.yaml",
    "STORAGE_POLICY.yaml",
    "PROJECTION_SPEC.md",
    "agents_swarm.yaml"
)

$requiredRuntimeViewsInTarget = @(
    "roadmap.json",
    "issues.json",
    "lessons.json"
)

$optionalFiles = @(
    "PARCER_PROFILE.agent-spec.yaml",
    "PARCER_PROFILE.agent-impl.yaml",
    "PARCER_PROFILE.agent-qa.yaml",
    "PARCER_PROFILE.orchestrator-runtime.yaml",
    "PARCER_PROFILE_agent-docs.yaml"
)

$missingRequiredInFramework = @()
foreach ($filename in $requiredFiles) {
    $source = Join-Path $frameworkRoadmap $filename
    if (-not (Test-Path -LiteralPath $source)) {
        $missingRequiredInFramework += $filename
    }
}

if ($missingRequiredInFramework.Count -gt 0) {
    $missingText = ($missingRequiredInFramework -join ", ")
    throw "[bootstrap-esaa] Required artifacts missing in framework .roadmap: $missingText"
}

$copiedRequired = 0
foreach ($filename in $requiredFiles) {
    $source = Join-Path $frameworkRoadmap $filename
    $target = Join-Path $targetRoadmap $filename
    Copy-Item -LiteralPath $source -Destination $target -Force
    $copiedRequired++
    Write-Host "[bootstrap-esaa] Copied required: .roadmap/$filename"
}

$copiedOptional = 0
$missingOptional = @()
foreach ($filename in $optionalFiles) {
    $source = Join-Path $frameworkRoadmap $filename
    $target = Join-Path $targetRoadmap $filename
    if (Test-Path -LiteralPath $source) {
        Copy-Item -LiteralPath $source -Destination $target -Force
        $copiedOptional++
        Write-Host "[bootstrap-esaa] Copied optional: .roadmap/$filename"
    }
    else {
        $missingOptional += $filename
        Write-Warning "[bootstrap-esaa] Optional artifact not found (skipped): $filename"
    }
}

$missingRequiredInTarget = @()
foreach ($filename in $requiredFiles) {
    $target = Join-Path $targetRoadmap $filename
    if (-not (Test-Path -LiteralPath $target)) {
        $missingRequiredInTarget += $filename
    }
}

foreach ($filename in $requiredRuntimeViewsInTarget) {
    $target = Join-Path $targetRoadmap $filename
    if (-not (Test-Path -LiteralPath $target)) {
        $missingRequiredInTarget += $filename
    }
}

if ($missingRequiredInTarget.Count -gt 0) {
    $missingText = ($missingRequiredInTarget -join ", ")
    throw "[bootstrap-esaa] Bootstrap failed: required artifacts missing in target .roadmap: $missingText"
}

Write-Host "[bootstrap-esaa] Done."
Write-Host "[bootstrap-esaa] Project root: $projectRootPath"
Write-Host "[bootstrap-esaa] Summary: required copied=$copiedRequired/$($requiredFiles.Count), optional copied=$copiedOptional/$($optionalFiles.Count)"
if ($missingOptional.Count -gt 0) {
    Write-Host "[bootstrap-esaa] Optional missing: $($missingOptional -join ', ')"
}
Write-Host "[bootstrap-esaa] Next steps:"
Write-Host "  esaa --root `"$projectRootPath`" run --steps 1"
Write-Host "  esaa --root `"$projectRootPath`" verify"
