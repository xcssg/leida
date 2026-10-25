#!/usr/bin/env bash
# ZUOFANG 一键构建：Rust 交叉编译 -> Flutter APK
#
# 用法：
#   ./build.sh                        # 后端地址用默认 10.0.2.2:8090（Android 模拟器）
#   ./build.sh 192.168.1.10:8090      # 真机：填后端所在机器的局域网/公网 IP
#   ZF_API=http://1.2.3.4:8090 ./build.sh
#
# 前置：Flutter 3.24+、Android SDK（platform 35 + NDK 27 + build-tools）、Rust stable、JDK 17
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
CLIENT="$ROOT/app"
RUST_DIR="$CLIENT/rust"
JNI_DIR="$CLIENT/android/app/src/main/jniLibs/arm64-v8a"

API_HOST="${1:-10.0.2.2:8090}"
case "$API_HOST" in
  http://*|https://*) ZF_API="$API_HOST" ;;
  *)                  ZF_API="http://$API_HOST" ;;
esac

say() { printf '\n\033[36m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[31m[错误] %s\033[0m\n' "$*" >&2; exit 1; }

# ---------- 0. 环境检查 ----------
say "检查工具链"
command -v flutter >/dev/null || die "未找到 flutter，请先安装 Flutter SDK 并加入 PATH"
command -v cargo   >/dev/null || die "未找到 cargo，请先安装 Rust（https://rustup.rs）"
command -v java    >/dev/null || die "未找到 java，请安装 JDK 17"
[ -n "${ANDROID_HOME:-}${ANDROID_SDK_ROOT:-}" ] || die "未设置 ANDROID_HOME / ANDROID_SDK_ROOT"

flutter --version | head -2
rustc --version
java -version 2>&1 | head -1

# ---------- 1. Rust 交叉编译 ----------
say "准备 Rust Android 目标"
rustup target add aarch64-linux-android

if ! command -v cargo-ndk >/dev/null; then
  say "安装 cargo-ndk"
  cargo install cargo-ndk --locked
fi

say "编译 libzuofang_native.so（arm64-v8a）"
mkdir -p "$JNI_DIR"
cd "$RUST_DIR"
cargo ndk -t arm64-v8a -o "$CLIENT/android/app/src/main/jniLibs" build --release

[ -f "$JNI_DIR/libzuofang_native.so" ] || die "交叉编译未产出 libzuofang_native.so"
ls -lh "$JNI_DIR/libzuofang_native.so"

# ---------- 2. Flutter 依赖 ----------
say "flutter pub get"
cd "$CLIENT"
flutter pub get

# ---------- 3. 打包 ----------
say "构建 APK（ZF_API=$ZF_API）"
chmod +x android/gradlew 2>/dev/null || true
flutter build apk --release --dart-define=ZF_API="$ZF_API"

APK="$CLIENT/build/app/outputs/flutter-apk/app-release.apk"
[ -f "$APK" ] || die "未找到产物 $APK"
ls -lh "$APK"

cat <<EOF

构建完成。

  产物：$APK
  后端：$ZF_API

安装到手机（USB 调试已开）：
  adb install -r "$APK"

如果换了后端地址，重新执行本脚本并传入新地址即可，APK 内的地址是编译期写死的。
EOF
