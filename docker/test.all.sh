#!/usr/bin/env bash

set -e

./docker/rebuild6.2.sh
./docker/up6.2.sh
if ! ./docker/ready6.2.sh; then
  ./docker/down6.2.sh
  exit 1
fi
./docker/test.suite.sh suites/test_suite_tx_subscribe.err.json
./docker/fetch.logs.sh
./docker/down6.2.sh

python3 harness/combine.results.py
