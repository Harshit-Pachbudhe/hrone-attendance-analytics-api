"""
Employee Attendance & Analytics API.

Run from the repository root:
    uvicorn app.main:app --port 8000

Configuration is read from MONGO_URI and MONGO_DB. A local .env is supported;
real environment variables take precedence over values in .env.
"""
import os
import re
import json
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000, tz_aware=False)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE = ("PRESENT", "WFH", "ON_DUTY")
ALL_STATUSES = ("PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE")
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# --------------------------------------------------------------------------- #
# Common helpers
# --------------------------------------------------------------------------- #
def now_utc() -> datetime:
    """Return a naive UTC datetime, matching PyMongo's default datetime mode."""
    return datetime.now(UTC).replace(tzinfo=None, microsecond=0)


def truncate_seconds(value: datetime) -> datetime:
    return value.replace(microsecond=0)


def from_epoch_ms(value: int) -> datetime:
    if type(value) is not int or not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS:
        raise HTTPException(422, "timestamp must be an integer epoch-millisecond value in range")
    return truncate_seconds(datetime.fromtimestamp(value / 1000, tz=UTC).replace(tzinfo=None))


def to_epoch_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    # MongoDB returns naive UTC datetimes. Treat them as UTC, never local time.
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return int(aware.timestamp() * 1000)


def decimal_round(value, places: int = 2):
    if value is None:
        return None
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def parse_iso_date(value: str, field_name: str = "date") -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        raise HTTPException(422, f"{field_name} must be YYYY-MM-DD")
    if parsed.isoformat() != value:
        raise HTTPException(422, f"{field_name} must be YYYY-MM-DD")
    return parsed


def parse_month(value: str) -> tuple[date, date]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value or ""):
        raise HTTPException(422, "month must be YYYY-MM")
    start = date.fromisoformat(value + "-01")
    if start.month == 12:
        next_month = date(start.year + 1, 1, 1)
    else:
        next_month = date(start.year, start.month + 1, 1)
    return start, next_month


def date_bounds(month: str) -> tuple[str, str]:
    start, next_month = parse_month(month)
    return start.isoformat(), next_month.isoformat()


def ist_datetime(value: datetime) -> datetime:
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value
    return aware.astimezone(IST)


def attendance_date_for(punch_in: datetime, shift_start: str, shift_end: str) -> date:
    local = ist_datetime(punch_in)
    shift_end_time = time.fromisoformat(shift_end)
    shift_start_time = time.fromisoformat(shift_start)
    result = local.date()
    if shift_end_time <= shift_start_time and local.time().replace(tzinfo=None) < shift_end_time:
        result -= timedelta(days=1)
    return result


def shift_start_datetime(attendance_day: date, shift_start: str) -> datetime:
    local = datetime.combine(attendance_day, time.fromisoformat(shift_start), tzinfo=IST)
    return local.astimezone(UTC).replace(tzinfo=None)


def shift_end_datetime(attendance_day: date, shift_start: str, shift_end: str) -> datetime:
    end_day = attendance_day
    if time.fromisoformat(shift_end) <= time.fromisoformat(shift_start):
        end_day += timedelta(days=1)
    local = datetime.combine(end_day, time.fromisoformat(shift_end), tzinfo=IST)
    return local.astimezone(UTC).replace(tzinfo=None)


def compute_late_minutes(punch_in: datetime, shift_start: str, attendance_day: Optional[date] = None) -> int:
    """R2: strictly over ten minutes late; count whole minutes since shift start."""
    local = ist_datetime(punch_in)
    day = attendance_day or local.date()
    start = shift_start_datetime(day, shift_start)
    elapsed = int((punch_in - start).total_seconds())
    return elapsed // 60 if elapsed > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int((punch_out - punch_in).total_seconds())
    return decimal_round(Decimal(seconds) / Decimal(3600), 2)


