#Requires -Version 5.1
<#
.SYNOPSIS
    ZYAgent 项目打包脚本（基于 UnifyPy + Inno Setup）

.DESCRIPTION
    使用 UnifyPy 调用 PyInstaller 打包为 onedir 可执行文件，
    再通过 Inno Setup 生成 Windows 安装包（Setup.exe）。

.PARAMETER Clean
    清理上一次的构建产物（dist、build、installer、spec 缓存）后重新打包。
    建议修改代码或配置后使用。

.PARAMETER SkipInstaller
    只生成可执行文件，跳过安装包生成。

.PARAMETER SkipExe
    跳过 PyInstaller 可执行文件构建（仅在已有 dist 时重新生成安装包有用）。

.EXAMPLE
    .\build.ps1
    增量打包（复用已有 dist，仅重新生成安装包）。

.EXAMPLE
    .\build.ps1 -Clean
    清理后完整重新打包。

.EXAMPLE
    .\build.ps1 -SkipInstaller
    只生成可执行文件，不生成安装包。
#>

[CmdletBinding()]
param(
    [switch]$Clean,
    [switch]$SkipInstaller,
    [switch]$SkipExe
)

# ============================================================
# 配置区：根据你的环境修改以下路径
# ============================================================
$ProjectDir   = "E:\LLM\Agent\ZYAgent"          # 项目根目录（含 main.py 和 build.json）
$ConfigFile   = Join-Path $ProjectDir "build.json"  # UnifyPy 配置文件
$UnifypyExe   = "E:\Python313\Scripts\unifypy.exe"  # unifypy 可执行文件路径
$InnoSetupDir = "E:\download\Inno Setup 6"          # Inno Setup 安装目录
# ============================================================

$ErrorActionPreference = "Stop"

function Write-Step($msg) {
    Write-Host ""
    Write-Host "==> $msg" -ForegroundColor Cyan
}

function Test-PathOrDie($path, $label) {
    if (-not (Test-Path $path)) {
        Write-Host "错误：找不到 $label：$path" -ForegroundColor Red
        exit 1
    }
}

# ---------- 环境检查 ----------
Write-Step "检查构建环境"

Test-PathOrDie $ProjectDir   "项目目录"
Test-PathOrDie $ConfigFile   "build.json 配置"
Test-PathOrDie $UnifypyExe   "unifypy"

$IsccExe = Join-Path $InnoSetupDir "ISCC.exe"
if (-not $SkipInstaller) {
    Test-PathOrDie $IsccExe "Inno Setup (ISCC.exe)"
}

Write-Host "项目目录   : $ProjectDir"
Write-Host "配置文件   : $ConfigFile"
Write-Host "UnifyPy    : $UnifypyExe"
if (-not $SkipInstaller) {
    Write-Host "Inno Setup : $IsccExe"
}

# ---------- 清理（可选） ----------
if ($Clean) {
    Write-Step "清理上一次构建产物"
    $artifacts = @(
        (Join-Path $ProjectDir "dist"),
        (Join-Path $ProjectDir "build"),
        (Join-Path $ProjectDir "installer"),
        (Join-Path $ProjectDir "ZYAgent.spec")
    )
    foreach ($art in $artifacts) {
        if (Test-Path $art) {
            Remove-Item $art -Recurse -Force
            Write-Host "  已删除: $art"
        }
    }
    # 清理 unifypy 回滚缓存
    $rollback = Join-Path $ProjectDir ".unifypy_rollback"
    if (Test-Path $rollback) {
        Get-ChildItem $rollback -Exclude ".gitignore" | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    }
}

# ---------- 调用 UnifyPy 打包 ----------
Write-Step "运行 UnifyPy 打包"

$unifypyArgs = @(
    $ProjectDir,
    "--config", $ConfigFile,
    "--inno-setup-path", $IsccExe
)
if ($Clean)          { $unifypyArgs += "--clean" }
if ($SkipInstaller)  { $unifypyArgs += "--skip-installer" }
if ($SkipExe)        { $unifypyArgs += "--skip-exe" }

& $UnifypyExe @unifypyArgs
$exitCode = $LASTEXITCODE

if ($exitCode -ne 0) {
    Write-Host "`n打包失败，退出码: $exitCode" -ForegroundColor Red
    exit $exitCode
}

# ---------- 输出结果 ----------
Write-Step "打包完成，产物清单"

$distDir    = Join-Path $ProjectDir "dist\ZYAgent"
$installerDir = Join-Path $ProjectDir "installer"

if (Test-Path $distDir) {
    $exe = Join-Path $distDir "ZYAgent.exe"
    if (Test-Path $exe) {
        $size = [math]::Round((Get-Item $exe).Length / 1MB, 1)
        Write-Host "  可执行文件: $exe ($size MB)" -ForegroundColor Green
    }
}

if (-not $SkipInstaller -and (Test-Path $installerDir)) {
    $setup = Get-ChildItem $installerDir -Filter "*.exe" | Select-Object -First 1
    if ($setup) {
        $size = [math]::Round($setup.Length / 1MB, 1)
        Write-Host "  安装包    : $($setup.FullName) ($size MB)" -ForegroundColor Green
    }
}

Write-Host "`n提示：" -ForegroundColor Yellow
Write-Host "  - 修改了代码或 build.json 后，请加 -Clean 参数重新打包"
Write-Host "  - config.ini（含 API Key）已打包进安装包，分发前请注意替换"
Write-Host "  - 如需自定义图标，在 build.json 中添加 icon 字段并准备 .ico 文件"