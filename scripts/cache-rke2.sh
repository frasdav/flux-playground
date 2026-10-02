#!/bin/sh
set -eu

: "${RKE2_VERSION:?}"
: "${RKE2_ARCH:?}"
: "${RKE2_CACHE_DIR:?}"

case "$RKE2_ARCH" in
  arm64) checksums_sha256=4b7f48205a5f1f9b6abbcd84bdf02c1127cad0abc92a388002346fbaae43f43c ;;
  amd64) checksums_sha256=8e12805c4bda79bec2fd20c89f705af3cb2ed11ea8854dc4937fca41b124b57a ;;
  *) echo "Unsupported architecture: $RKE2_ARCH" >&2; exit 1 ;;
esac

installer_sha256=42983c86d1da64a92061d83afb57630cedd69241989f1b0673f3db6c3d92ee6b
release_version=$(printf '%s' "$RKE2_VERSION" | sed 's/+/%2B/g')
release_url="https://github.com/rancher/rke2/releases/download/$release_version"
installer_url="https://raw.githubusercontent.com/rancher/rke2/$release_version/install.sh"
mkdir -p "$RKE2_CACHE_DIR"

sha256() {
  shasum -a 256 "$1" | awk '{ print $1 }'
}

fetch() {
  name=$1
  url=$2
  expected=$3
  destination="$RKE2_CACHE_DIR/$name"
  if [ -f "$destination" ] && [ "$(sha256 "$destination")" = "$expected" ]; then
    echo "Using cached $name"
    return
  fi
  temp="$destination.part"
  echo "Downloading $name"
  curl --fail --location --silent --show-error --retry 3 --output "$temp" "$url"
  if [ "$(sha256 "$temp")" != "$expected" ]; then
    echo "Checksum failed: $name" >&2
    exit 1
  fi
  mv "$temp" "$destination"
}

checksums="sha256sum-$RKE2_ARCH.txt"
fetch install.sh "$installer_url" "$installer_sha256"
fetch "$checksums" "$release_url/$checksums" "$checksums_sha256"

for asset in "rke2.linux-$RKE2_ARCH.tar.gz" "rke2-images.linux-$RKE2_ARCH.tar.zst"; do
  expected=$(awk -v name="$asset" '$2 == name || $2 == "./" name { print $1; exit }' "$RKE2_CACHE_DIR/$checksums")
  if [ -z "$expected" ]; then
    echo "No checksum for $asset" >&2
    exit 1
  fi
  fetch "$asset" "$release_url/$asset" "$expected"
done
