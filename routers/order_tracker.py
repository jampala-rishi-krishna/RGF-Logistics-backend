from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from auth.dependencies import CurrentUser, require_role
from database import get_db
from services import order_tracker as tracker

ACCESS = require_role("admin", "dispatcher", "sales", "wet_market")
router = APIRouter(prefix="/api/order-tracker", tags=["order-tracker"], dependencies=[Depends(ACCESS)])


@router.get("/orders")
def find_orders(q: str = Query("", max_length=120), date_from: date | None = None, date_to: date | None = None,
                status: str | None = Query(None, max_length=40), offset: int = Query(0, ge=0, le=10000),
                user: CurrentUser = Depends(ACCESS), db: Session = Depends(get_db)):
    return tracker.find_orders(user, q, date_from, date_to, status, db, offset)


@router.get("/orders/{number}")
def get_order(number: str, date_from: date | None = None, date_to: date | None = None,
              user: CurrentUser = Depends(ACCESS), db: Session = Depends(get_db)):
    return tracker.get_order(user, number[:200], date_from, date_to, db)


@router.get("/buyers-per-sku")
def buyers_per_sku(date_from: date | None = None, date_to: date | None = None, trip: str | None = Query(None, max_length=100),
                   user: CurrentUser = Depends(ACCESS), db: Session = Depends(get_db)):
    return tracker.buyers_per_sku(user, date_from, date_to, {"trip": trip}, db)
