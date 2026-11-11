#!/usr/bin/env bash
#
# Build an Android APK from a prepared Ren'Py project.
#
#   bash scripts/build.sh --project build/project --out build/out
#
# The script downloads the official Ren'Py SDK + RAPT (Android support) and
# the Android command line tools, generates a throwaway signing key, and runs
# Ren'Py's `launcher android_build` command.
#
# Everything is cached in .cache/ (override with --cache / $CACHE) so repeated
# builds only re-run Gradle.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

RENPY_VERSION="${RENPY_VERSION:-8.5.3}"
PROJECT=""
OUT=""
CACHE="${CACHE:-$ROOT_DIR/.cache}"
SDK_ZIP=""
RAPT_ZIP=""
BUNDLE=0
KEEP_CACHE=1
GRADLE_HOME="${GRADLE_USER_HOME:-}"

log() { printf '\n==> %s\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

usage() {
    sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
    cat <<'EOF'

Options:
  --project DIR        prepared project directory (contains game/ and android.json)
  --out DIR            directory that receives the APK
  --renpy-version VER  Ren'Py version to build with (default 8.5.3)
  --sdk-zip FILE       use a local renpy-<ver>-sdk.zip instead of downloading
  --rapt-zip FILE      use a local renpy-<ver>-rapt.zip instead of downloading
  --cache DIR          cache directory (default <repo>/.cache)
  --gradle-home DIR    GRADLE_USER_HOME (default <cache>/gradle-home)
  --bundle             also build an .aab bundle
  --keys DIR           where to create/reuse android.keystore + bundle.keystore
                       (default: the project directory)
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --project) PROJECT="$2"; shift 2 ;;
        --out) OUT="$2"; shift 2 ;;
        --renpy-version) RENPY_VERSION="$2"; shift 2 ;;
        --sdk-zip) SDK_ZIP="$2"; shift 2 ;;
        --rapt-zip) RAPT_ZIP="$2"; shift 2 ;;
        --cache) CACHE="$2"; shift 2 ;;
        --gradle-home) GRADLE_HOME="$2"; shift 2 ;;
        --keys) KEYSTORE_DIR="$2"; shift 2 ;;
        --bundle) BUNDLE=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown argument: $1 (see --help)" ;;
    esac
done

[ -n "$PROJECT" ] || die "--project is required"
[ -n "$OUT" ] || die "--out is required"
PROJECT="$(cd "$PROJECT" && pwd)" || die "project directory does not exist"
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"
CACHE="$(mkdir -p "$CACHE" && cd "$CACHE" && pwd)"
GRADLE_HOME="${GRADLE_HOME:-$CACHE/gradle-home}"
KEYSTORE_DIR="${KEYSTORE_DIR:-$PROJECT}"
mkdir -p "$GRADLE_HOME" "$KEYSTORE_DIR"

# --------------------------------------------------------------------------- #
# Java 17-25 (Gradle 9 / AGP 8.13 reject older and newer JDKs)
# --------------------------------------------------------------------------- #
if [ -n "${JAVA_HOME:-}" ]; then
    JAVA="${JAVA_HOME}/bin/java"
else
    JAVA="$(command -v java || true)"
fi
[ -n "$JAVA" ] && [ -x "$JAVA" ] || die "no Java found; install JDK 21 (e.g. 'mise use -g java@21')"
JAVA_VERSION="$("$JAVA" -version 2>&1 | awk -F'"' '/version/ {print $2; exit}')"
JAVA_MAJOR="${JAVA_VERSION%%.*}"
[ "$JAVA_MAJOR" = "1" ] && JAVA_MAJOR="$(echo "$JAVA_VERSION" | cut -d. -f2)"
if [ "$JAVA_MAJOR" -lt 17 ] || [ "$JAVA_MAJOR" -gt 25 ]; then
    die "Java $JAVA_VERSION found, but Gradle 9 needs JDK 17-25 (JDK 21 recommended)"
fi
JAVA_HOME="$(dirname "$(dirname "$(readlink -f "$JAVA")")")"
export JAVA_HOME
log "Using Java $JAVA_VERSION ($JAVA_HOME)"

# --------------------------------------------------------------------------- #
# Ren'Py SDK (official build: it ships lib/python3.12 and the py3 launchers)
# --------------------------------------------------------------------------- #
SDK_DIR="$CACHE/renpy-$RENPY_VERSION"
FETCH="python3 $SCRIPT_DIR/fetch.py"

export FETCH_CONNECTIONS="${FETCH_CONNECTIONS:-32}"

# update.renpy.org serves a certificate that does not cover that host name, so
# plain HTTPS verification fails there.  www.renpy.org carries the same files
# under /dl/ with a valid certificate and Range support, so it is tried first
# and the original host is kept as a plain-HTTP fallback.
RENPY_DL="https://www.renpy.org/dl/$RENPY_VERSION"
RENPY_DL_FALLBACK="http://update.renpy.org/$RENPY_VERSION"

