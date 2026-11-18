"""Seats team: inventory Activities.

`open_drop` backs a Nexus Operation directly (a Nexus Standalone Activity).
The rest run inside SeatHoldWorkflow. Each one is a single call to the
venue's seat database, turned into a durable, retried step.
"""

from dataclasses import dataclass

from temporalio import activity
from temporalio.exceptions import ApplicationError

from drop_day.systems import seat_db
from drop_day.temporal.contracts import DropOpened, OpenDropRequest, SeatRequest


@dataclass
class ReservedSeat:
    seat: str
    hold_seconds: int


@activity.defn
def open_drop(req: OpenDropRequest) -> DropOpened:
    seats = seat_db.open_drop(req.drop_id, req.capacity, req.hold_seconds)
    return DropOpened(drop_id=req.drop_id, seats=seats)


@activity.defn
def reserve_seat(req: SeatRequest) -> ReservedSeat:
    try:
        seat, hold_seconds = seat_db.reserve(req.drop_id, req.fan_id)
    except seat_db.SoldOut:
        # Retrying won't create seats, so tell Temporal not to retry.
        raise ApplicationError("Sold out", type="SoldOut", non_retryable=True)
    except seat_db.DropNotOpen as err:
        raise ApplicationError(str(err), type="DropNotOpen", non_retryable=True)
    return ReservedSeat(seat, hold_seconds)


@activity.defn
def mark_sold(req: SeatRequest) -> None:
    seat_db.mark_sold(req.drop_id, req.fan_id)


@activity.defn
def free_seat(req: SeatRequest) -> None:
    seat_db.free(req.drop_id, req.fan_id)
