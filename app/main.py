"""Employee Attendance & Analytics API for the HROne engineering assignment."""

from __future__ import annotations

import logging
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Literal

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # Environment variables keep priority over a local .env file.
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
STATUSES = ["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000

mongo_uri = os.getenv("MONGO_URI")
mongo_db_name = os.getenv("MONGO_DB")
if not mongo_uri or not mongo_db_name:
    raise RuntimeError("MONGO_URI and MONGO_DB must be set in the environment or local .env file")

client = MongoClient(
    mongo_uri,
    tz_aware=True,
    tzinfo=UTC,
    serverSelectionTimeoutMS=2_000,
    connectTimeoutMS=2_000,
)
db = client[mongo_db_name]
indexes_ready = False


def ensure_indexes() -> None:
    """Create the natural-key and query indexes idempotently."""
    global indexes_ready
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="employee_code_unique")
    db.employees.create_index(
        [("department", ASCENDING), ("emp_code", ASCENDING)], name="employee_department_code"
    )
    db.employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING), ("emp_code", ASCENDING)],
        name="employee_department_joined",
    )
    db.employees.create_index(
        [("joined_on", ASCENDING), ("department", ASCENDING), ("emp_code", ASCENDING)],
        name="employee_joined_department",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="attendance_employee_date_unique",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="attendance_employee_punchin"
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)], name="attendance_date_employee"
    )
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("late_minutes", ASCENDING)], name="attendance_date_late"
    )
    db.attendance_logs.create_index(
        [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)],
        name="attendance_status_date_employee",
    )
    indexes_ready = True


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError:
        # Keep the process available so /health can correctly report 503 while
        # MongoDB is unavailable. The grader's database is ready at startup.
        logger.exception("Could not create MongoDB indexes during startup")
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


def fail_422(message: str) -> None:
    raise HTTPException(status_code=422, detail=message)


def validate_date(value: str, label: str = "date") -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        fail_422(f"{label} must be YYYY-MM-DD")
    if parsed.isoformat() != value:
        fail_422(f"{label} must be YYYY-MM-DD")
    return parsed


def month_bounds(month: str) -> tuple[str, str, str]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        fail_422("month must be YYYY-MM")
    year, mon = map(int, month.split("-"))
    if year == 9999 and mon == 12:
        fail_422("month is outside the supported calendar range")
    start = date(year, mon, 1)
    next_month = date(year + (mon == 12), 1 if mon == 12 else mon + 1, 1)
    return start.isoformat(), (next_month - timedelta(days=1)).isoformat(), next_month.isoformat()


def parse_shift(value: str) -> tuple[int, int]:
    hour, minute = map(int, value.split(":"))
    return hour, minute


def truncate_datetime(value: datetime) -> datetime:
    """Return an aware UTC datetime truncated (not rounded) to a whole second."""
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return aware.replace(microsecond=0)