def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, date_str: str) -> int:
    day = parse_iso_date(date_str)
    end = shift_end_datetime(day, shift_start, shift_end)
    elapsed = int((punch_out - end).total_seconds())
    minutes = elapsed // 60
    return minutes if minutes >= 30 else 0


def serialize_employee(doc: dict) -> dict:
    result = {k: v for k, v in doc.items() if k != "_id"}
    if "created_at" in result:
        result["created_at"] = to_epoch_ms(result["created_at"])
    return result


def serialize_attendance(doc: dict) -> dict:
    result = {k: v for k, v in doc.items() if k != "_id"}
    result["punch_in"] = to_epoch_ms(result.get("punch_in"))
    result["punch_out"] = to_epoch_ms(result.get("punch_out"))
    result["work_hours"] = decimal_round(result.get("work_hours"), 2)
    result["late_minutes"] = int(result.get("late_minutes") or 0)
    result["overtime_minutes"] = int(result.get("overtime_minutes") or 0)
    result["half_day"] = bool(result.get("half_day", False))
    result["history"] = result.get("history") or []
    for entry in result["history"]:
        if "at" in entry:
            entry["at"] = to_epoch_ms(entry["at"])
        changes = entry.get("changes", {})
        for field in ("punch_in", "punch_out"):
            if field in changes:
                changes[field]["from"] = to_epoch_ms(changes[field].get("from"))
                changes[field]["to"] = to_epoch_ms(changes[field].get("to"))
    return result


def get_employee(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    return emp


def validate_epoch_field(value):
    if value is None:
        return None
    if type(value) is not int or not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS:
        raise ValueError("must be an integer epoch-millisecond timestamp in range")
    return value


def working_day(date_value: date) -> bool:
    return date_value.weekday() < 5


def month_filter(month: str) -> dict:
    start, end = date_bounds(month)
    return {"date": {"$gte": start, "$lt": end}}


def month_working_days_pipeline(joined_on_expr="$joined_on", month_start: date = date(2000, 1, 1), next_month: date = date(2000, 2, 1)):
    """Expression counting weekdays from max(joined_on, month start) through month end."""
    return {
        "$let": {
            "vars": {
                "first": {"$max": [{"$dateFromString": {"dateString": joined_on_expr}}, {"$dateFromString": {"dateString": month_start.isoformat()}}]},
                "last": {"$dateFromString": {"dateString": (next_month - timedelta(days=1)).isoformat()}},
            },
            "in": {
                "$cond": [
                    {"$gt": ["$$first", "$$last"]},
                    0,
                    {"$size": {"$filter": {
                        "input": {"$map": {
                            "input": {"$range": [0, {"$add": [{"$dateDiff": {"startDate": "$$first", "endDate": "$$last", "unit": "day"}}, 1]}]},
                            "as": "n",
                            "in": {"$dateAdd": {"startDate": "$$first", "unit": "day", "amount": "$$n"}},
                        }},
                        "as": "d",
                        "cond": {"$and": [{"$gte": [{"$dayOfWeek": "$$d"}, 2]}, {"$lte": [{"$dayOfWeek": "$$d"}, 6]}]},
                    }}},
                ],
            },
        }
    }


def ensure_indexes():
    """Idempotent indexes for natural keys, list ordering, and aggregation lookups."""
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="ix_employee_department_joined")
    db.employees.create_index([("joined_on", ASCENDING), ("department", ASCENDING)], name="ix_employee_joined_department")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_employee_date")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_employee")
    db.attendance_logs.create_index([("status", ASCENDING), ("date", ASCENDING), ("emp_code", ASCENDING)], name="ix_attendance_status_date_employee")


@app.on_event("startup")
def startup_indexes():
    ensure_indexes()


