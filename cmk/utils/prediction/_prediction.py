#!/usr/bin/env python3
# Copyright (C) 2019 Checkmk GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.

import logging
import math
import time
import typing
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from statistics import fmean
from typing import Final, Literal, NamedTuple, NewType

from pydantic import BaseModel

import livestatus

import cmk.utils.debug
import cmk.utils.paths
from cmk.utils import dateutils
from cmk.utils.exceptions import MKGeneralException
from cmk.utils.hostaddress import HostName
from cmk.utils.log import VERBOSE
from cmk.utils.servicename import ServiceName

from ._paths import DATA_FILE_SUFFIX, INFO_FILE_SUFFIX

logger = logging.getLogger("cmk.prediction")

Seconds = int
Timestamp = int

TimeWindow = tuple[Timestamp, Timestamp, Seconds]
RRDColumnFunction = Callable[[Timestamp, Timestamp], "TimeSeries"]
TimeSeriesValue = float | None
TimeSeriesValues = list[TimeSeriesValue]
ConsolidationFunctionName = str
Timegroup = NewType("Timegroup", str)
EstimatedLevel = float | None
EstimatedLevels = tuple[EstimatedLevel, EstimatedLevel, EstimatedLevel, EstimatedLevel]

_PeriodName = Literal["wday", "day", "hour", "minute"]
LevelsSpec = tuple[Literal["absolute", "relative", "stdev"], tuple[float, float]]


class PredictionParameters(BaseModel, frozen=True):
    period: _PeriodName
    horizon: int
    levels_upper: LevelsSpec | None = None
    levels_upper_min: tuple[float, float] | None = None
    levels_lower: LevelsSpec | None = None


class DataStat(typing.NamedTuple):
    average: float
    min_: float
    max_: float
    stdev: float | None


_TimeSlices = list[tuple[Timestamp, Timestamp]]

_GroupByFunction = Callable[[Timestamp], tuple[Timegroup, Timestamp]]


class _PeriodInfo(NamedTuple):
    slice: int
    groupby: _GroupByFunction
    valid: int


class PredictionInfo(BaseModel, frozen=True):
    name: Timegroup
    time: int
    range: tuple[Timestamp, Timestamp]
    cf: ConsolidationFunctionName
    dsname: str
    slice: int
    params: PredictionParameters


class PredictionData(BaseModel, frozen=True):
    points: list[DataStat | None]
    data_twindow: list[Timestamp]
    step: Seconds

    @property
    def num_points(self) -> int:
        return len(self.points)


def is_dst(timestamp: float) -> bool:
    """Check wether a certain time stamp lies with in daylight saving time (DST)"""
    return bool(time.localtime(timestamp).tm_isdst)


def timezone_at(timestamp: float) -> int:
    """Returns the timezone *including* DST shift at a certain point of time"""
    return time.altzone if is_dst(timestamp) else time.timezone


def rrd_timestamps(time_window: TimeWindow) -> list[Timestamp]:
    start, end, step = time_window
    return [] if step == 0 else [t + step for t in range(start, end, step)]


def aggregation_functions(
    series: TimeSeriesValues, aggr: ConsolidationFunctionName | None
) -> TimeSeriesValue:
    """Aggregate data in series list according to aggr

    If series has None values they are dropped before aggregation"""
    cleaned_series = [x for x in series if x is not None]
    if not cleaned_series:
        return None

    aggr = "max" if aggr is None else aggr.lower()
    match aggr:
        case "average":
            return fmean(cleaned_series)
        case "max":
            return max(cleaned_series)
        case "min":
            return min(cleaned_series)
        case _:
            raise ValueError(f"Invalid Aggregation function {aggr}, only max, min, average allowed")