fetch() { # fetch DEST URL [FALLBACK_URL ...]
    local dest="$1" url
    shift
    if [ -s "$dest" ]; then
        log "Using cached $(basename "$dest")"
        return
    fi
    for url in "$@"; do
        log "Downloading $url"
        if $FETCH "$url" "$dest"; then
            return
        fi
        log "Download failed from $url; trying the next source"
        rm -f "$dest"
    done
    die "Could not download $(basename "$dest")"
}

if [ ! -d "$SDK_DIR/renpy" ]; then
    if [ -n "$SDK_ZIP" ]; then
        zip="$SDK_ZIP"
    else
        zip="$CACHE/renpy-$RENPY_VERSION-sdk.zip"
        fetch "$zip" "$RENPY_DL/renpy-$RENPY_VERSION-sdk.zip" "$RENPY_DL_FALLBACK/renpy-$RENPY_VERSION-sdk.zip"
    fi
    unzip -t "$zip" >/dev/null || die "$zip is corrupt (delete it and retry)"
    log "Extracting Ren'Py SDK ..."
    rm -rf "$CACHE/.extract"
    mkdir -p "$CACHE/.extract"
    unzip -q "$zip" -d "$CACHE/.extract"
    root="$(find "$CACHE/.extract" -mindepth 1 -maxdepth 1 -type d | head -n1)"
    rm -rf "$SDK_DIR"
    mv "$root" "$SDK_DIR"
    rm -rf "$CACHE/.extract"
fi
[ -d "$SDK_DIR/renpy" ] || die "Ren'Py SDK not found in $SDK_DIR"

# --------------------------------------------------------------------------- #
# RAPT (Ren'Py Android port tooling)
# --------------------------------------------------------------------------- #
if [ ! -d "$SDK_DIR/rapt/buildlib" ]; then
    if [ -n "$RAPT_ZIP" ]; then
        zip="$RAPT_ZIP"
    else
        zip="$CACHE/renpy-$RENPY_VERSION-rapt.zip"
        if [ ! -s "$zip" ]; then
            # fetch.py falls back to a single stream when the mirror has no
            # range support.
            fetch "$zip" "$RENPY_DL/renpy-$RENPY_VERSION-rapt.zip" "$RENPY_DL_FALLBACK/renpy-$RENPY_VERSION-rapt.zip"
        fi
    fi
    unzip -t "$zip" >/dev/null || die "$zip is corrupt (delete it and retry)"
    log "Extracting RAPT ..."
    unzip -q -o "$zip" -d "$SDK_DIR"
fi
[ -d "$SDK_DIR/rapt/buildlib" ] || die "RAPT not found in $SDK_DIR/rapt"

# --------------------------------------------------------------------------- #
# Android SDK packages required by RAPT (platform-tools + android-36 platform)
# --------------------------------------------------------------------------- #
SDKROOT="$SDK_DIR/rapt/Sdk"
BUILD_TOOLS="$SDKROOT/build-tools/36.0.0"
if [ ! -d "$SDKROOT/platforms/android-36" ] || [ ! -d "$BUILD_TOOLS" ] || [ ! -d "$SDKROOT/platform-tools" ]; then
    if [ ! -x "$SDKROOT/cmdline-tools/latest/bin/sdkmanager" ]; then
        zip="$CACHE/commandlinetools-linux-11076708_latest.zip"
        if [ ! -s "$zip" ]; then
            fetch "$zip" "https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip"
        fi
        log "Installing Android command line tools ..."
        rm -rf "$SDKROOT/cmdline-tools/.tmp"
        mkdir -p "$SDKROOT/cmdline-tools/.tmp"
        unzip -q -o "$zip" -d "$SDKROOT/cmdline-tools/.tmp"
        rm -rf "$SDKROOT/cmdline-tools/latest"
        mv "$SDKROOT/cmdline-tools/.tmp/cmdline-tools" "$SDKROOT/cmdline-tools/latest"
        rm -rf "$SDKROOT/cmdline-tools/.tmp"
    fi

    if [ -n "${https_proxy:-}" ]; then
        proxy_host="${https_proxy#*://}"; proxy_host="${proxy_host%%:*}"
        proxy_port="${https_proxy##*:}"
        export SDKMANAGER_OPTS="-Dhttps.proxyHost=$proxy_host -Dhttps.proxyPort=$proxy_port -Dhttp.proxyHost=$proxy_host -Dhttp.proxyPort=$proxy_port ${SDKMANAGER_OPTS:-}"
        log "Using proxy $proxy_host:$proxy_port for sdkmanager"
    fi

    log "Installing Android SDK packages (platform-tools, android-36, build-tools 36.0.0) ..."
    yes | "$SDKROOT/cmdline-tools/latest/bin/sdkmanager" --sdk_root="$SDKROOT" --licenses >/dev/null 2>&1 || true
    "$SDKROOT/cmdline-tools/latest/bin/sdkmanager" --sdk_root="$SDKROOT" \
        "platform-tools" "platforms;android-36" "build-tools;36.0.0"
