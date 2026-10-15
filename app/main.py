"""Employee attendance & analytics API."""

import os
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
client = MongoClient(MONGO_URI)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
PRESENT_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
VALID_STATUSES = PRESENT_STATUSES | {"ABSENT", "LEAVE"}

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


@app.on_event("startup")
def ensure_indexes() -> None:
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index([("department", 1), ("joined_on", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", 1)])
    db.attendance_logs.create_index("date")


def ms_to_dt(value: Optional[int]) -> Optional[datetime]:
    if value is None:
        return None
    if not isinstance(value, int) or not (100_000_000_000 <= value <= 4_102_444_800_000):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Invalid epoch milliseconds: {value}")
    dt = datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    return dt.replace(microsecond=0)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.astimezone(timezone.utc).timestamp() * 1000)


def round_half_up(value: Decimal, digits: str) -> Decimal:
    return value.quantize(Decimal(digits), rounding=ROUND_HALF_UP)


def parse_hhmm(value: str) -> tuple[int, int]:
    if not re.fullmatch(r"\d{2}:\d{2}", value):
        raise ValueError(f"Invalid time: {value}")
    h, m = map(int, value.split(":"))
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"Invalid time: {value}")
    return h, m


def attendance_date_for_punch(emp: dict, punch_in_dt: datetime) -> str:
    local = punch_in_dt.astimezone(IST)
    shift_end_h, shift_end_m = parse_hhmm(emp["shift_end"])
    if emp["shift_end"] <= emp["shift_start"]:
        end_local = local.replace(hour=shift_end_h, minute=shift_end_m, second=0, microsecond=0)
        if local < end_local:
            return (local.date() - timedelta(days=1)).isoformat()
    return local.date().isoformat()


def shift_start_dt(emp: dict, attendance_date: str) -> datetime:
    h, m = parse_hhmm(emp["shift_start"])
    return datetime.fromisoformat(attendance_date).replace(hour=h, minute=m, tzinfo=IST)


def shift_end_dt(emp: dict, attendance_date: str) -> datetime:
    h, m = parse_hhmm(emp["shift_end"])
    end = datetime.fromisoformat(attendance_date).replace(hour=h, minute=m, tzinfo=IST)
    if emp["shift_end"] <= emp["shift_start"]:
        end += timedelta(days=1)
    return end


def compute_late_minutes(punch_in_dt: datetime, emp: dict, attendance_date: str) -> int:
    local = punch_in_dt.astimezone(IST)
    start = shift_start_dt(emp, attendance_date)
    delta_minutes = int((local - start).total_seconds() // 60)
    return delta_minutes if delta_minutes > 10 else 0


def compute_work_hours(punch_in_dt: datetime, punch_out_dt: datetime) -> float:
    total_hours = Decimal(str((punch_out_dt - punch_in_dt).total_seconds())) / Decimal("3600")
    return float(round_half_up(total_hours, "0.01"))


def compute_overtime_minutes(punch_out_dt: datetime, emp: dict, attendance_date: str) -> int:
    end_dt = shift_end_dt(emp, attendance_date).astimezone(timezone.utc)
    elapsed = (punch_out_dt.astimezone(timezone.utc) - end_dt).total_seconds()
    if elapsed < 0:
        return 0
    minutes = int(elapsed // 60)
    return minutes if minutes >= 30 else 0


def derive_record(doc: dict, emp: dict) -> dict:
    status = doc.get("status")
    punch_in = doc.get("punch_in")
    punch_out = doc.get("punch_out")
    if status in PRESENT_STATUSES and punch_in is not None:
        punch_in_dt = punch_in if punch_in.tzinfo else punch_in.replace(tzinfo=timezone.utc)
        attendance_date = doc.get("date") or attendance_date_for_punch(emp, punch_in_dt)
        doc["late_minutes"] = compute_late_minutes(punch_in_dt, emp, attendance_date)
        if punch_out is not None:
            punch_out_dt = punch_out if punch_out.tzinfo else punch_out.replace(tzinfo=timezone.utc)
            doc["work_hours"] = compute_work_hours(punch_in_dt, punch_out_dt)
            doc["overtime_minutes"] = compute_overtime_minutes(punch_out_dt, emp, attendance_date)
            doc["half_day"] = doc["work_hours"] < 4.50
        else:
            doc["work_hours"] = None
            doc["overtime_minutes"] = 0
            doc["half_day"] = False
    else:
        doc["late_minutes"] = 0
        doc["work_hours"] = None
        doc["overtime_minutes"] = 0
        doc["half_day"] = False
    return doc


def serialize_history(history: list[dict]) -> list[dict]:
    out = []
    for entry in history or []:
        changes = {}
        for field, value in (entry.get("changes") or {}).items():
            changes[field] = {
                "from": dt_to_ms(value.get("from")) if isinstance(value.get("from"), datetime) else value.get("from"),
                "to": dt_to_ms(value.get("to")) if isinstance(value.get("to"), datetime) else value.get("to"),
            }
        out.append({
            "at": dt_to_ms(entry.get("at")),
            "by": entry.get("by"),
            "reason": entry.get("reason"),
            "changes": changes,
        })
    return out


def serialize_employee(doc: dict) -> dict:
    result = dict(doc)
    result["created_at"] = dt_to_ms(result.get("created_at"))
    result.pop("_id", None)
    return result


def serialize_record(doc: dict) -> dict:
    return {
        "emp_code": doc.get("emp_code"),
        "date": doc.get("date"),
        "status": doc.get("status"),
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": serialize_history(doc.get("history", [])),
    }


def month_bounds(month: str) -> tuple[str, str]:
    dt = datetime.strptime(month, "%Y-%m")
    start = dt.strftime("%Y-%m-01")
    next_month = (dt.replace(day=28) + timedelta(days=4)).replace(day=1)
    end = (next_month - timedelta(days=1)).strftime("%Y-%m-%d")
    return start, end


class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value: str) -> str:
        date.fromisoformat(value)
        return value

    @model_validator(mode="after")
    def validate_shift_values(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def validate_epoch(cls, value: Optional[int]) -> Optional[int]:
        if value is None:
            return value
        if not isinstance(value, int) or not (100_000_000_000 <= value <= 4_102_444_800_000):
            raise ValueError("punched_at must be a valid epoch milliseconds integer")
        return value

    @field_validator("status")
    @classmethod
    def valid_status(cls, value: str) -> str:
        if value not in PRESENT_STATUSES:
            raise ValueError("status must be PRESENT, WFH or ON_DUTY")
        return value


class PunchOutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at")
    @classmethod
    def validate_epoch(cls, value: Optional[int]) -> Optional[int]:
        if value is None:
            return value
        if not isinstance(value, int) or not (100_000_000_000 <= value <= 4_102_444_800_000):
            raise ValueError("punched_at must be a valid epoch milliseconds integer")
        return value


class RegularizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)

    @field_validator("status")
    @classmethod
    def valid_status(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        if value not in VALID_STATUSES:
            raise ValueError("status is invalid")
        return value

    @field_validator("punch_in", "punch_out")
    @classmethod
    def validate_epoch(cls, value: Optional[int]) -> Optional[int]:
        if value is None:
            return value
        if not isinstance(value, int) or not (100_000_000_000 <= value <= 4_102_444_800_000):
            raise ValueError("timestamp must be a valid epoch milliseconds")
        return value


@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "MongoDB unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(409, "emp_code already exists")
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc)
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
    q: dict[str, Any] = {}
    if department is not None:
        q["department"] = department
    total = db.employees.count_documents(q)
    docs = list(db.employees.find(q).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size))
    return {"items": [serialize_employee(doc) for doc in docs], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    punch_in_dt = ms_to_dt(body.punched_at) if body.punched_at is not None else datetime.now(timezone.utc)
    punch_in_dt = punch_in_dt.replace(microsecond=0)
    attendance_date = attendance_date_for_punch(emp, punch_in_dt)
    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(punch_in_dt, emp, attendance_date),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        result = db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    doc["_id"] = result.inserted_id
    return serialize_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    punch_out_dt = ms_to_dt(body.punched_at) if body.punched_at is not None else datetime.now(timezone.utc)
    punch_out_dt = punch_out_dt.replace(microsecond=0)
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_dt}, "punch_out": None},
        sort=[("punch_in", -1)],
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    punch_in_dt = rec["punch_in"] if rec["punch_in"].tzinfo else rec["punch_in"].replace(tzinfo=timezone.utc)
    if punch_out_dt <= punch_in_dt:
        raise HTTPException(422, "punch_out must be after punch_in")
    if punch_out_dt - punch_in_dt > timedelta(hours=24):
        raise HTTPException(422, "punch_out is more than 24 hours after punch_in")

    rec["punch_out"] = punch_out_dt
    rec = derive_record(rec, emp)
    result = db.attendance_logs.update_one(
        {"_id": rec["_id"]},
        {"$set": {
            "punch_out": rec["punch_out"],
            "work_hours": rec["work_hours"],
            "overtime_minutes": rec["overtime_minutes"],
            "half_day": rec["half_day"],
            "late_minutes": rec["late_minutes"],
        }},
    )
    if result.matched_count == 0:
        raise HTTPException(409, "record already updated by another request")
    refreshed = db.attendance_logs.find_one({"_id": rec["_id"]})
    return serialize_record(refreshed)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q: dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "date_from cannot be after date_to")
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    total = db.attendance_logs.count_documents(q)
    docs = list(db.attendance_logs.find(q).sort([("date", -1), ("emp_code", 1)]).skip((page - 1) * page_size).limit(page_size))
    return {"items": [serialize_record(doc) for doc in docs], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    current = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if current is None:
        raise HTTPException(404, "attendance record not found")

    payload = body.model_dump(exclude_unset=True)
    updated = dict(current)
    status_value = payload.get("status", current.get("status"))
    punch_in_value = payload.get("punch_in")
    punch_out_value = payload.get("punch_out")

    if punch_in_value is not None:
        punch_in_value = ms_to_dt(punch_in_value)
    else:
        punch_in_value = current.get("punch_in")

    if punch_out_value is not None:
        punch_out_value = ms_to_dt(punch_out_value)
    else:
        punch_out_value = current.get("punch_out")

    if status_value in {"ABSENT", "LEAVE"}:
        if punch_in_value is not None or punch_out_value is not None:
            raise HTTPException(422, "ABSENT and LEAVE records cannot include punch times")
        updated["status"] = status_value
        updated["punch_in"] = None
        updated["punch_out"] = None
    else:
        if punch_in_value is None:
            raise HTTPException(422, "presence status requires punch_in")
        punch_in_value = punch_in_value if punch_in_value.tzinfo else punch_in_value.replace(tzinfo=timezone.utc)
        if punch_in_value.astimezone(IST).date().isoformat() != date:
            raise HTTPException(422, "punch_in must stay on the attendance date")
        updated["punch_in"] = punch_in_value
        if punch_out_value is not None:
            punch_out_value = punch_out_value if punch_out_value.tzinfo else punch_out_value.replace(tzinfo=timezone.utc)
            if punch_out_value <= punch_in_value:
                raise HTTPException(422, "punch_out must be after punch_in")
            if punch_out_value - punch_in_value > timedelta(hours=24):
                raise HTTPException(422, "punch_out is more than 24 hours after punch_in")
            updated["punch_out"] = punch_out_value
        else:
            updated["punch_out"] = current.get("punch_out")
        updated["status"] = status_value

    if set(payload) <= {"reason", "regularized_by"}:
        raise HTTPException(422, "request changes nothing")

    updated = derive_record(updated, emp)
    changes = {}
    for field in ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"):
        old = current.get(field)
        new = updated.get(field)
        if old != new:
            changes[field] = {"from": old, "to": new}
    if not changes:
        raise HTTPException(422, "request changes nothing")

    history_entry = {
        "at": datetime.now(timezone.utc),
        "by": payload["regularized_by"],
        "reason": payload["reason"],
        "changes": {
            key: {
                "from": dt_to_ms(val["from"]) if isinstance(val["from"], datetime) else val["from"],
                "to": dt_to_ms(val["to"]) if isinstance(val["to"], datetime) else val["to"],
            }
            for key, val in changes.items()
        },
    }
    updated["history"] = (current.get("history") or []) + [history_entry]

    db.attendance_logs.update_one(
        {"_id": current["_id"]},
        {"$set": {
            "status": updated["status"],
            "punch_in": updated.get("punch_in"),
            "punch_out": updated.get("punch_out"),
            "work_hours": updated.get("work_hours"),
            "late_minutes": updated.get("late_minutes", 0),
            "overtime_minutes": updated.get("overtime_minutes", 0),
            "half_day": updated.get("half_day", False),
            "history": updated["history"],
        }},
    )
    refreshed = db.attendance_logs.find_one({"_id": current["_id"]})
    return serialize_record(refreshed)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    start, end = month_bounds(month)
    joined_on = date.fromisoformat(employee["joined_on"])
    month_start = date.fromisoformat(start)
    month_end = date.fromisoformat(end)
    working_days = 0
    cursor = month_start
    while cursor <= month_end:
        if cursor.weekday() < 5 and cursor >= joined_on:
            working_days += 1
        cursor += timedelta(days=1)

    agg = list(db.attendance_logs.aggregate([
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [{"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}, 1, 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": "$late_minutes"},
            "total_overtime_minutes": {"$sum": "$overtime_minutes"},
        }},
    ]))
    summary = agg[0] if agg else {"present_days": 0, "leave_days": 0, "late_count": 0, "total_late_minutes": 0, "total_overtime_minutes": 0}
    present_days = float(summary.get("present_days", 0))
    attendance_pct = None if working_days == 0 else float((Decimal(str(present_days)) / Decimal(str(working_days)) * Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": summary.get("leave_days", 0),
        "late_count": summary.get("late_count", 0),
        "total_late_minutes": summary.get("total_late_minutes", 0),
        "total_overtime_minutes": summary.get("total_overtime_minutes", 0),
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), department: Optional[str] = None):
    start, end = month_bounds(month)
    q: dict[str, Any] = {"joined_on": {"$lte": end}}
    if department is not None:
        q["department"] = department
    employees = list(db.employees.find(q, {"_id": 0, "emp_code": 1, "department": 1}).sort("department", 1))
    if not employees:
        return {"month": month, "items": []}

    by_department: dict[str, list[str]] = {}
    for emp in employees:
        by_department.setdefault(emp["department"], []).append(emp["emp_code"])

    items = []
    for dept_name in sorted(by_department):
        dept_codes = by_department[dept_name]
        agg = list(db.attendance_logs.aggregate([
            {"$match": {"emp_code": {"$in": dept_codes}, "date": {"$gte": start, "$lte": end}}},
            {"$group": {
                "_id": None,
                "present_days": {"$sum": {"$cond": [{"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}, 1, 0]}},
                "avg_work_hours": {"$avg": {"$cond": [{"$and": [{"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}, {"$ne": ["$work_hours", None]}]}, "$work_hours", None]}},
                "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": "$late_minutes"},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
            }},
        ]))
        result = agg[0] if agg else {"present_days": 0, "avg_work_hours": None, "late_count": 0, "total_late_minutes": 0, "leave_count": 0, "on_duty_count": 0}
        items.append({
            "department": dept_name,
            "headcount": len(dept_codes),
            "present_days": float(result.get("present_days", 0)),
            "avg_work_hours": None if result.get("avg_work_hours") is None else float(round_half_up(Decimal(str(result["avg_work_hours"])), "0.01")),
            "late_count": result.get("late_count", 0),
            "total_late_minutes": result.get("total_late_minutes", 0),
            "leave_count": result.get("leave_count", 0),
            "on_duty_count": result.get("on_duty_count", 0),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    start, end = month_bounds(month)
    match = {"date": {"$gte": start, "$lte": end}}
    if department is not None:
        employee_codes = [emp["emp_code"] for emp in db.employees.find({"department": department}, {"_id": 0, "emp_code": 1})]
        if not employee_codes:
            return {"month": month, "items": []}
        match["emp_code"] = {"$in": employee_codes}

    pipeline = [
        {"$match": match},
        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
        {"$group": {
            "_id": "$emp_code",
            "name": {"$first": "$employee.name"},
            "department": {"$first": "$employee.department"},
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
        }},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
    ]
    rows = list(db.attendance_logs.aggregate(pipeline))
    ranked = []
    prev_total = None
    rank = 0
    for idx, row in enumerate(rows):
        if prev_total is None or row["total_late_minutes"] != prev_total:
            rank = idx + 1
            prev_total = row["total_late_minutes"]
        ranked.append({
            "rank": rank,
            "emp_code": row["_id"],
            "name": row["name"],
            "department": row["department"],
            "total_late_minutes": row["total_late_minutes"],
            "late_count": row["late_count"],
        })
    return {"month": month, "items": ranked[:limit]}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str = Query(..., alias="from"), to_date: str = Query(..., alias="to")):
    if to_date < from_date:
        raise HTTPException(422, "to must be on or after from")
    day_count = (date.fromisoformat(to_date) - date.fromisoformat(from_date)).days + 1
    if day_count > 92:
        raise HTTPException(422, "trend range cannot exceed 92 days")
    if db.employees.find_one({"department": department}) is None:
        raise HTTPException(404, "department not found")

    employee_codes = [emp["emp_code"] for emp in db.employees.find({"department": department}, {"_id": 0, "emp_code": 1})]
    pipeline = [
        {"$match": {"emp_code": {"$in": employee_codes}, "date": {"$gte": from_date, "$lte": to_date}}},
        {"$group": {
            "_id": "$date",
            "present_count": {"$sum": {"$cond": [{"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}, {"$cond": ["$half_day", 0.5, 1]}, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
        }},
        {"$sort": {"_id": 1}},
    ]
    by_date = {row["_id"]: row for row in db.attendance_logs.aggregate(pipeline)}

    rows = []
    cursor = date.fromisoformat(from_date)
    while cursor <= date.fromisoformat(to_date):
        day_str = cursor.isoformat()
        headcount = db.employees.count_documents({"department": department, "joined_on": {"$lte": day_str}})
        daily = by_date.get(day_str, {"present_count": 0, "late_count": 0})
        present_count = float(daily.get("present_count", 0))
        attendance_rate = None
        if cursor.weekday() < 5 and headcount > 0:
            attendance_rate = float((Decimal(str(present_count)) / Decimal(str(headcount)) * Decimal("100")).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))
        rows.append({
            "date": day_str,
            "is_working_day": cursor.weekday() < 5,
            "headcount": headcount,
            "present_count": present_count,
            "late_count": daily.get("late_count", 0),
            "attendance_rate": attendance_rate,
            "moving_avg_7d": None,
        })
        cursor += timedelta(days=1)

    window = []
    for row in rows:
        if row["attendance_rate"] is not None:
            window.append(Decimal(str(row["attendance_rate"])))
            if len(window) > 7:
                window.pop(0)
            if window:
                row["moving_avg_7d"] = float((sum(window) / Decimal(len(window))).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))
    return {"department": department, "items": rows}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: Optional[int] = Query(None, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        q: dict[str, Any] = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status
        explain = db.attendance_logs.find(q).sort([("date", -1), ("emp_code", 1)]).skip((page - 1) * page_size).limit(page_size).explain()
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    if endpoint == "employee_monthly":
        if emp_code is None or month is None:
            raise HTTPException(422, "emp_code and month are required")
        start, end = month_bounds(month)
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},
            {"$group": {"_id": None, "present_days": {"$sum": 1}}},
        ]
        explain = db.attendance_logs.aggregate(pipeline).explain()
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    if endpoint == "department_summary":
        if month is None:
            raise HTTPException(422, "month is required")
        start, end = month_bounds(month)
        pipeline = [
            {"$match": {"date": {"$gte": start, "$lte": end}}},
            {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
            {"$unwind": "$employee"},
            {"$match": {"employee.department": department} if department is not None else {"employee": {"$exists": True}}},
            {"$group": {"_id": "$employee.department", "present_days": {"$sum": 1}}},
        ]
        explain = db.attendance_logs.aggregate(pipeline).explain()
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    if endpoint == "late_leaderboard":
        if month is None:
            raise HTTPException(422, "month is required")
        start, end = month_bounds(month)
        pipeline = [
            {"$match": {"date": {"$gte": start, "$lte": end}}},
            {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
            {"$unwind": "$employee"},
            {"$match": {"employee.department": department} if department is not None else {"employee": {"$exists": True}}},
            {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}}},
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
            {"$limit": limit or 10},
        ]
        explain = db.attendance_logs.aggregate(pipeline).explain()
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    if endpoint == "department_trend":
        if department is None or from_date is None or to_date is None:
            raise HTTPException(422, "department, from and to are required")
        pipeline = [
            {"$match": {"emp_code": {"$in": [doc["emp_code"] for doc in db.employees.find({"department": department}, {"_id": 0, "emp_code": 1})]}, "date": {"$gte": from_date, "$lte": to_date}}},
            {"$group": {"_id": "$date", "present_count": {"$sum": 1}}},
        ]
        explain = db.attendance_logs.aggregate(pipeline).explain()
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    raise HTTPException(404, "unknown endpoint")


@app.get("/")
def root():
    return {"status": "ok"}
