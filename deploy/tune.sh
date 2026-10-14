#!/usr/bin/env bash
# Підбір налаштувань моделі під свою відеокарту (див. deploy/tune.py --help).
exec python3 "$(dirname "${BASH_SOURCE[0]}")/tune.py" "$@"
