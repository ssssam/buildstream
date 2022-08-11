#!/bin/bash

set -eux

wget https://gitlab.com/tristanvb/buildbox-integration/-/releases/permalink/latest/downloads/binaries.tgz

mkdir -p src/buildstream/subprojects/buildbox
tar --extract --file ./binaries.tgz --directory src/buildstream/subprojects/buildbox

cd src/buildstream/subprojects/buildbox
rm buildbox-run
mv buildbox-run-bubblewrap buildbox-run