# --------------------------------------------------------------------------- #
# Request models. Unknown fields are ignored so clients cannot set derived data.
# --------------------------------------------------------------------------- #
class APIModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class EmployeeIn(APIModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def valid_joined_on(cls, value):
        try:
            if date.fromisoformat(value).isoformat() != value:
                raise ValueError
        except ValueError:
            raise ValueError("joined_on must be YYYY-MM-DD")
        return value

    @model_validator(mode="after")
    def different_shift_times(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(APIModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def valid_punched_at(cls, value):
        return validate_epoch_field(value)

    @model_validator(mode="after")
    def reject_explicit_null_timestamp(self):
        if "punched_at" in self.model_fields_set and self.punched_at is None:
            raise ValueError("punched_at must be an epoch-millisecond integer when supplied")
        return self


class PunchOutIn(APIModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None

    @field_validator("punched_at")
    @classmethod
    def valid_punched_at(cls, value):
        return validate_epoch_field(value)

    @model_validator(mode="after")
    def reject_explicit_null_timestamp(self):
        if "punched_at" in self.model_fields_set and self.punched_at is None:
            raise ValueError("punched_at must be an epoch-millisecond integer when supplied")
        return self


class RegularizeIn(APIModel):
    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out")
    @classmethod
    def valid_punch_times(cls, value):
        return validate_epoch_field(value)

    @model_validator(mode="after")
    def reject_explicit_null_changes(self):
        for field in ("status", "punch_in", "punch_out"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null when supplied")
        return self


# --------------------------------------------------------------------------- #
# Health and employee endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        db.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {"department": department} if department is not None else {}
    total = db.employees.count_documents(query)
    items = db.employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [serialize_employee(d) for d in items], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance CRUD
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee(body.emp_code)
    ts = from_epoch_ms(body.punched_at) if body.punched_at is not None else truncate_seconds(now_utc())
    local = ist_datetime(ts)
    attendance_day = attendance_date_for(ts, emp["shift_start"], emp["shift_end"])
    date_str = attendance_day.isoformat()
    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], attendance_day),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return serialize_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee(body.emp_code)
    ts = from_epoch_ms(body.punched_at) if body.punched_at is not None else truncate_seconds(now_utc())
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": ts}},
        sort=[("punch_in", DESCENDING)],
    )
    if not record:
        raise HTTPException(404, "no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "attendance record is already punched out")
    elapsed = (ts - record["punch_in"]).total_seconds()
    if elapsed <= 0 or elapsed > 24 * 60 * 60:
        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours")
    hours = compute_work_hours(record["punch_in"], ts)
    overtime = compute_overtime(ts, emp["shift_start"], emp["shift_end"], record["date"])
    half_day = hours < 4.50
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, "work_hours": hours, "overtime_minutes": overtime, "half_day": half_day}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "attendance record is already punched out")
    return serialize_attendance(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if status is not None and status not in ALL_STATUSES:
        raise HTTPException(422, "invalid status")
    if date_from is not None:
        parse_iso_date(date_from, "date_from")
    if date_to is not None:
        parse_iso_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be on or before date_to")
    query = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    if status:
        query["status"] = status
    total = db.attendance_logs.count_documents(query)
    docs = db.attendance_logs.find(query, {"_id": 0}).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size)
    return {"items": [serialize_attendance(d) for d in docs], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    record_date = parse_iso_date(date).isoformat()
    emp = get_employee(emp_code)
    old = db.attendance_logs.find_one({"emp_code": emp_code, "date": record_date})
    if not old:
        raise HTTPException(404, "attendance record not found")

    supplied = body.model_fields_set
    new_status = body.status if "status" in supplied and body.status is not None else old["status"]
    old_in = old.get("punch_in")
    old_out = old.get("punch_out")
    new_in = from_epoch_ms(body.punch_in) if "punch_in" in supplied and body.punch_in is not None else old_in
    new_out = from_epoch_ms(body.punch_out) if "punch_out" in supplied and body.punch_out is not None else old_out

    if new_status in ("ABSENT", "LEAVE"):
        if "punch_in" in supplied or "punch_out" in supplied:
            raise HTTPException(422, "ABSENT and LEAVE cannot be supplied with punch times")
        new_in, new_out = None, None
    elif new_in is None:
        raise HTTPException(422, "presence status requires punch_in")

    if new_in is not None:
        computed_day = attendance_date_for(new_in, emp["shift_start"], emp["shift_end"])
        if computed_day.isoformat() != record_date:
            raise HTTPException(422, "punch_in must belong to the record attendance date")
    if new_out is not None:
        if new_in is None or new_out <= new_in or (new_out - new_in).total_seconds() > 24 * 3600:
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    if new_status in ("ABSENT", "LEAVE"):
        derived = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    else:
        late = compute_late_minutes(new_in, emp["shift_start"], record_date and parse_iso_date(record_date))
        if new_out is None:
            derived = {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
        else:
            hours = compute_work_hours(new_in, new_out)
            derived = {
                "work_hours": hours,
                "late_minutes": late,
                "overtime_minutes": compute_overtime(new_out, emp["shift_start"], emp["shift_end"], record_date),
                "half_day": hours < 4.50,
            }

    proposed = {"status": new_status, "punch_in": new_in, "punch_out": new_out, **derived}
    changes = {}
    for field, new_value in proposed.items():
        old_value = old.get(field)
        if field in ("late_minutes", "overtime_minutes"):
            old_value = int(old_value or 0)
        if field == "half_day":
            old_value = bool(old_value or False)
        if old_value != new_value:
            changes[field] = {
                "from": old_value,
                "to": new_value,
            }
    if not changes:
        raise HTTPException(422, "request does not change the record")

    history_changes = {}
    for field, pair in changes.items():
        history_changes[field] = dict(pair)
    history_entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": history_changes}

    # Compare-and-set: if another regularization changed this record after our read, return 409.
    cas = {"_id": old["_id"], "status": old["status"]}
    for field in ("punch_in", "punch_out"):
        if field in old:
            cas[field] = old[field]
        else:
            cas[field] = {"$exists": False}
    old_history = old.get("history")
    if "history" in old:
        cas["history"] = old_history
    else:
        cas["history"] = {"$exists": False}
    updated = db.attendance_logs.find_one_and_update(
        cas,
        {"$set": proposed, "$push": {"history": history_entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "attendance record changed concurrently; retry")
    return serialize_attendance(updated)


# --------------------------------------------------------------------------- #
# Analytics aggregation pipelines
# --------------------------------------------------------------------------- #
def employee_monthly_pipeline(emp_code: str, month: str):
    start, next_month = parse_month(month)
    start_s, end_s = start.isoformat(), next_month.isoformat()
    return [
        {"$match": {"emp_code": emp_code}},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"code": "$emp_code"},
            "pipeline": [{"$match": {"$expr": {"$and": [
                {"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", start_s]}, {"$lt": ["$date", end_s]}
            ]}}}],
            "as": "logs",
        }},
        {"$set": {
            "working_days": month_working_days_pipeline("$joined_on", start, next_month),
            "present_days": {"$sum": {"$map": {
                "input": {"$filter": {"input": "$logs", "as": "l", "cond": {"$and": [
                    {"$in": ["$$l.status", list(PRESENCE)]},
                    {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$$l.date"}}}, 2]},
                    {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$$l.date"}}}, 6]},
                ]}}},
                "as": "l",
                "in": {"$cond": [{"$eq": ["$$l.half_day", True]}, 0.5, 1]},
            }}},
            "leave_days": {"$size": {"$filter": {"input": "$logs", "as": "l", "cond": {"$eq": ["$$l.status", "LEAVE"]}}}},
            "late_count": {"$size": {"$filter": {"input": "$logs", "as": "l", "cond": {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]}}}},
            "total_late_minutes": {"$sum": {"$map": {"input": "$logs", "as": "l", "in": {"$ifNull": ["$$l.late_minutes", 0]}}}},
            "total_overtime_minutes": {"$sum": {"$map": {"input": "$logs", "as": "l", "in": {"$ifNull": ["$$l.overtime_minutes", 0]}}}},
        }},
        {"$project": {
            "_id": 0, "emp_code": 1, "working_days": 1, "present_days": 1, "leave_days": 1,
            "late_count": 1, "total_late_minutes": 1, "total_overtime_minutes": 1,
            "attendance_pct": {"$cond": [
                {"$gt": ["$working_days", 0]},
                {"$multiply": [{"$divide": ["$present_days", "$working_days"]}, 100]},
                None,
            ]},
        }},
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    get_employee(emp_code)
    pipeline = employee_monthly_pipeline(emp_code, month)
    result = next(db.employees.aggregate(pipeline), None)
    if result is None:
        raise HTTPException(404, "employee not found")
    result["month"] = month
    result["present_days"] = decimal_round(result.get("present_days", 0), 2)
    result["attendance_pct"] = decimal_round(result.get("attendance_pct"), 4)
    for key in ("working_days", "leave_days", "late_count", "total_late_minutes", "total_overtime_minutes"):
        result[key] = int(result.get(key) or 0)
    return result


def department_summary_pipeline(month: str, department: Optional[str] = None):
    start, end = date_bounds(month)
    employee_match = {"joined_on": {"$lt": end}}
    if department is not None:
        employee_match["department"] = department
    return [
        {"$match": employee_match},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"code": "$emp_code"},
            "pipeline": [{"$match": {"$expr": {"$and": [
                {"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", start]}, {"$lt": ["$date", end]}
            ]}}}],
            "as": "logs",
        }},
        {"$set": {
            "employee_present_days": {"$sum": {"$map": {
                "input": {"$filter": {
                    "input": "$logs", "as": "l",
                    "cond": {"$and": [
                        {"$in": ["$$l.status", list(PRESENCE)]},
                        {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$$l.date"}}}, 2]},
                        {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$$l.date"}}}, 6]},
                    ]},
                }},
                "as": "l",
                "in": {"$cond": [{"$eq": ["$$l.half_day", True]}, 0.5, 1]},
            }}},
            "employee_hours_sum": {"$sum": {"$map": {
                "input": {"$filter": {
                    "input": "$logs", "as": "l",
                    "cond": {"$and": [
                        {"$in": ["$$l.status", list(PRESENCE)]},
                        {"$ne": ["$$l.work_hours", None]},
                    ]},
                }},
                "as": "l",
                "in": "$$l.work_hours",
            }}},
            "employee_hours_count": {"$size": {"$filter": {
                "input": "$logs", "as": "l",
                "cond": {"$and": [
                    {"$in": ["$$l.status", list(PRESENCE)]},
                    {"$ne": ["$$l.work_hours", None]},
                ]},
            }}},
            "employee_late_count": {"$size": {"$filter": {
                "input": "$logs", "as": "l",
                "cond": {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]},
            }}},
            "employee_late_minutes": {"$sum": {"$map": {
                "input": "$logs", "as": "l",
                "in": {"$ifNull": ["$$l.late_minutes", 0]},
            }}},
            "employee_leave_count": {"$size": {"$filter": {
                "input": "$logs", "as": "l",
                "cond": {"$eq": ["$$l.status", "LEAVE"]},
            }}},
            "employee_on_duty_count": {"$size": {"$filter": {
                "input": "$logs", "as": "l",
                "cond": {"$eq": ["$$l.status", "ON_DUTY"]},
            }}},
        }},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": "$employee_present_days"},
            "work_hours_sum": {"$sum": "$employee_hours_sum"},
            "work_hours_count": {"$sum": "$employee_hours_count"},
            "late_count": {"$sum": "$employee_late_count"},
            "total_late_minutes": {"$sum": "$employee_late_minutes"},
            "leave_count": {"$sum": "$employee_leave_count"},
            "on_duty_count": {"$sum": "$employee_on_duty_count"},
        }},
        {"$project": {
            "_id": 0, "department": "$_id", "headcount": 1, "present_days": 1,
            "avg_work_hours": {"$cond": [
                {"$gt": ["$work_hours_count", 0]},
                {"$divide": ["$work_hours_sum", "$work_hours_count"]},
                None,
            ]},
            "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1,
        }},
        {"$sort": {"department": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    start, end = parse_month(month)
    items = list(db.employees.aggregate(department_summary_pipeline(month, department), allowDiskUse=True))
    for item in items:
        item["present_days"] = decimal_round(item.get("present_days", 0), 2)
        item["avg_work_hours"] = decimal_round(item.get("avg_work_hours"), 2)
        for key in ("headcount", "late_count", "total_late_minutes", "leave_count", "on_duty_count"):
            item[key] = int(item.get(key) or 0)
    return {"month": month, "items": items}


def late_leaderboard_pipeline(month: str, limit: int, department: Optional[str] = None):
    start, end = date_bounds(month)
    lookup_pipeline = [{"$match": {"$expr": {"$eq": ["$emp_code", "$$code"]}}}]
    if department is not None:
        lookup_pipeline[0]["$match"]["department"] = department
    return [
        {"$match": {"date": {"$gte": start, "$lt": end}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "let": {"code": "$_id"}, "pipeline": lookup_pipeline, "as": "employee"}},
        {"$unwind": "$employee"},
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}},
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name",
            "department": "$employee.department", "total_late_minutes": 1, "late_count": 1,
        }},
    ]


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str,
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    parse_month(month)
    items = list(db.attendance_logs.aggregate(late_leaderboard_pipeline(month, limit, department), allowDiskUse=True))
    for item in items:
        item["rank"] = int(item["rank"])
        item["total_late_minutes"] = int(item["total_late_minutes"])
        item["late_count"] = int(item["late_count"])
    return {"month": month, "items": items}