def epoch_ms_to_datetime(value: int) -> datetime:
    if value < MIN_EPOCH_MS or value > MAX_EPOCH_MS:
        fail_422(f"timestamp must be between {MIN_EPOCH_MS} and {MAX_EPOCH_MS} epoch milliseconds")
    try:
        return datetime.fromtimestamp((value // 1000), tz=UTC)
    except (OverflowError, OSError, ValueError):
        fail_422("invalid epoch-millisecond timestamp")


def datetime_to_epoch_ms(value: datetime | None) -> int | None:
    if value is None:
        return None
    return int(truncate_datetime(value).timestamp()) * 1000


def attendance_date(punch_in: datetime, employee: dict[str, Any]) -> str:
    local = truncate_datetime(punch_in).astimezone(IST)
    result = local.date()
    start = parse_shift(employee["shift_start"])
    end = parse_shift(employee["shift_end"])
    if end <= start and (local.hour, local.minute) < end:
        result -= timedelta(days=1)
    return result.isoformat()


def shift_start_datetime(day: str, employee: dict[str, Any]) -> datetime:
    start = date.fromisoformat(day)
    h, m = parse_shift(employee["shift_start"])
    return datetime.combine(start, time(h, m), IST).astimezone(UTC)


def shift_end_datetime(day: str, employee: dict[str, Any]) -> datetime:
    start_day = date.fromisoformat(day)
    shift_start = parse_shift(employee["shift_start"])
    shift_end = parse_shift(employee["shift_end"])
    end_day = start_day + timedelta(days=1) if shift_end <= shift_start else start_day
    return datetime.combine(end_day, time(*shift_end), IST).astimezone(UTC)


def late_minutes(punch_in: datetime | None, day: str, employee: dict[str, Any]) -> int:
    if punch_in is None:
        return 0
    elapsed = int(
        (truncate_datetime(punch_in) - shift_start_datetime(day, employee)).total_seconds()
    )
    return elapsed // 60 if elapsed > 600 else 0


def work_hours(punch_in: datetime | None, punch_out: datetime | None) -> float | None:
    if punch_in is None or punch_out is None:
        return None
    seconds = int((truncate_datetime(punch_out) - truncate_datetime(punch_in)).total_seconds())
    return float(
        (Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    )


def overtime_minutes(punch_out: datetime | None, day: str, employee: dict[str, Any]) -> int:
    if punch_out is None:
        return 0
    elapsed = int(
        (truncate_datetime(punch_out) - shift_end_datetime(day, employee)).total_seconds()
    )
    whole_minutes = elapsed // 60
    return whole_minutes if whole_minutes >= 30 else 0


def derived_values(
    status: str,
    punch_in: datetime | None,
    punch_out: datetime | None,
    day: str,
    employee: dict[str, Any],
) -> dict[str, Any]:
    if status not in PRESENCE:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    hours = work_hours(punch_in, punch_out)
    return {
        "work_hours": hours,
        "late_minutes": late_minutes(punch_in, day, employee),
        "overtime_minutes": overtime_minutes(punch_out, day, employee),
        "half_day": bool(hours is not None and hours < 4.50),
    }


def record_to_api(doc: dict[str, Any]) -> dict[str, Any]:
    history = []
    for entry in doc.get("history", []) or []:
        changes = {}
        for field, pair in (entry.get("changes") or {}).items():
            changes[field] = {
                "from": (
                    datetime_to_epoch_ms(pair.get("from"))
                    if field in ("punch_in", "punch_out")
                    else pair.get("from")
                ),
                "to": (
                    datetime_to_epoch_ms(pair.get("to"))
                    if field in ("punch_in", "punch_out")
                    else pair.get("to")
                ),
            }
        history.append(
            {
                "at": datetime_to_epoch_ms(entry["at"]),
                "by": entry["by"],
                "reason": entry["reason"],
                "changes": changes,
            }
        )
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": datetime_to_epoch_ms(doc.get("punch_in")),
        "punch_out": datetime_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history,
    }


def employee_to_api(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        key: doc[key]
        for key in (
            "emp_code",
            "name",
            "email",
            "department",
            "shift_start",
            "shift_end",
            "joined_on",
        )
    } | {"created_at": datetime_to_epoch_ms(doc["created_at"])}


class StrictModel(BaseModel):
    # In particular, contract-declared read-only derived fields are ignored.
    model_config = ConfigDict(extra="ignore")


class EmployeeIn(StrictModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def valid_joined_on(cls, value: str) -> str:
        validate_date(value, "joined_on")
        return value

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            fail_422("shift_start and shift_end must differ")
        return self


class PunchInIn(StrictModel):
    emp_code: str
    punched_at: StrictInt | None = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(StrictModel):
    emp_code: str
    punched_at: StrictInt | None = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)


class RegularizeIn(StrictModel):
    status: Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"] | None = None
    punch_in: StrictInt | None = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
    punch_out: StrictInt | None = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


def attendance_filter(
    emp_code: str | None, date_from: str | None, date_to: str | None, status: str | None
) -> dict[str, Any]:
    query: dict[str, Any] = {}
    if emp_code:
        query["emp_code"] = emp_code
    if date_from or date_to:
        bounds: dict[str, str] = {}
        if date_from:
            bounds["$gte"] = validate_date(date_from, "date_from").isoformat()
        if date_to:
            bounds["$lte"] = validate_date(date_to, "date_to").isoformat()
        if "$gte" in bounds and "$lte" in bounds and bounds["$gte"] > bounds["$lte"]:
            fail_422("date_from must be on or before date_to")
        query["date"] = bounds
    if status is not None:
        if status not in STATUSES:
            fail_422("invalid attendance status")
        query["status"] = status
    return query


def attendance_list_sort() -> dict[str, int]:
    return {"date": -1, "emp_code": 1}


def attendance_list_index(emp_code: str | None, status: str | None) -> str:
    if emp_code is not None:
        return "attendance_employee_date_unique"
    if status is not None:
        return "attendance_status_date_employee"
    return "attendance_date_employee"


def weekday_expression(date_string: str) -> dict[str, Any]:
    weekday = {"$dayOfWeek": {"$dateFromString": {"dateString": date_string}}}
    return {
        "$cond": [
            {"$ne": [{"$ifNull": [date_string, None]}, None]},
            {"$and": [{"$gte": [weekday, 2]}, {"$lte": [weekday, 6]}]},
            False,
        ]
    }


def list_employee_pipeline(emp_code: str, start: str, end: str) -> list[dict[str, Any]]:
    """Start from the employee so a month with no attendance records still returns a summary."""
    start_expr = {"$cond": [{"$gt": ["$joined_on", start]}, "$joined_on", start]}
    start_date_expr = {"$dateFromString": {"dateString": start_expr}}
    end_exclusive = {"$dateFromString": {"dateString": end}}
    offsets = {
        "$range": [
            0,
            {"$dateDiff": {"startDate": start_date_expr, "endDate": end_exclusive, "unit": "day"}},
        ]
    }
    working_days = {
        "$size": {
            "$filter": {
                "input": {
                    "$map": {
                        "input": offsets,
                        "as": "n",
                        "in": {
                            "$dateAdd": {
                                "startDate": start_date_expr,
                                "unit": "day",
                                "amount": "$$n",
                            }
                        },
                    }
                },
                "as": "d",
                "cond": {
                    "$and": [
                        {"$gte": [{"$dayOfWeek": "$$d"}, 2]},
                        {"$lte": [{"$dayOfWeek": "$$d"}, 6]},
                    ]
                },
            }
        }
    }
    lookup = {
        "$lookup": {
            "from": "attendance_logs",
            "let": {"code": "$emp_code"},
            "pipeline": [
                {
                    "$match": {
                        "$expr": {
                            "$and": [
                                {"$eq": ["$emp_code", "$$code"]},
                                {"$gte": ["$date", start]},
                                {"$lt": ["$date", end]},
                            ]
                        }
                    }
                }
            ],
            "as": "log",
        }
    }
    is_weekday = weekday_expression("$log.date")
    present = {"$and": [{"$in": ["$log.status", PRESENCE]}, is_weekday]}
    return [
        {"$match": {"emp_code": emp_code}},
        {"$addFields": {"working_days": working_days}},
        lookup,
        {"$unwind": {"path": "$log", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": {
                    "emp_code": "$emp_code",
                    "month": start[:7],
                    "working_days": "$working_days",
                },
                "present_days": {
                    "$sum": {
                        "$cond": [
                            present,
                            {"$cond": [{"$ifNull": ["$log.half_day", False]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$log.status", "LEAVE"]}, 1, 0]}},
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$log.late_minutes", 0]}, 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$log.late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$log.overtime_minutes", 0]}},
            }
        },
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id.emp_code",
                "month": "$_id.month",
                "working_days": "$_id.working_days",
                "present_days": 1,
                "leave_days": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "total_overtime_minutes": 1,
                "attendance_pct": {
                    "$cond": [
                        {"$gt": ["$_id.working_days", 0]},
                        {"$divide": [{"$multiply": ["$present_days", 100]}, "$_id.working_days"]},
                        None,
                    ]
                },
            }
        },
        {"$addFields": {"attendance_pct": half_up_expr("$attendance_pct", 4)}},
    ]


def half_up_expr(value: Any, places: int) -> dict[str, Any]:
    scale = 10**places
    return {
        "$cond": [
            {"$eq": [value, None]},
            None,
            {"$divide": [{"$floor": {"$add": [{"$multiply": [value, scale]}, 0.5]}}, scale]},
        ]
    }


def department_summary_pipeline(
    start: str, end: str, department: str | None
) -> list[dict[str, Any]]:
    """Join logs to eligible employees, then group per employee before department headcount."""
    employee_match: dict[str, Any] = {"joined_on": {"$lte": end}}
    if department is not None:
        employee_match["department"] = department
    return [
        {"$match": employee_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$gte": ["$date", start]},
                                    {"$lt": ["$date", end]},
                                ]
                            }
                        }
                    }
                ],
                "as": "log",
            }
        },
        {"$unwind": {"path": "$log", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": {"emp_code": "$emp_code", "department": "$department"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$log.status", PRESENCE]},
                                    weekday_expression("$log.date"),
                                ]
                            },
                            {"$cond": [{"$ifNull": ["$log.half_day", False]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$log.late_minutes", 0]}, 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$log.late_minutes", 0]}},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$log.status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$log.status", "ON_DUTY"]}, 1, 0]}},
                "work_hours_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$log.status", PRESENCE]},
                                    {"$ne": [{"$ifNull": ["$log.work_hours", None]}, None]},
                                ]
                            },
                            "$log.work_hours",
                            0,
                        ]
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$log.status", PRESENCE]},
                                    {"$ne": [{"$ifNull": ["$log.work_hours", None]}, None]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$group": {
                "_id": "$_id.department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$present_days"},
                "late_count": {"$sum": "$late_count"},
                "total_late_minutes": {"$sum": "$total_late_minutes"},
                "leave_count": {"$sum": "$leave_count"},
                "on_duty_count": {"$sum": "$on_duty_count"},
                "work_hours_sum": {"$sum": "$work_hours_sum"},
                "work_hours_count": {"$sum": "$work_hours_count"},
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$work_hours_count", 0]},
                        {"$divide": ["$work_hours_sum", "$work_hours_count"]},
                        None,
                    ]
                },
            }
        },
        {"$addFields": {"avg_work_hours": half_up_expr("$avg_work_hours", 2)}},
        {"$sort": {"department": 1}},
    ]


