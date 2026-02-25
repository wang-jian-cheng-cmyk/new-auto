#!/usr/bin/env bash
set -euo pipefail

if [ -z "${ANDROID_SDK_ROOT:-}" ] && [ -z "${ANDROID_HOME:-}" ]; then
  echo "ANDROID_SDK_ROOT or ANDROID_HOME is not set."
  echo "Example: export ANDROID_SDK_ROOT=$HOME/Android/Sdk"
  exit 1
fi

SDK_ROOT="${ANDROID_SDK_ROOT:-${ANDROID_HOME:-}}"
AAPT2="$SDK_ROOT/build-tools/34.0.0/aapt2"
if [ -x "$AAPT2" ]; then
  if ! "$AAPT2" version >/dev/null 2>&1; then
    echo "aapt2 cannot run on this host architecture."
    echo "Use Android Studio on a supported host to build APK."
    exit 1
  fi
fi

./gradlew assembleDebug
echo "Debug APK: app/build/outputs/apk/debug/app-debug.apk"