def department_trend_pipeline(department: str, from_day: date, to_day: date):
    start_dt = datetime.combine(from_day, time.min)
    end_exclusive = datetime.combine(to_day + timedelta(days=1), time.min)
    return [
        {"$documents": [{"dateObj": start_dt}]},
        {"$densify": {"field": "dateObj", "range": {
            "step": 1, "unit": "day", "bounds": [start_dt, end_exclusive]
        }}},
        {"$set": {"date": {"$dateToString": {"date": "$dateObj", "format": "%Y-%m-%d", "timezone": "UTC"}}}},
        {"$lookup": {
            "from": "employees",
            "let": {"day": "$date"},
            "pipeline": [{"$match": {"$expr": {"$and": [
                {"$eq": ["$department", department]}, {"$lte": ["$joined_on", "$$day"]}
            ]}}}, {"$count": "n"}],
            "as": "headcount_result",
        }},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"day": "$date"},
            "pipeline": [
                {"$match": {"$expr": {"$eq": ["$date", "$$day"]}}},
                {"$lookup": {
                    "from": "employees",
                    "let": {"code": "$emp_code"},
                    "pipeline": [{"$match": {"$expr": {"$and": [
                        {"$eq": ["$emp_code", "$$code"]}, {"$eq": ["$department", department]}
                    ]}}}, {"$project": {"_id": 1}}],
                    "as": "employee_match",
                }},
                {"$match": {"employee_match.0": {"$exists": True}}},
                {"$group": {
                    "_id": None,
                    "present_count": {"$sum": {"$cond": [
                        {"$in": ["$status", list(PRESENCE)]},
                        {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]},
                        0,
                    ]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                }},
            ],
            "as": "daily_stats",
        }},
        {"$set": {
            "headcount": {"$ifNull": [{"$arrayElemAt": ["$headcount_result.n", 0]}, 0]},
            "present_count": {"$ifNull": [{"$arrayElemAt": ["$daily_stats.present_count", 0]}, 0]},
            "late_count": {"$ifNull": [{"$arrayElemAt": ["$daily_stats.late_count", 0]}, 0]},
            "is_working_day": {"$and": [
                {"$gte": [{"$dayOfWeek": "$dateObj"}, 2]},
                {"$lte": [{"$dayOfWeek": "$dateObj"}, 6]},
            ]},
        }},
        {"$set": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$divide": ["$present_count", "$headcount"]},
            None,
        ]}}},
        {"$setWindowFields": {
            "sortBy": {"dateObj": 1},
            "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}},
        }},
        {"$project": {
            "_id": 0, "date": 1, "is_working_day": 1, "headcount": 1,
            "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1,
        }},
        {"$sort": {"date": 1}},
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(...)):
    from_day = parse_iso_date(from_, "from")
    to_day = parse_iso_date(to, "to")
    if to_day < from_day:
        raise HTTPException(422, "to must be on or after from")
    if (to_day - from_day).days + 1 > 92:
        raise HTTPException(422, "date range cannot exceed 92 days")
    if not db.employees.find_one({"department": department}, {"_id": 1}):
        raise HTTPException(404, "department not found")
    pipeline = department_trend_pipeline(department, from_day, to_day)
    items = list(db.aggregate(pipeline, allowDiskUse=True))
    for item in items:
        item["headcount"] = int(item.get("headcount") or 0)
        item["present_count"] = decimal_round(item.get("present_count", 0), 2)
        item["late_count"] = int(item.get("late_count") or 0)
        item["attendance_rate"] = decimal_round(item.get("attendance_rate"), 4)
        item["moving_avg_7d"] = decimal_round(item.get("moving_avg_7d"), 4)
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Explain endpoint: executeStats explain of the same endpoint pipeline/query.
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
):
    allowed = {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}
    if endpoint not in allowed:
        raise HTTPException(422, "unsupported endpoint")
    if endpoint == "attendance_list":
        if date_from:
            parse_iso_date(date_from, "date_from")
        if date_to:
            parse_iso_date(date_to, "date_to")
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from must be on or before date_to")
        query = {}
        if emp_code is not None:
            query["emp_code"] = emp_code
        if date_from or date_to:
            query["date"] = {}
            if date_from:
                query["date"]["$gte"] = date_from
            if date_to:
                query["date"]["$lte"] = date_to
        if status is not None:
            if status not in ALL_STATUSES:
                raise HTTPException(422, "invalid status")
            query["status"] = status
        command = {"find": "attendance_logs", "filter": query, "sort": {"date": -1, "emp_code": 1}, "skip": (page - 1) * page_size, "limit": page_size}
        collection = "attendance_logs"
    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required")
        parse_month(month)
        command = {"aggregate": "employees", "pipeline": employee_monthly_pipeline(emp_code, month), "cursor": {}}
        collection = "employees"
    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")
        parse_month(month)
        command = {"aggregate": "employees", "pipeline": department_summary_pipeline(month, department), "cursor": {}}
        collection = "employees"
    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")
        parse_month(month)
        command = {"aggregate": "attendance_logs", "pipeline": late_leaderboard_pipeline(month, limit, department), "cursor": {}}
        collection = "attendance_logs"
    elif endpoint == "department_trend":
        if not department or not from_ or not to:
            raise HTTPException(422, "department, from and to are required")
        from_day = parse_iso_date(from_, "from")
        to_day = parse_iso_date(to, "to")
        if to_day < from_day or (to_day - from_day).days + 1 > 92:
            raise HTTPException(422, "invalid trend date range")
        if not db.employees.find_one({"department": department}, {"_id": 1}):
            raise HTTPException(404, "department not found")

        # Explain the indexed attendance-date query used inside the trend pipeline.
        # Atlas rejects explain commands for the collectionless $documents pipeline.
        command = {
            "find": "attendance_logs",
            "filter": {
                "date": {
                    "$gte": from_day.isoformat(),
                    "$lte": to_day.isoformat(),
                }
            },
            "sort": {"date": 1},
        }
        collection = "attendance_logs"

    try:
        explain = db.command("explain", command, verbosity="executionStats")
    except PyMongoError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"could not explain query: {exc.__class__.__name__}",
        )

    explain_json = json.loads(json.dumps(explain, default=str))
    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": explain_json,
    }
