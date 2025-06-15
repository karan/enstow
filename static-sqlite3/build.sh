#!/bin/bash

set -euo pipefail

SQLITE_VERSION="$1"
SQLITE_AUTOCONF_URL="https://www.sqlite.org/2025/sqlite-autoconf-${SQLITE_VERSION}.tar.gz"

BUILD_DIR="/tmp/sqlite_build"
TEMP_INSTALL_DIR="/tmp/sqlite_install"

echo "Building sqlite3 version: ${SQLITE_VERSION} from autoconf tarball"
echo "Downloading from: ${SQLITE_AUTOCONF_URL}"

mkdir -p "${BUILD_DIR}"
cd "${BUILD_DIR}"

wget "${SQLITE_AUTOCONF_URL}" -O sqlite-autoconf.tar.gz
tar -xzf sqlite-autoconf.tar.gz

SQLITE_SOURCE_DIR=$(find . -maxdepth 1 -type d -name "sqlite-autoconf*" | head -n 1)

if [ -z "${SQLITE_SOURCE_DIR}" ]; then
    echo "Error: Could not find SQLite autoconf source directory after extraction."
    exit 1
fi

cd "${SQLITE_SOURCE_DIR}"

./configure --prefix="${TEMP_INSTALL_DIR}" \
    --disable-shared \
    --enable-static \
    CFLAGS="-Os -DSQLITE_THREADSAFE=1 -DSQLITE_ENABLE_JSON1 -DSQLITE_ENABLE_RTREE -DSQLITE_ENABLE_FTS5 -DSQLITE_OMIT_LOAD_EXTENSION -DSQLITE_DQS=0 -fPIC -static" \
    LDFLAGS="-static -static-libgcc"

make -j$(nproc)
make install

echo "Verifying static linking of installed binary..."
ldd "${TEMP_INSTALL_DIR}/bin/sqlite3" || true

chmod +x "${TEMP_INSTALL_DIR}/bin/sqlite3"

# Move the binary to a well-known location for Docker COPY
mv "${TEMP_INSTALL_DIR}/bin/sqlite3" /out/sqlite3

echo "sqlite3 binary built and moved to /out/sqlite3"
ls -lah /out/sqlite3