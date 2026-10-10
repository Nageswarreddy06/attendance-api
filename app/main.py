"""Employee Attendance & Analytics API.

Run from the repository root with: python -m uvicorn app.main:app --reload --port 8000
Configuration: MONGO_URI and MONGO_DB in the environment or this folder's .env file.
"""
from __future__ import annotations

import os
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Annotated, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path as ApiPath, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env", override=False)
MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
if not MONGO_URI:
    raise RuntimeError(
        f"MONGO_URI is missing. Create {PROJECT_ROOT / '.env'} with "
        "MONGO_URI=<Atlas connection string> and MONGO_DB=attendance_db."
    )

client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=10000, tz_aware=True)
db = client[MONGO_DB]
employees = db.employees
attendance = db.attendance_logs
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENT_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
ALL_STATUSES = ("PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE")
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000
EpochMillis = Annotated[StrictInt, Field(ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)]

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# ---------------------------------------------------------------------------
# Startup indexes. They support both the API's filters/sorts and race safety.
# ---------------------------------------------------------------------------
@app.on_event("startup")
def create_indexes() -> None:
    employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_emp_code")
    employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="ix_employee_department_code")
    employees.create_index([("joined_on", ASCENDING)], name="ix_employee_joined_on")
    employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="ix_employee_department_joined")

    # Unique natural key enforces exactly one record per employee attendance-day,
    # including when simultaneous requests race past any application-level check.
    attendance.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_emp_date")
    attendance.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_code")
    attendance.create_index([("emp_code", ASCENDING), ("date", DESCENDING)], name="ix_attendance_employee_date")
    attendance.create_index([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_status_date_code")
    attendance.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="ix_attendance_employee_punchin")


# ---------------------------------------------------------------------------
# Validation / date-time helpers
# ---------------------------------------------------------------------------
def _parse_iso_date(value: str, label: str = "date") -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise HTTPException(status_code=422, detail=f"{label} must be YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"{label} must be a valid calendar date")
    if parsed.isoformat() != value:
        raise HTTPException(status_code=422, detail=f"{label} must be YYYY-MM-DD")
    return parsed


def _month_bounds(month: str) -> tuple[str, str, date, date]:
    if not isinstance(month, str) or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise HTTPException(status_code=422, detail="month must be YYYY-MM")
    year, mo = map(int, month.split("-"))
    start = date(year, mo, 1)
    next_month = date(year + (mo == 12), 1 if mo == 12 else mo + 1, 1)
    end = next_month - timedelta(days=1)
    return start.isoformat(), end.isoformat(), start, end


def _epoch_to_utc(ms: int) -> datetime:
    # Truncate milliseconds to whole seconds before calculations/storage.
    return datetime.fromtimestamp(ms / 1000, tz=UTC).replace(microsecond=0)


def _to_epoch_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp() * 1000)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _minutes_of_day(value: str) -> int:
    h, m = (int(part) for part in value.split(":"))
    return h * 60 + m


def _is_overnight(shift_start: str, shift_end: str) -> bool:
    return _minutes_of_day(shift_end) <= _minutes_of_day(shift_start)


def _attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    local = punch_in.astimezone(IST)
    day = local.date()
    if _is_overnight(shift_start, shift_end) and _minutes_of_day(f"{local.hour:02d}:{local.minute:02d}") < _minutes_of_day(shift_end):
        day -= timedelta(days=1)
    return day.isoformat()


def _scheduled_shift_start(attendance_day: str, shift_start: str) -> datetime:
    day = date.fromisoformat(attendance_day)
    hour, minute = map(int, shift_start.split(":"))
    return datetime.combine(day, time(hour, minute), tzinfo=IST).astimezone(UTC)


def _scheduled_shift_end(attendance_day: str, shift_start: str, shift_end: str) -> datetime:
    day = date.fromisoformat(attendance_day)
    if _is_overnight(shift_start, shift_end):
        day += timedelta(days=1)
    hour, minute = map(int, shift_end.split(":"))
    return datetime.combine(day, time(hour, minute), tzinfo=IST).astimezone(UTC)


def _late_minutes(punch_in: Optional[datetime], attendance_day: str, employee: dict) -> int:
    if punch_in is None:
        return 0
    delta_seconds = (_as_utc(punch_in) - _scheduled_shift_start(attendance_day, employee["shift_start"])).total_seconds()
    return int(delta_seconds // 60) if delta_seconds > 600 else 0


def _work_hours(punch_in: Optional[datetime], punch_out: Optional[datetime]) -> Optional[float]:
    if punch_in is None or punch_out is None:
        return None
    seconds = int((_as_utc(punch_out) - _as_utc(punch_in)).total_seconds())
    hours = (Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(hours)


def _overtime_minutes(punch_out: Optional[datetime], attendance_day: str, employee: dict) -> int:
    if punch_out is None:
        return 0
    diff = int((_as_utc(punch_out) - _scheduled_shift_end(attendance_day, employee["shift_start"], employee["shift_end"])).total_seconds())
    return diff // 60 if diff >= 1800 else 0


def _derived_fields(status: str, punch_in: Optional[datetime], punch_out: Optional[datetime], attendance_day: str, employee: dict) -> dict:
    if status not in PRESENT_STATUSES:
        return {"punch_in": None, "punch_out": None, "work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    late = _late_minutes(punch_in, attendance_day, employee)
    hours = _work_hours(punch_in, punch_out)
    overtime = _overtime_minutes(punch_out, attendance_day, employee) if punch_out is not None else 0
    half_day = hours is not None and hours < 4.50
    return {"punch_in": punch_in, "punch_out": punch_out, "work_hours": hours, "late_minutes": late, "overtime_minutes": overtime, "half_day": half_day}


def _serialize_employee(doc: dict) -> dict:
    doc = dict(doc)
    doc.pop("_id", None)
    if isinstance(doc.get("created_at"), datetime):
        doc["created_at"] = _to_epoch_ms(doc["created_at"])
    return doc


def _serialize_attendance(doc: dict) -> dict:
    doc = dict(doc)
    doc.pop("_id", None)
    doc["punch_in"] = _to_epoch_ms(doc.get("punch_in"))
    doc["punch_out"] = _to_epoch_ms(doc.get("punch_out"))
    doc.setdefault("work_hours", None)
    doc.setdefault("late_minutes", 0)
    doc.setdefault("overtime_minutes", 0)
    doc.setdefault("half_day", False)
    history = []
    for original in doc.get("history", []) or []:
        entry = dict(original)
        entry["at"] = _to_epoch_ms(entry.get("at"))
        changes = {}
        for field, change in (entry.get("changes") or {}).items():
            pair = dict(change)
            if field in ("punch_in", "punch_out"):
                pair["from"] = _to_epoch_ms(pair.get("from"))
                pair["to"] = _to_epoch_ms(pair.get("to"))
            changes[field] = pair
        entry["changes"] = changes
        history.append(entry)
    doc["history"] = history
    return doc


def _round_half_up_expression(expr, places: int = 2):
    factor = 10 ** places
    return {"$divide": [{"$floor": {"$add": [{"$multiply": [expr, factor]}, 0.5]}}, factor]}


def _att_filter_pipeline(emp_code: Optional[str], date_from: Optional[str], date_to: Optional[str], status: Optional[str], page: int, page_size: int) -> list[dict]:
    query: dict = {}
    if emp_code:
        query["emp_code"] = emp_code
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    if status:
        query["status"] = status
    pipeline = []
    if query:
        pipeline.append({"$match": query})
    pipeline.extend([{"$sort": {"date": -1, "emp_code": 1}}, {"$skip": (page - 1) * page_size}, {"$limit": page_size}])
    return pipeline


def _attendance_hint(emp_code: Optional[str], status: Optional[str]) -> str:
    if emp_code:
        return "ix_attendance_employee_date"
    if status:
        return "ix_attendance_status_date_code"
    return "ix_attendance_date_code"


def _monthly_employee_pipeline(emp_code: str, month: str) -> list[dict]:
    month_start, month_end, _, _ = _month_bounds(month)
    period_start = {"$cond": [{"$gt": ["$joined_on", month_start]}, "$joined_on", month_start]}
    day_count = {"$add": [{"$dateDiff": {"startDate": "$_period_start", "endDate": {"$dateFromString": {"dateString": month_end}}, "unit": "day"}}, 1]}
    day_values = {"$map": {"input": {"$range": [0, {"$max": [0, day_count]}]}, "as": "offset", "in": {"$dateAdd": {"startDate": "$_period_start", "unit": "day", "amount": "$$offset"}}}}
    return [
        {"$match": {"emp_code": emp_code}},
        {"$addFields": {"_period_start_str": period_start, "_period_start": {"$dateFromString": {"dateString": period_start}}}},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [
            {"$match": {"date": {"$gte": month_start, "$lte": month_end}, "$expr": {"$eq": ["$emp_code", "$$code"]}}},
            {"$project": {"_id": 0, "date": 1, "status": 1, "late_minutes": {"$ifNull": ["$late_minutes", 0]}, "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]}, "work_hours": 1, "half_day": {"$ifNull": ["$half_day", False]}}},
        ], "as": "_logs"}},
        {"$addFields": {
            "_logs": {
                "$map": {
                    "input": {"$filter": {"input": "$_logs", "as": "r", "cond": {"$gte": ["$$r.date", "$_period_start_str"]}}},
                    "as": "r",
                    "in": {"$mergeObjects": ["$$r", {"_dow": {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$$r.date"}}}}]},
                }
            }
        }},
        {"$addFields": {
            "working_days": {"$size": {"$filter": {"input": day_values, "as": "d", "cond": {"$lte": [{"$isoDayOfWeek": "$$d"}, 5]}}}},
            "present_days": {"$sum": {"$map": {"input": {"$filter": {"input": "$_logs", "as": "r", "cond": {"$and": [{"$in": ["$$r.status", list(PRESENT_STATUSES)]}, {"$lte": ["$$r._dow", 5]}]}}}, "as": "r", "in": {"$cond": ["$$r.half_day", 0.5, 1]}}}},
            "leave_days": {"$size": {"$filter": {"input": "$_logs", "as": "r", "cond": {"$eq": ["$$r.status", "LEAVE"]}}}},
            "late_count": {"$size": {"$filter": {"input": "$_logs", "as": "r", "cond": {"$gt": ["$$r.late_minutes", 0]}}}},
            "total_late_minutes": {"$sum": {"$map": {"input": "$_logs", "as": "r", "in": "$$r.late_minutes"}}},
            "total_overtime_minutes": {"$sum": {"$map": {"input": "$_logs", "as": "r", "in": "$$r.overtime_minutes"}}},
        }},
        {"$project": {"_id": 0, "emp_code": 1, "working_days": 1, "present_days": 1, "leave_days": 1, "late_count": 1, "total_late_minutes": 1, "total_overtime_minutes": 1,
            "attendance_pct": {"$cond": [{"$gt": ["$working_days", 0]}, _round_half_up_expression({"$multiply": [{"$divide": ["$present_days", "$working_days"]}, 100]}, 4), None]}}},
    ]


def _department_summary_pipeline(month: str, department: Optional[str]) -> list[dict]:
    month_start, month_end, _, _ = _month_bounds(month)
    match: dict = {"joined_on": {"$lte": month_end}}
    if department:
        match["department"] = department
    p = [
        {"$match": match},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code", "joined": "$joined_on"}, "pipeline": [
            {"$match": {"date": {"$gte": month_start, "$lte": month_end}, "$expr": {"$eq": ["$emp_code", "$$code"]}}},
            {"$project": {"_id": 0, "date": 1, "status": 1, "work_hours": 1, "late_minutes": {"$ifNull": ["$late_minutes", 0]}, "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]}, "half_day": {"$ifNull": ["$half_day", False]}}},
        ], "as": "_all_logs"}},
        {"$addFields": {
            "_logs": {
                "$map": {
                    "input": {"$filter": {"input": "$_all_logs", "as": "r", "cond": {"$gte": ["$$r.date", {"$cond": [{"$gt": ["$joined_on", month_start]}, "$joined_on", month_start]}]}}},
                    "as": "r",
                    "in": {"$mergeObjects": ["$$r", {"_dow": {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$$r.date"}}}}]},
                }
            }
        }},
        {"$addFields": {
            "_present": {"$filter": {"input": "$_logs", "as": "r", "cond": {"$and": [{"$in": ["$$r.status", list(PRESENT_STATUSES)]}, {"$lte": ["$$r._dow", 5]}]}}},
            "_worked": {"$filter": {"input": "$_logs", "as": "r", "cond": {"$and": [{"$in": ["$$r.status", list(PRESENT_STATUSES)]}, {"$ne": [{"$ifNull": ["$$r.work_hours", None]}, None]}]}}},
            "_late": {"$filter": {"input": "$_logs", "as": "r", "cond": {"$gt": ["$$r.late_minutes", 0]}}},
            "_leave": {"$filter": {"input": "$_logs", "as": "r", "cond": {"$eq": ["$$r.status", "LEAVE"]}}},
            "_onduty": {"$filter": {"input": "$_logs", "as": "r", "cond": {"$eq": ["$$r.status", "ON_DUTY"]}}},
        }},
        {"$group": {
            "_id": "$department", "headcount": {"$sum": 1},
            "present_days": {"$sum": {"$sum": {"$map": {"input": "$_present", "as": "r", "in": {"$cond": ["$$r.half_day", 0.5, 1]}}}}},
            "_work_hours_sum": {"$sum": {"$sum": {"$map": {"input": "$_worked", "as": "r", "in": "$$r.work_hours"}}}},
            "_work_hours_count": {"$sum": {"$size": "$_worked"}},
            "late_count": {"$sum": {"$size": "$_late"}},
            "total_late_minutes": {"$sum": {"$sum": {"$map": {"input": "$_late", "as": "r", "in": "$$r.late_minutes"}}}},
            "leave_count": {"$sum": {"$size": "$_leave"}},
            "on_duty_count": {"$sum": {"$size": "$_onduty"}},
        }},
        {"$project": {"_id": 0, "department": "$_id", "headcount": 1, "present_days": 1,
            "avg_work_hours": {"$cond": [{"$gt": ["$_work_hours_count", 0]}, _round_half_up_expression({"$divide": ["$_work_hours_sum", "$_work_hours_count"]}, 2), None]},
            "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}},
        {"$sort": {"department": 1}},
    ]
    return p


def _late_leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list[dict]:
    month_start, month_end, _, _ = _month_bounds(month)
    pipeline: list[dict] = [
        {"$match": {"date": {"$gte": month_start, "$lte": month_end}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend([
        {"$set": {"emp_code": "$_id", "name": "$employee.name", "department": "$employee.department"}},
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": 1, "name": 1, "department": 1, "total_late_minutes": 1, "late_count": 1}},
    ])
    return pipeline


def _department_trend_pipeline(department: str, from_day: date, to_day: date) -> list[dict]:
    start_dt = datetime.combine(from_day, time.min, tzinfo=UTC)
    bounds_end = datetime.combine(to_day + timedelta(days=1), time.min, tzinfo=UTC)
    # Start with one department employee to seed the range, then union the real
    # attendance rows. The seed makes $densify return rows even if there are no logs.
    return [
        {"$match": {"department": department}},
        {"$limit": 1},
        {"$project": {"_id": 0, "day": {"$literal": start_dt}, "present_count": {"$literal": 0}, "late_count": {"$literal": 0}}},
        {"$unionWith": {"coll": "attendance_logs", "pipeline": [
            {"$match": {"date": {"$gte": from_day.isoformat(), "$lte": to_day.isoformat()}}},
            {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
            {"$unwind": "$employee"},
            {"$match": {"employee.department": department}},
            {"$project": {"_id": 0, "day": {"$dateFromString": {"dateString": "$date"}},
                "present_count": {"$cond": [{"$in": ["$status", list(PRESENT_STATUSES)]}, {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]},
                "late_count": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}},
        ]}},
        {"$group": {"_id": "$day", "present_count": {"$sum": "$present_count"}, "late_count": {"$sum": "$late_count"}}},
        {"$project": {"_id": 0, "day": "$_id", "present_count": 1, "late_count": 1}},
        {"$densify": {"field": "day", "range": {"bounds": [start_dt, bounds_end], "step": 1, "unit": "day"}}},
        {"$set": {"date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d", "timezone": "UTC"}},
            "present_count": {"$ifNull": ["$present_count", 0]}, "late_count": {"$ifNull": ["$late_count", 0]},
            "is_working_day": {"$lte": [{"$isoDayOfWeek": "$day"}, 5]}}},
        {"$lookup": {"from": "employees", "let": {"dayStr": "$date"}, "pipeline": [
            {"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$dayStr"]}}},
            {"$count": "n"},
        ], "as": "_headcount"}},
        {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$_headcount.n", 0]}, 0]}}},
        {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, _round_half_up_expression({"$divide": ["$present_count", "$headcount"]}, 4), None]}}},
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {"_moving_avg_raw": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1, "attendance_rate": 1,
            "moving_avg_7d": {"$cond": [{"$eq": ["$_moving_avg_raw", None]}, None, _round_half_up_expression("$_moving_avg_raw", 4)]}}},
        {"$sort": {"date": 1}},
    ]


def _json_safe(value):
    # Explain plans may contain BSON-specific values; Extended JSON is safe to return.
    return json_util.loads(json_util.dumps(value))


def _explain_aggregate(collection_name: str, pipeline: list[dict], hint: Optional[str] = None) -> dict:
    command: dict = {"aggregate": collection_name, "pipeline": pipeline, "cursor": {}}
    if hint:
        command["hint"] = hint
    return db.command({"explain": command, "verbosity": "executionStats"})


# ---------------------------------------------------------------------------
# Request models (strict types prevent seconds, strings, or floats as epochs).
# ---------------------------------------------------------------------------
class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
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
        try:
            parsed = date.fromisoformat(value)
        except (ValueError, TypeError):
            raise ValueError("joined_on must be a valid YYYY-MM-DD date")
        if parsed.isoformat() != value:
            raise ValueError("joined_on must be YYYY-MM-DD")
        return value

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @model_validator(mode="after")
    def reject_null_epoch(self):
        if "punched_at" in self.model_fields_set and self.punched_at is None:
            raise ValueError("punched_at must be an epoch-millisecond integer")
        return self


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    emp_code: str
    punched_at: Optional[EpochMillis] = None

    @model_validator(mode="after")
    def reject_null_epoch(self):
        if "punched_at" in self.model_fields_set and self.punched_at is None:
            raise ValueError("punched_at must be an epoch-millisecond integer")
        return self


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def validate_epochs(self):
        if "status" in self.model_fields_set and self.status is None:
            raise ValueError("status cannot be null when supplied")
        for field in ("punch_in", "punch_out"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} must be an epoch-millisecond integer when supplied")
        return self


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="MongoDB is unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC).replace(microsecond=0)
    try:
        employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return _serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    query = {"department": department} if department is not None else {}
    total = employees.count_documents(query)
    docs = employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [_serialize_employee(d) for d in docs], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    employee = employees.find_one({"emp_code": body.emp_code})
    if not employee:
        raise HTTPException(status_code=404, detail="employee not found")
    punched_at = _epoch_to_utc(body.punched_at) if body.punched_at is not None else datetime.now(UTC).replace(microsecond=0)
    attendance_day = _attendance_date(punched_at, employee["shift_start"], employee["shift_end"])
    derived = _derived_fields(body.status, punched_at, None, attendance_day, employee)
    doc = {"emp_code": body.emp_code, "date": attendance_day, "status": body.status,
           **derived, "history": []}
    try:
        attendance.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")
    return _serialize_attendance(doc)


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutIn):
    employee = employees.find_one({"emp_code": body.emp_code})
    if not employee:
        raise HTTPException(status_code=404, detail="employee not found")
    punched_out = _epoch_to_utc(body.punched_at) if body.punched_at is not None else datetime.now(UTC).replace(microsecond=0)
    record = attendance.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": punched_out}}, sort=[("punch_in", DESCENDING), ("date", DESCENDING)])
    if not record:
        raise HTTPException(status_code=404, detail="no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="this attendance record is already punched out")
    punched_in = _as_utc(record["punch_in"])
    elapsed = (punched_out - punched_in).total_seconds()
    if elapsed <= 0 or elapsed > 24 * 3600:
        raise HTTPException(status_code=422, detail="punched_at must be after punch_in and within 24 hours")
    derived = _derived_fields(record["status"], punched_in, punched_out, record["date"], employee)
    updated = attendance.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {"punch_out": derived["punch_out"], "work_hours": derived["work_hours"],
                  "overtime_minutes": derived["overtime_minutes"], "half_day": derived["half_day"]}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="this attendance record was punched out concurrently")
    return _serialize_attendance(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    start = date_from.isoformat() if date_from else None
    end = date_to.isoformat() if date_to else None
    if start and end and start > end:
        raise HTTPException(status_code=422, detail="date_from must be on or before date_to")
    query: dict = {}
    if emp_code:
        query["emp_code"] = emp_code
    if start or end:
        query["date"] = {}
        if start:
            query["date"]["$gte"] = start
        if end:
            query["date"]["$lte"] = end
    if status:
        query["status"] = status
    total = attendance.count_documents(query)
    hint = _attendance_hint(emp_code, status)
    pipeline = _att_filter_pipeline(emp_code, start, end, status, page, page_size)
    records = attendance.aggregate(pipeline, hint=hint)
    return {"items": [_serialize_attendance(d) for d in records], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    date_value: str = ApiPath(alias="date"),
    body: RegularizeIn = ...,
):
    _parse_iso_date(date_value, "date")
    employee = employees.find_one({"emp_code": emp_code})
    if not employee:
        raise HTTPException(status_code=404, detail="employee not found")
    record = attendance.find_one({"emp_code": emp_code, "date": date_value})
    if not record:
        raise HTTPException(status_code=404, detail="attendance record not found")

    supplied = body.model_fields_set
    final_status = body.status if "status" in supplied else record["status"]
    final_in = _epoch_to_utc(body.punch_in) if "punch_in" in supplied else record.get("punch_in")
    final_out = _epoch_to_utc(body.punch_out) if "punch_out" in supplied else record.get("punch_out")

    if final_status in ("ABSENT", "LEAVE"):
        if "punch_in" in supplied or "punch_out" in supplied:
            raise HTTPException(status_code=422, detail="punch times cannot be supplied with ABSENT or LEAVE")
        final_in = final_out = None
    else:
        if final_in is None:
            raise HTTPException(status_code=422, detail="presence status requires punch_in")
        if _attendance_date(_as_utc(final_in), employee["shift_start"], employee["shift_end"]) != date_value:
            raise HTTPException(status_code=422, detail="punch_in must belong to the record attendance date")
        if final_out is not None:
            elapsed = (_as_utc(final_out) - _as_utc(final_in)).total_seconds()
            if elapsed <= 0 or elapsed > 24 * 3600:
                raise HTTPException(status_code=422, detail="punch_out must be after punch_in and within 24 hours")

    derived = _derived_fields(final_status, final_in, final_out, date_value, employee)
    old_values = {"status": record.get("status"), "punch_in": record.get("punch_in"), "punch_out": record.get("punch_out"),
                  "work_hours": record.get("work_hours"), "late_minutes": record.get("late_minutes", 0),
                  "overtime_minutes": record.get("overtime_minutes", 0), "half_day": record.get("half_day", False)}
    new_values = {"status": final_status, **derived}
    changed = {}
    audit_changes = {}
    for field in ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"):
        old = old_values.get(field)
        new = new_values.get(field)
        if old != new:
            changed[field] = {"from": old, "to": new}
            audit_changes[field] = {"from": old, "to": new}
    if not changed:
        raise HTTPException(status_code=422, detail="regularization does not change the record")

    history_entry = {"at": datetime.now(UTC).replace(microsecond=0), "by": body.regularized_by, "reason": body.reason, "changes": audit_changes}
    optimistic_filter = {"_id": record["_id"], "status": record.get("status"), "punch_in": record.get("punch_in"), "punch_out": record.get("punch_out")}
    if "history" in record:
        optimistic_filter["history"] = record["history"]
    else:
        optimistic_filter["history"] = {"$exists": False}
    update = {"$set": {"status": final_status, **derived}, "$push": {"history": history_entry}}
    result = attendance.update_one(optimistic_filter, update)
    if result.modified_count != 1:
        raise HTTPException(status_code=409, detail="record changed concurrently; retry regularization")
    updated = attendance.find_one({"_id": record["_id"]})
    return _serialize_attendance(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(...)):
    _month_bounds(month)
    if not employees.find_one({"emp_code": emp_code}, {"_id": 1}):
        raise HTTPException(status_code=404, detail="employee not found")
    results = list(employees.aggregate(_monthly_employee_pipeline(emp_code, month), hint="uq_employee_emp_code"))
    if not results:
        raise HTTPException(status_code=404, detail="employee not found")
    return {"month": month, **results[0]}


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(...), department: Optional[str] = None):
    _month_bounds(month)
    pipeline = _department_summary_pipeline(month, department)
    hint = "ix_employee_department_joined" if department else "ix_employee_joined_on"
    items = list(employees.aggregate(pipeline, hint=hint))
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(...), limit: int = Query(default=10, ge=1, le=50), department: Optional[str] = None):
    _month_bounds(month)
    pipeline = _late_leaderboard_pipeline(month, limit, department)
    items = list(attendance.aggregate(pipeline, hint="ix_attendance_date_code"))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_day: str = Query(..., alias="from"), to_day: str = Query(..., alias="to")):
    start = _parse_iso_date(from_day, "from")
    end = _parse_iso_date(to_day, "to")
    if end < start:
        raise HTTPException(status_code=422, detail="to must be on or after from")
    if (end - start).days + 1 > 92:
        raise HTTPException(status_code=422, detail="date range cannot exceed 92 days")
    if not employees.find_one({"department": department}, {"_id": 1}):
        raise HTTPException(status_code=404, detail="department not found")
    pipeline = _department_trend_pipeline(department, start, end)
    items = list(employees.aggregate(pipeline, hint="ix_employee_department_code"))
    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    from_day: Optional[str] = Query(default=None, alias="from"),
    to_day: Optional[str] = Query(default=None, alias="to"),
):
    collection_name = "attendance_logs"
    hint = None
    if endpoint == "attendance_list":
        start = date_from.isoformat() if date_from else None
        end = date_to.isoformat() if date_to else None
        if start and end and start > end:
            raise HTTPException(status_code=422, detail="date_from must be on or before date_to")
        pipeline = _att_filter_pipeline(emp_code, start, end, status, page, page_size)
        collection_name = "attendance_logs"
        hint = _attendance_hint(emp_code, status)
    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required for employee_monthly")
        _month_bounds(month)
        pipeline = _monthly_employee_pipeline(emp_code, month)
        collection_name = "employees"
        hint = "uq_employee_emp_code"
    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for department_summary")
        _month_bounds(month)
        pipeline = _department_summary_pipeline(month, department)
        collection_name = "employees"
        hint = "ix_employee_department_joined" if department else "ix_employee_joined_on"
    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for late_leaderboard")
        _month_bounds(month)
        pipeline = _late_leaderboard_pipeline(month, limit, department)
        collection_name = "attendance_logs"
        hint = "ix_attendance_date_code"
    else:
        if not department or not from_day or not to_day:
            raise HTTPException(status_code=422, detail="department, from and to are required for department_trend")
        start = _parse_iso_date(from_day, "from")
        end = _parse_iso_date(to_day, "to")
        if end < start or (end - start).days + 1 > 92:
            raise HTTPException(status_code=422, detail="invalid trend range; to must be >= from and range <= 92 days")
        pipeline = _department_trend_pipeline(department, start, end)
        collection_name = "employees"
        hint = "ix_employee_department_code"
    try:
        explained = _explain_aggregate(collection_name, pipeline, hint)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Unable to explain query: {type(exc).__name__}")
    return {"endpoint": endpoint, "collection": collection_name, "explain": _json_safe(explained)}