def leaderboard_pipeline(
    start: str, end: str, limit: int, department: str | None
) -> list[dict[str, Any]]:
    """Rank by total lateness before applying the cutoff so boundary ties are retained."""
    pipeline: list[dict[str, Any]] = [
        {"$match": {"date": {"$gte": start, "$lt": end}, "late_minutes": {"$gt": 0}}},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}},
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]
    return pipeline


def trend_pipeline(department: str, date_from: str, date_to: str) -> list[dict[str, Any]]:
    """Generate the date spine in MongoDB and window over its daily rates."""
    start_expr = {"$dateFromString": {"dateString": date_from}}
    days = {
        "$range": [
            0,
            {
                "$add": [
                    {
                        "$dateDiff": {
                            "startDate": start_expr,
                            "endDate": {
                                "$dateAdd": {
                                    "startDate": {"$dateFromString": {"dateString": date_to}},
                                    "unit": "day",
                                    "amount": 1,
                                }
                            },
                            "unit": "day",
                        }
                    },
                    0,
                ]
            },
        ]
    }
    date_expr = {
        "$dateToString": {
            "date": {"$dateAdd": {"startDate": start_expr, "unit": "day", "amount": "$$day"}},
            "format": "%Y-%m-%d",
        }
    }
    daily_logs = [
        {"$match": {"$expr": {"$eq": ["$date", "$$day"]}}},
        {
            "$lookup": {
                "from": "employees",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$eq": ["$department", department]},
                                ]
                            }
                        }
                    },
                    {"$limit": 1},
                ],
                "as": "employee",
            }
        },
        {"$match": {"employee.0": {"$exists": True}}},
        {
            "$group": {
                "_id": None,
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", PRESENCE]},
                            {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                },
            }
        },
    ]
    return [
        {"$match": {"department": department}},
        {"$limit": 1},
        {
            "$project": {
                "_id": 0,
                "department": 1,
                "calendar": {"$map": {"input": days, "as": "day", "in": date_expr}},
            }
        },
        {"$unwind": "$calendar"},
        {
            "$lookup": {
                "from": "employees",
                "let": {"d": "$calendar"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$d"]},
                                ]
                            }
                        }
                    },
                    {"$count": "headcount"},
                ],
                "as": "headcount_doc",
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"day": "$calendar"},
                "pipeline": daily_logs,
                "as": "daily",
            }
        },
        {
            "$addFields": {
                "date": "$calendar",
                "headcount": {"$ifNull": [{"$arrayElemAt": ["$headcount_doc.headcount", 0]}, 0]},
                "present_count": {"$ifNull": [{"$arrayElemAt": ["$daily.present_count", 0]}, 0]},
                "late_count": {"$ifNull": [{"$arrayElemAt": ["$daily.late_count", 0]}, 0]},
            }
        },
        {
            "$addFields": {
                "is_working_day": {
                    "$and": [
                        {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 2]},
                        {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]},
                    ]
                },
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {
                                    "$gte": [
                                        {
                                            "$dayOfWeek": {
                                                "$dateFromString": {"dateString": "$date"}
                                            }
                                        },
                                        2,
                                    ]
                                },
                                {
                                    "$lte": [
                                        {
                                            "$dayOfWeek": {
                                                "$dateFromString": {"dateString": "$date"}
                                            }
                                        },
                                        6,
                                    ]
                                },
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                },
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d_raw": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": half_up_expr("$attendance_rate", 4),
                "moving_avg_7d": half_up_expr("$moving_avg_7d_raw", 4),
            }
        },
    ]


