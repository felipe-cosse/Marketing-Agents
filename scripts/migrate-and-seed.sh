#!/bin/sh
# DEL-05: a single startup owner; existing paired key is never replaced.
set -eu
python -m marketing_agents.workers.database_cli migrate
python -m marketing_agents.workers.database_cli seed
python -m marketing_agents.workers.database_cli seed --check
