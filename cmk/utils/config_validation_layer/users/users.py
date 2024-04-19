#!/usr/bin/env python3
# Copyright (C) 2024 Checkmk GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.

from typing import Literal

from pydantic import BaseModel, RootModel, ValidationError

from cmk.utils.config_validation_layer.type_defs import OMITTED_FIELD
from cmk.utils.config_validation_layer.validation_utils import ConfigValidationError

TEMPERATURE = Literal["celsius", "fahrenheit"]

SHOW_MODE = Literal["default_show_less", "default_show_more", "enforce_show_more"]


class User(BaseModel):
    alias: str = OMITTED_FIELD
    connector: str = OMITTED_FIELD
    locked: bool = OMITTED_FIELD
    roles: list[str] = OMITTED_FIELD
    temperature_unit: TEMPERATURE | None = OMITTED_FIELD
    force_authuser: bool = OMITTED_FIELD
    nav_hide_icons_title: Literal["hide"] | None = OMITTED_FIELD
    icons_per_item: Literal["entry"] | None = OMITTED_FIELD
    show_mode: SHOW_MODE | None = OMITTED_FIELD
    automation_secret: str = OMITTED_FIELD
    language: str = OMITTED_FIELD


UserMapModel = RootModel[dict[str, User]]


def validate_users(users: dict) -> None:
    try:
        UserMapModel(users)

    except ValidationError as exc:
        raise ConfigValidationError(
            which_file="users.mk",
            pydantic_error=exc,
            original_data=users,
        )
