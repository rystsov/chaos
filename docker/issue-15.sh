#!/usr/bin/env bash

set -e

./docker/rebuild6.2.sh
./docker/up6.2.sh
if ! ./docker/ready6.2.sh; then
  ./docker/down6.2.sh
  exit 1
fi
# each test lasts ~5 min so 288 runs ~ 24h
./docker/test.test.sh suites/tests/tx_subscribe/pause_group_leader.json 288
./docker/fetch.logs.sh
#we don't bring the images down we cause we need an active cluster to debug
#./docker/down6.2.sh

python3 harness/combine.results.py