fi

# --------------------------------------------------------------------------- #
# Gradle: isolated GRADLE_USER_HOME (avoids a global init.gradle that injects
# repositories and breaks AGP 8.13's settings-repository mode) + wrapper cache
# --------------------------------------------------------------------------- #
if [ -n "${https_proxy:-}" ]; then
    proxy_host="${https_proxy#*://}"; proxy_host="${proxy_host%%:*}"
    proxy_port="${https_proxy##*:}"
    cat > "$GRADLE_HOME/gradle.properties" <<EOF
systemProp.http.proxyHost=$proxy_host
systemProp.http.proxyPort=$proxy_port
systemProp.https.proxyHost=$proxy_host
systemProp.https.proxyPort=$proxy_port
EOF
fi

WRAPPER_PROPS="$SDK_DIR/rapt/prototype/gradle/wrapper/gradle-wrapper.properties"
if [ -f "$WRAPPER_PROPS" ]; then
    DIST_URL="$(grep '^distributionUrl=' "$WRAPPER_PROPS" | cut -d= -f2- | sed 's/\\:/:/g')"
    DIST_NAME="$(basename "$DIST_URL" .zip)"
    DIST_HASH="$(python3 - "$DIST_URL" <<'PY'
import hashlib, sys
n = int.from_bytes(hashlib.md5(sys.argv[1].encode()).digest(), "big")
digits = "0123456789abcdefghijklmnopqrstuvwxyz"
out = ""
while n:
    out = digits[n % 36] + out
    n //= 36
print(out)
PY
)"
    DIST_ZIP="$GRADLE_HOME/wrapper/dists/$DIST_NAME/$DIST_HASH/$DIST_NAME.zip"
    if [ ! -s "$DIST_ZIP" ]; then
        mkdir -p "$(dirname "$DIST_ZIP")"
        log "Pre-downloading Gradle distribution ($DIST_NAME) ..."
        $FETCH "$DIST_URL" "$DIST_ZIP"
        rm -f "$(dirname "$DIST_ZIP")"/*.lck "$(dirname "$DIST_ZIP")"/*.part 2>/dev/null || true
    fi
fi

# --------------------------------------------------------------------------- #
# Signing keys (RAPT hardcodes alias/password "android")
# --------------------------------------------------------------------------- #
if [ ! -f "$KEYSTORE_DIR/android.keystore" ] || [ ! -f "$KEYSTORE_DIR/bundle.keystore" ]; then
    log "Generating throwaway signing keys in $KEYSTORE_DIR"
    rm -f "$KEYSTORE_DIR/android.keystore" "$KEYSTORE_DIR/bundle.keystore"
    keytool -genkeypair -keystore "$KEYSTORE_DIR/android.keystore" \
        -alias android -keyalg RSA -keysize 2048 -validity 20000 \
        -storepass android -keypass android -storetype PKCS12 \
        -dname "CN=ZATO Android Build, O=zato-android-build, C=US"
    cp "$KEYSTORE_DIR/android.keystore" "$KEYSTORE_DIR/bundle.keystore"
fi

# RAPT always looks for the keystores at the project root, so mirror the
# selected key directory into the project before invoking it.
for f in android.keystore bundle.keystore; do
    if [ ! -f "$PROJECT/$f" ] || ! cmp -s "$KEYSTORE_DIR/$f" "$PROJECT/$f"; then
        cp -f "$KEYSTORE_DIR/$f" "$PROJECT/$f"
    fi
done

[ -f "$PROJECT/android.json" ] || die "no android.json in $PROJECT (run scripts/prepare.py first)"

# --------------------------------------------------------------------------- #
# Build
# --------------------------------------------------------------------------- #
ARGS=(launcher android_build "$PROJECT" --destination "$OUT")
[ "$BUNDLE" = "1" ] && ARGS+=(--bundle)

log "Running Ren'Py android_build (Ren'Py $RENPY_VERSION) ..."
rm -f "$OUT"/*.apk "$OUT"/*.aab 2>/dev/null || true
(
    cd "$SDK_DIR"
    GRADLE_USER_HOME="$GRADLE_HOME" \
    SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy \
    ./renpy.sh "${ARGS[@]}"
)

APK="$(ls -1 "$OUT"/*.apk 2>/dev/null | head -n1 || true)"
[ -n "$APK" ] || die "build finished but no APK was produced"
log "APK: $APK ($(du -h "$APK" | cut -f1))"

if [ -x "$BUILD_TOOLS/aapt2" ]; then
    "$BUILD_TOOLS/aapt2" dump badging "$APK" | sed -n \
        -e 's/^package: /package: /p' \
        -e 's/^sdkVersion: /minSdk: /p' \
        -e 's/^targetSdkVersion: /targetSdk: /p' \
        -e 's/^native-code: /abi: /p' | head -n 10
fi