class TimeSeries:
    """Describes the returned time series returned by livestatus

    - Timestamped values are valid for the measurement interval:
        [timestamp-step; timestamp[
      which means they are at the end of the interval.
    - The Series describes the interval [start; end[
    - Start has no associated value to it.

    args:
        data : list
            Includes [start, end, step, *values]
        timewindow: tuple
            describes (start, end, step), in this case data has only values
        conversion:
            optional conversion to account for user-specific unit settings

    """

    def __init__(
        self,
        data: TimeSeriesValues,
        time_window: TimeWindow | None = None,
        conversion: Callable[[float], float] = lambda v: v,
    ) -> None:
        if time_window is None:
            if not data or data[0] is None or data[1] is None or data[2] is None:
                raise ValueError(data)

            time_window = int(data[0]), int(data[1]), int(data[2])
            data = data[3:]

        assert time_window is not None
        self.start = int(time_window[0])
        self.end = int(time_window[1])
        self.step = int(time_window[2])
        self.values = [v if v is None else conversion(v) for v in data]

    @property
    def twindow(self) -> TimeWindow:
        return self.start, self.end, self.step

    def bfill_upsample(self, twindow: TimeWindow) -> TimeSeriesValues:
        """Upsample by backward filling values

        twindow : 3-tuple, (start, end, step)
             description of target time interval
        """
        if twindow == self.twindow:
            return self.values

        upsa = []
        i = 0
        current_times = rrd_timestamps(self.twindow)
        for t in range(*twindow):
            if t >= current_times[i]:
                i += 1
            upsa.append(self.values[i])

        return upsa

    def downsample(
        self, twindow: TimeWindow, cf: ConsolidationFunctionName | None = "max"
    ) -> TimeSeriesValues:
        """Downsample time series by consolidation function

        twindow : 3-tuple, (start, end, step)
             description of target time interval
        cf : str ('max', 'average', 'min')
             consolidation function imitating RRD methods
        """
        if twindow == self.twindow:
            return self.values

        dwsa = []
        co: TimeSeriesValues = []
        desired_times = rrd_timestamps(twindow)
        i = 0
        for t, val in self.time_data_pairs():
            if t > desired_times[i]:
                dwsa.append(aggregation_functions(co, cf))
                co = []
                i += 1
            co.append(val)

        diff_len = len(desired_times) - len(dwsa)
        if diff_len > 0:
            dwsa.append(aggregation_functions(co, cf))
            dwsa += [None] * (diff_len - 1)

        return dwsa

    def time_data_pairs(self) -> list[tuple[Timestamp, TimeSeriesValue]]:
        return list(zip(rrd_timestamps(self.twindow), self.values))

    def __repr__(self) -> str:
        return f"TimeSeries({self.values}, timewindow={self.twindow})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, TimeSeries):
            return NotImplemented

        return (
            self.start == other.start
            and self.end == other.end
            and self.step == other.step
            and self.values == other.values
        )

    def __getitem__(self, i: int) -> TimeSeriesValue:
        return self.values[i]

    def __len__(self) -> int:
        return len(self.values)

    def __iter__(self) -> Iterator[TimeSeriesValue]:
        yield from self.values

    def count(self, /, v: TimeSeriesValue) -> int:
        return self.values.count(v)


def get_rrd_data_with_mk_general_exception(
    host_name: HostName,
    service_description: str,
    metric_name: str,
    cf: ConsolidationFunctionName,
    fromtime: Timestamp,
    untiltime: Timestamp,
) -> livestatus.RRDResponse:
    """Wrapper to raise MKGeneralException."""
    try:
        response = livestatus.get_rrd_data(
            livestatus.LocalConnection(),
            host_name,
            service_description,
            f"{metric_name}.{cf.lower()}",
            fromtime,
            untiltime,
        )
    except livestatus.MKLivestatusNotFoundError as e:
        if cmk.utils.debug.enabled():
            raise
        raise MKGeneralException(f"Cannot get historic metrics via Livestatus: {e}")

    if response is None:
        raise MKGeneralException("Cannot retrieve historic data with Nagios Core")

    return response


