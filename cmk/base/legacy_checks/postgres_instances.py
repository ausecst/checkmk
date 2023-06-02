#!/usr/bin/env python3
# Copyright (C) 2019 Checkmk GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.

# single instance without config
# <<<postgres_instances>>>
# [[[postgres]]]
# 3717 /usr/lib/postgresql83/bin/postgres -D /var/lib/pgsql/data

# single instance with config
# <<<postgres_instances>>>
# 9792 /postgres/9.5.5/bin/postgres -D /postgres/CCENTERE

# multi instances with config
# <<<postgres_instances>>>
# 3960 /usr/lib/postgresql91/bin/postgres -D /var/lib/pgsql/bbtdb -p 5433
# 4149 /usr/lib/postgresql91/bin/postgres -D /var/lib/pgsql/conftdb -p 5434
# 16400 /usr/lib/postgresql91/bin/postgres -D /postgres/jiratdb


# mypy: disable-error-code="var-annotated"

from cmk.base.check_api import discover, LegacyCheckDefinition
from cmk.base.config import check_info


def check_postgres_instances(item, _no_params, parsed):
    pid = parsed.get(item)
    if pid is not None:
        return 0, "Status: running with PID %s" % pid
    return (
        2,
        "Instance %s not running or postgres DATADIR name is not identical with instance name."
        % item,
    )


check_info["postgres_instances"] = LegacyCheckDefinition(
    discovery_function=discover(),
    check_function=check_postgres_instances,
    service_name="PostgreSQL Instance %s",
)