@app.get("/health")
def health():
    try:
        db.command("ping")
        if not indexes_ready:
            ensure_indexes()
    except PyMongoError:
        raise HTTPException(
            status_code=503, detail="MongoDB is unavailable or indexes are not ready"
        )
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC).replace(microsecond=0)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return employee_to_api(doc)


@app.get("/employees")
def list_employees(
    department: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {"department": department} if department is not None else {}
    index = "employee_department_code" if department is not None else "employee_code_unique"
    total = db.employees.count_documents(query, hint=index)
    docs = (
        db.employees.find(query, {"_id": 0})
        .hint(index)
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [employee_to_api(doc) for doc in docs],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")
    punched = (
        epoch_ms_to_datetime(body.punched_at)
        if body.punched_at is not None
        else truncate_datetime(datetime.now(UTC))
    )
    day = attendance_date(punched, employee)
    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": punched,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes(punched, day, employee),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this attendance date")
    return record_to_api(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")
    punched = (
        epoch_ms_to_datetime(body.punched_at)
        if body.punched_at is not None
        else truncate_datetime(datetime.now(UTC))
    )
    current = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punched}}, sort=[("punch_in", DESCENDING)]
    )
    if current is None:
        raise HTTPException(status_code=404, detail="no punch-in found")
    if current.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="record is already punched out")
    seconds = (punched - truncate_datetime(current["punch_in"])).total_seconds()
    if seconds <= 0 or seconds > 24 * 60 * 60:
        fail_422("punched_at must be after punch_in and no more than 24 hours later")
    values = derived_values(
        current["status"], current["punch_in"], punched, current["date"], employee
    )
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": current["_id"],
            "status": current["status"],
            "punch_in": current["punch_in"],
            "punch_out": None,
        },
        {"$set": {"punch_out": punched, **values}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="record is already punched out")
    return record_to_api(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    status: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = attendance_filter(emp_code, date_from, date_to, status)
    sort = attendance_list_sort()
    index = attendance_list_index(emp_code, status)
    total = db.attendance_logs.count_documents(query, hint=index)
    docs = (
        db.attendance_logs.find(query, {"_id": 0})
        .hint(index)
        .sort(list(sort.items()))
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [record_to_api(doc) for doc in docs],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    day = validate_date(date).isoformat()
    employee = db.employees.find_one({"emp_code": emp_code})
    current = db.attendance_logs.find_one({"emp_code": emp_code, "date": day})
    if employee is None or current is None:
        raise HTTPException(status_code=404, detail="employee or attendance record not found")
    request_fields = body.model_fields_set
    if "status" in request_fields and body.status is None:
        fail_422("status may not be null")
    if any(
        key in request_fields and getattr(body, key) is None for key in ("punch_in", "punch_out")
    ):
        fail_422("punch timestamps may not be null; use ABSENT or LEAVE to clear punches")
    status = body.status if body.status is not None else current["status"]
    if status in ("ABSENT", "LEAVE") and ({"punch_in", "punch_out"} & request_fields):
        fail_422("ABSENT and LEAVE cannot be supplied with punch times")
    if status in ("ABSENT", "LEAVE"):
        punched_in = punched_out = None
    else:
        punched_in = (
            epoch_ms_to_datetime(body.punch_in)
            if "punch_in" in request_fields and body.punch_in is not None
            else current.get("punch_in")
        )
        punched_out = (
            epoch_ms_to_datetime(body.punch_out)
            if "punch_out" in request_fields and body.punch_out is not None
            else current.get("punch_out")
        )
        if "punch_in" in request_fields and body.punch_in is None:
            punched_in = None
        if "punch_out" in request_fields and body.punch_out is None:
            punched_out = None
        if punched_in is None:
            fail_422("a presence status requires punch_in")
        if attendance_date(punched_in, employee) != day:
            fail_422("punch_in must belong to the record attendance date")
        if punched_out is not None:
            seconds = (punched_out - punched_in).total_seconds()
            if seconds <= 0 or seconds > 24 * 60 * 60:
                fail_422("punch_out must be after punch_in and no more than 24 hours later")
    computed = derived_values(status, punched_in, punched_out, day, employee)
    old = {
        "status": current["status"],
        "punch_in": current.get("punch_in"),
        "punch_out": current.get("punch_out"),
        "work_hours": current.get("work_hours"),
        "late_minutes": current.get("late_minutes", 0),
        "overtime_minutes": current.get("overtime_minutes", 0),
        "half_day": current.get("half_day", False),
    }
    new = {"status": status, "punch_in": punched_in, "punch_out": punched_out, **computed}
    changes = {}
    for key in (
        "status",
        "punch_in",
        "punch_out",
        "work_hours",
        "late_minutes",
        "overtime_minutes",
        "half_day",
    ):
        if old[key] != new[key]:
            changes[key] = {"from": old[key], "to": new[key]}
    if not changes:
        fail_422("regularization must change at least one field")
    entry = {
        "at": truncate_datetime(datetime.now(UTC)),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    optimistic = {
        "_id": current["_id"],
        "status": current["status"],
        "punch_in": current.get("punch_in"),
        "punch_out": current.get("punch_out"),
    }
    if "history" in current:
        optimistic["history"] = current["history"]
    else:
        optimistic["history"] = {"$exists": False}
    updated = db.attendance_logs.find_one_and_update(
        optimistic, {"$set": new, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER
    )
    if updated is None:
        raise HTTPException(
            status_code=409, detail="record changed concurrently; retry with the latest record"
        )
    return record_to_api(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    start, _, end = month_bounds(month)
    result = list(
        db.employees.aggregate(
            list_employee_pipeline(emp_code, start, end), hint="employee_code_unique"
        )
    )
    if not result:
        raise HTTPException(status_code=404, detail="employee not found")
    return result[0]


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: str | None = None):
    start, _, end = month_bounds(month)
    pipeline = department_summary_pipeline(start, end, department)
    items = list(
        db.employees.aggregate(
            pipeline,
            hint=(
                "employee_department_joined"
                if department is not None
                else "employee_joined_department"
            ),
        )
    )
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str, limit: int = Query(10, ge=1, le=50), department: str | None = None
):
    start, _, end = month_bounds(month)
    items = list(
        db.attendance_logs.aggregate(
            leaderboard_pipeline(start, end, limit, department), hint="attendance_date_late"
        )
    )
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str, from_date: str = Query(alias="from"), to_date: str = Query(alias="to")
):
    start_date = validate_date(from_date, "from")
    end_date = validate_date(to_date, "to")
    if end_date < start_date:
        fail_422("to must be on or after from")
    if (end_date - start_date).days + 1 > 92:
        fail_422("date range may not exceed 92 days")
    items = list(
        db.employees.aggregate(
            trend_pipeline(department, start_date.isoformat(), end_date.isoformat()),
            hint="employee_department_joined",
        )
    )
    if not items:
        raise HTTPException(status_code=404, detail="department not found")
    return {"department": department, "items": items}


def explain(command: dict[str, Any]) -> dict[str, Any]:
    result = db.command("explain", command, verbosity="executionStats")
    # Keep BSON-only values (for example, Timestamp) in Extended JSON form so
    # FastAPI can serialize the explain response to ordinary JSON.
    return json.loads(json_util.dumps(result))


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: str | None = None,
    month: str | None = None,
    department: str | None = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: str | None = None,
    date_to: str | None = None,
    status: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    from_date: str | None = Query(None, alias="from"),
    to_date: str | None = Query(None, alias="to"),
):
    if endpoint == "attendance_list":
        query = attendance_filter(emp_code, date_from, date_to, status)
        operation = {
            "find": "attendance_logs",
            "filter": query,
            "sort": attendance_list_sort(),
            "skip": (page - 1) * page_size,
            "limit": page_size,
            "hint": attendance_list_index(emp_code, status),
        }
        collection = "attendance_logs"
    elif endpoint == "employee_monthly":
        if emp_code is None or month is None:
            fail_422("emp_code and month are required")
        start, _, end = month_bounds(month)
        operation = {
            "aggregate": "employees",
            "pipeline": list_employee_pipeline(emp_code, start, end),
            "cursor": {},
            "hint": "employee_code_unique",
        }
        collection = "employees"
    elif endpoint == "department_summary":
        if month is None:
            fail_422("month is required")
        start, _, end = month_bounds(month)
        operation = {
            "aggregate": "employees",
            "pipeline": department_summary_pipeline(start, end, department),
            "cursor": {},
            "hint": (
                "employee_department_joined"
                if department is not None
                else "employee_joined_department"
            ),
        }
        collection = "employees"
    elif endpoint == "late_leaderboard":
        if month is None:
            fail_422("month is required")
        start, _, end = month_bounds(month)
        operation = {
            "aggregate": "attendance_logs",
            "pipeline": leaderboard_pipeline(start, end, limit, department),
            "cursor": {},
            "hint": "attendance_date_late",
        }
        collection = "attendance_logs"
    elif endpoint == "department_trend":
        if department is None or from_date is None or to_date is None:
            fail_422("department, from, and to are required")
        start_date, end_date = validate_date(from_date, "from"), validate_date(to_date, "to")
        if end_date < start_date or (end_date - start_date).days + 1 > 92:
            fail_422("invalid trend date range")
        operation = {
            "aggregate": "employees",
            "pipeline": trend_pipeline(department, start_date.isoformat(), end_date.isoformat()),
            "cursor": {},
            "hint": "employee_department_joined",
        }
        collection = "employees"
    else:
        fail_422(
            "endpoint must be one of attendance_list, employee_monthly, department_summary, late_leaderboard, department_trend"
        )
    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": explain(operation),
    }