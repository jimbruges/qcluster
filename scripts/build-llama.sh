#!/usr/bin/env bash
# Builds llama.cpp (with the GGML RPC backend) inside a Debian container, because the
# UNO Q factory image ships no compiler and installing one would need root.
set -euo pipefail

ROOT=/home/arduino/qcluster
SRC="$ROOT/build/llama.cpp"
OUT="$ROOT/runtime"
IMAGE=qcluster/builder:trixie
JOBS="${JOBS:-2}"

test -d "$SRC" || { echo "missing $SRC - clone llama.cpp first" >&2; exit 1; }

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "== building builder image =="
  docker build -t "$IMAGE" -f "$ROOT/scripts/Dockerfile.builder" "$ROOT/scripts"
fi

echo "== compiling llama.cpp (GGML_RPC=ON, -j$JOBS) =="
docker run --rm -v "$SRC:/src" -e JOBS="$JOBS" \
  -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" \
  "$IMAGE" bash -c '
    set -e
    cmake -S /src -B /src/build \
      -DGGML_RPC=ON \
      -DLLAMA_CURL=ON \
      -DGGML_NATIVE=ON \
      -DLLAMA_BUILD_TESTS=OFF \
      -DCMAKE_BUILD_TYPE=Release
    cmake --build /src/build --config Release -j "$JOBS" \
      --target llama-server llama-cli ggml-rpc-server
    chown -R "$HOST_UID:$HOST_GID" /src/build
  '

echo "== staging runtime =="
install -d "$OUT"
install -m 0755 "$SRC/build/bin/llama-server" "$OUT/llama-server"
install -m 0755 "$SRC/build/bin/llama-cli" "$OUT/llama-cli"
# Upstream calls it ggml-rpc-server; keep a stable name for the provisioner.
install -m 0755 "$SRC/build/bin/ggml-rpc-server" "$OUT/rpc-server"
find "$SRC/build/bin" -name '*.so*' -exec install -m 0755 {} "$OUT/" \;

python3 "$ROOT/scripts/make-manifest.py" "$OUT"
echo "== runtime staged in $OUT =="
ls -lh "$OUT"
