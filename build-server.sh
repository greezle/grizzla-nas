#!/bin/sh
# Build the deployable single-file server (a Python zipapp) from the
# awaria/ package. The output runs exactly like the old monolith did
# (ExecStart=/usr/bin/python3 /usr/local/bin/awaria-server), so the
# systemd unit and the scp-one-file deploy workflow are unchanged.
set -eu
cd "$(dirname "$0")"
rm -rf build/stage
mkdir -p build/stage
cp -r awaria build/stage/
find build/stage -name '__pycache__' -type d -exec rm -rf {} +
python3 -m zipapp build/stage -m "awaria.main:main" -o build/awaria-server
echo "built build/awaria-server ($(wc -c < build/awaria-server) bytes)"