class PredictionStore:
    def __init__(
        self,
        basedir: Path,
        host_name: HostName,
        service_description: ServiceName,
        dsname: str,
    ) -> None:
        self._dir = (
            basedir
            / host_name
            / cmk.utils.pnp_cleanup(service_description)
            / cmk.utils.pnp_cleanup(dsname)
        )

    def _data_file(self, timegroup: Timegroup) -> Path:
        return self._dir / Path(timegroup).with_suffix(DATA_FILE_SUFFIX)

    def _info_file(self, timegroup: Timegroup) -> Path:
        return self._dir / Path(timegroup).with_suffix(INFO_FILE_SUFFIX)

    def save_prediction(
        self,
        info: PredictionInfo,
        data: PredictionData,
    ) -> None:
        self._dir.mkdir(exist_ok=True, parents=True)
        self._info_file(info.name).write_text(info.json())
        self._data_file(info.name).write_text(data.json())

    def remove_prediction(self, timegroup: Timegroup) -> None:
        self._data_file(timegroup).unlink(missing_ok=True)
        self._info_file(timegroup).unlink(missing_ok=True)

    def get_info(self, timegroup: Timegroup) -> PredictionInfo | None:
        file_path = self._info_file(timegroup)
        try:
            return PredictionInfo.parse_raw(file_path.read_text())
        except FileNotFoundError:
            logger.log(VERBOSE, "No prediction info for group %s available.", timegroup)
        return None

    def get_data(self, timegroup: Timegroup) -> PredictionData | None:
        file_path = self._data_file(timegroup)
        try:
            return PredictionData.parse_raw(file_path.read_text())
        except FileNotFoundError:
            logger.log(VERBOSE, "No prediction for group %s available.", timegroup)
        return None


def compute_prediction(
    info: PredictionInfo,
    prediction_store: PredictionStore,
    now: int,
    period_info: _PeriodInfo,
    host_name: HostName,
    service_description: ServiceName,
) -> PredictionData:
    logger.log(VERBOSE, "Calculating prediction data for time group %s", info.name)
    prediction_store.remove_prediction(info.name)

    time_windows = _time_slices(now, info.params.horizon * 86400, period_info, info.name)

    from_time = time_windows[0][0]
    rrd_responses = [
        (
            get_rrd_data_with_mk_general_exception(
                host_name,
                service_description,
                info.dsname,
                info.cf,
                start,
                end,
            ),
            from_time - start,
        )
        for start, end in time_windows
    ]

    raw_slices = [
        (
            TimeSeries(
                list(rrd_response.values),
                (rrd_response.window.start, rrd_response.window.stop, rrd_response.window.step),
            ),
            offset,
        )
        for rrd_response, offset in rrd_responses
    ]

    data_for_pred = _calculate_data_for_prediction(raw_slices)

    prediction_store.save_prediction(info, data_for_pred)

    return data_for_pred


def _window_start(timestamp: int, span: int) -> int:
    """If time is partitioned in SPAN intervals, how many seconds is TIMESTAMP away from the start

    It works well across time zones, but has an unfair behavior with daylight savings time."""
    return (timestamp - timezone_at(timestamp)) % span


def _group_by_wday(t: Timestamp) -> tuple[Timegroup, Timestamp]:
    wday = time.localtime(t).tm_wday
    return Timegroup(dateutils.weekday_ids()[wday]), _window_start(t, 86400)


def _group_by_day(t: Timestamp) -> tuple[Timegroup, Timestamp]:
    return Timegroup("everyday"), _window_start(t, 86400)


def _group_by_day_of_month(t: Timestamp) -> tuple[Timegroup, Timestamp]:
    mday = time.localtime(t).tm_mday
    return Timegroup(str(mday)), _window_start(t, 86400)


def _group_by_everyhour(t: Timestamp) -> tuple[Timegroup, Timestamp]:
    return Timegroup("everyhour"), _window_start(t, 3600)


