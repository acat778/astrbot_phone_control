#!/usr/bin/env bash
# SDK 35 / JDK 17+；输出目录由调用方指定到工作区缓存。
set -euo pipefail
: "${ANDROID_HOME:?需指定 Android SDK}"
: "${JAVA_HOME:?需指定 JDK}"
: "${OUT_DIR:?需指定缓存输出目录}"
export PATH="$JAVA_HOME/bin:$PATH"
SOURCE_DIR="$(cd "$(dirname "$0")" && pwd)"
PLATFORM="$ANDROID_HOME/platforms/android-35/android.jar"
mkdir -p "$OUT_DIR/classes" "$OUT_DIR/dex"
javac -source 8 -target 8 -classpath "$PLATFORM" -d "$OUT_DIR/classes" "$SOURCE_DIR/UiCapture.java"
jar cf "$OUT_DIR/classes.jar" -C "$OUT_DIR/classes" .
"$ANDROID_HOME/build-tools/35.0.0/d8" --lib "$PLATFORM" --min-api 26 --output "$OUT_DIR/dex" "$OUT_DIR/classes.jar"
jar cf "$OUT_DIR/phone-ui-capture.jar" -C "$OUT_DIR/dex" classes.dex
