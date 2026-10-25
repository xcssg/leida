# ZUOFANG 一键构建（Windows 11 / PowerShell）
#
# 用法：
#   .\build.ps1
#   .\build.ps1 192.168.1.10:8090
#   .\build.ps1 -ApiHost http://1.2.3.4:8090
#   .\build.ps1 -SkipRust          # 不重编 Rust，直接用仓库里已有的 .so
#
# 首次运行若报「禁止运行脚本」：
#   Set-ExecutionPolicy -Scope Process -Bypass -Force

[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$ApiHost = "10.0.2.2:8090",
    [switch]$SkipRust
)

$ErrorActionPreference = "Stop"

$Root      = Split-Path -Parent $MyInvocation.MyCommand.Path
$Client    = Join-Path $Root "app"
$RustDir   = Join-Path $Client "rust"
$JniDir    = Join-Path $Client "android\app\src\main\jniLibs\arm64-v8a"
$SoFile    = Join-Path $JniDir "libzuofang_native.so"

if ($ApiHost -match '^https?://') { $ZfApi = $ApiHost } else { $ZfApi = "http://$ApiHost" }

function Say($m) { Write-Host ""; Write-Host "==> $m" -ForegroundColor Cyan }
function Die($m) { Write-Host ""; Write-Host "[错误] $m" -ForegroundColor Red; exit 1 }

# ---------- 0. 环境检查 ----------
Say "检查工具链"
foreach ($tool in @("flutter", "cargo", "java")) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        Die "未找到 $tool，请先安装并加入 PATH"
    }
}

$sdk = $env:ANDROID_HOME
if (-not $sdk) { $sdk = $env:ANDROID_SDK_ROOT }
if (-not $sdk) { $sdk = Join-Path $env:LOCALAPPDATA "Android\Sdk" }
if (-not (Test-Path $sdk)) { Die "未找到 Android SDK，请设置 ANDROID_HOME 或安装到 $env:LOCALAPPDATA\Android\Sdk" }
$env:ANDROID_HOME = $sdk
$env:ANDROID_SDK_ROOT = $sdk
Write-Host "ANDROID_HOME = $sdk"

$ndk = Join-Path $sdk "ndk\27.0.12077973"
if (-not (Test-Path $ndk)) {
    $found = Get-ChildItem (Join-Path $sdk "ndk") -Directory -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($found) { $ndk = $found.FullName; Write-Host "警告：未找到 NDK 27.0.12077973，改用 $($found.Name)" -ForegroundColor Yellow }
    else { Die "未找到 NDK，请在 Android Studio 的 SDK Manager 里安装 NDK 27.0.12077973" }
}
$env:ANDROID_NDK_HOME = $ndk
Write-Host "ANDROID_NDK_HOME = $ndk"

flutter --version | Select-Object -First 1
cargo --version
java -version 2>&1 | Select-Object -First 1

# ---------- 1. Rust 交叉编译 ----------
if (-not $SkipRust) {
    Say "准备 Rust Android 目标"
    rustup target add aarch64-linux-android

    if (-not (Get-Command cargo-ndk -ErrorAction SilentlyContinue)) {
        Say "安装 cargo-ndk（需要 MSVC 链接器，见 README 前置条件）"
        cargo install cargo-ndk --locked
    }

    Say "编译 libzuofang_native.so（arm64-v8a）"
    New-Item -ItemType Directory -Force -Path $JniDir | Out-Null
    Push-Location $RustDir
    try {
        cargo ndk -t arm64-v8a -o (Join-Path $Client "android\app\src\main\jniLibs") build --release
    } finally {
        Pop-Location
    }

    if (-not (Test-Path $SoFile)) { Die "交叉编译未产出 libzuofang_native.so" }
} else {
    Say "跳过 Rust 编译，复用已有产物"
    if (-not (Test-Path $SoFile)) { Die "未找到 $SoFile，去掉 -SkipRust 重新编译" }
}
Get-Item $SoFile | ForEach-Object { "{0}  {1:N0} 字节" -f $_.Name, $_.Length }

# ---------- 2. Flutter 依赖 ----------
Say "flutter pub get"
Push-Location $Client
try {
    flutter pub get

    # ---------- 3. 打包 ----------
    Say "构建 APK（ZF_API=$ZfApi）"
    flutter build apk --release --dart-define=ZF_API=$ZfApi
} finally {
    Pop-Location
}

$apk = Join-Path $Client "build\app\outputs\flutter-apk\app-release.apk"
if (-not (Test-Path $apk)) { Die "未找到产物 $apk" }

Write-Host ""
Write-Host "构建完成" -ForegroundColor Green
Write-Host "  产物：$apk"
$mb = [math]::Round((Get-Item $apk).Length / 1MB, 1)
Write-Host "  大小：$mb MB"
Write-Host "  后端：$ZfApi"
Write-Host ""
Write-Host "安装到手机（USB 调试已开）："
Write-Host "  adb install -r `"$apk`""
Write-Host ""
Write-Host "后端地址是编译期写死的。换地址请重新执行本脚本。"