PREDICTION_PERIODS: Final[Mapping[_PeriodName, _PeriodInfo]] = {
    "wday": _PeriodInfo(
        slice=86400,  # 7 slices
        groupby=_group_by_wday,
        valid=7,
    ),
    "day": _PeriodInfo(
        slice=86400,  # 31 slices
        groupby=_group_by_day_of_month,
        valid=28,
    ),
    "hour": _PeriodInfo(
        slice=86400,  # 1 slice
        groupby=_group_by_day,
        valid=1,
    ),
    "minute": _PeriodInfo(
        slice=3600,  # 1 slice
        groupby=_group_by_everyhour,
        valid=24,
    ),
}


def _time_slices(
    timestamp: Timestamp,
    horizon: Seconds,
    period_info: _PeriodInfo,
    timegroup: Timegroup,
) -> _TimeSlices:
    "Collect all slices back into the past until time horizon is reached"
    timestamp = int(timestamp)
    abs_begin = timestamp - horizon
    slices = []

    # Note: due to the f**king DST, we can have several shifts between DST
    # and non-DST during a computation. Treatment is unfair on those longer
    # or shorter days. All days have 24hrs. DST swaps within slices are
    # being ignored, we work with slice shifts. The DST flag is checked
    # against the query timestamp. In general that means test is done at
    # the beginning of the day(because predictive levels refresh at
    # midnight) and most likely before DST swap is applied.

    # Have fun understanding the tests for this function.
    for begin in range(timestamp, abs_begin, -period_info.slice):
        tg, start, end = get_timegroup_relative_time(begin, period_info)[:3]
        if tg == timegroup:
            slices.append((start, end))
    return slices


def get_timegroup_relative_time(
    t: Timestamp,
    period_info: _PeriodInfo,
) -> tuple[Timegroup, Timestamp, Timestamp, Seconds]:
    """
    Return:
    timegroup: name of the group, like 'monday' or '12'
    from_time: absolute epoch time of the first second of the
    current slice.
    until_time: absolute epoch time of the first second *not* in the slice
    rel_time: seconds offset of now in the current slice
    """
    # Convert to local timezone
    timegroup, rel_time = period_info.groupby(t)
    from_time = t - rel_time
    until_time = from_time + period_info.slice
    return timegroup, from_time, until_time, rel_time


def _calculate_data_for_prediction(
    raw_slices: Sequence[tuple[TimeSeries, int]],
) -> PredictionData:
    twindow, slices = _upsample(raw_slices)

    return PredictionData(
        points=_data_stats(slices),
        data_twindow=list(twindow[:2]),
        step=twindow[2],
    )


def _data_stats(slices: list[TimeSeriesValues]) -> list[DataStat | None]:
    "Statistically summarize all the upsampled RRD data"

    descriptors: list[DataStat | None] = []

    for time_column in zip(*slices):
        point_line = [x for x in time_column if x is not None]
        if point_line:
            average = sum(point_line) / float(len(point_line))
            descriptors.append(
                DataStat(
                    average=average,
                    min_=min(point_line),
                    max_=max(point_line),
                    stdev=_std_dev(point_line, average),
                )
            )
        else:
            descriptors.append(None)

    return descriptors


def _std_dev(point_line: list[float], average: float) -> float | None:
    samples = len(point_line)
    # In the case of a single data-point an unbiased standard deviation is undefined.
    if samples == 1:
        return None
    return math.sqrt(
        abs(sum(p**2 for p in point_line) - average**2 * samples) / float(samples - 1)
    )


def _upsample(
    slices: Sequence[tuple[TimeSeries, int]]
) -> tuple[TimeWindow, list[TimeSeriesValues]]:
    """Upsample all time slices to same resolution

    The resolutions of the different time ranges differ. We upsample
    to the best resolution. We assume that the youngest slice has the
    finest resolution.
    """
    twindow = slices[0][0].twindow
    return twindow, [
        ts.bfill_upsample((twindow[0] - shift, twindow[1] - shift, twindow[2]))
        for ts, shift in slices
    ]
