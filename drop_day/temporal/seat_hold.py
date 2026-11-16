"""Seats team: SeatHoldWorkflow, one per fan per drop.

A hold is a process, not a single step: it reserves a seat, then waits for a
`confirm` Update or a `release` Signal. If neither arrives before the hold
time runs out, the Workflow frees the seat by itself, even if the fan's
Workflow in the box office crashed.
"""

import asyncio
from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from drop_day.temporal.contracts import SeatDone, SeatHold, SeatRequest
    from drop_day.temporal.seat_inventory import ReservedSeat, free_seat, mark_sold, reserve_seat

ACTIVITY_TIMEOUT = timedelta(seconds=10)


@dataclass
class HoldOutcome:
    fan_id: str
    seat: str
    status: str  # SOLD, EXPIRED, RELEASED, SOLD_OUT


def _failure_type(err: BaseException) -> str:
    cur: BaseException | None = err
    while cur is not None:
        if isinstance(cur, ApplicationError) and cur.type:
            return cur.type
        cur = cur.__cause__
    return ""


@workflow.defn
class SeatHoldWorkflow:
    def __init__(self) -> None:
        self._req: SeatRequest | None = None
        self._seat = ""
        self._hold_seconds = 0
        # new → held → confirming → confirmed, or held → released / expired, or new → sold_out
        self._state = "new"

    @workflow.run
    async def run(self, req: SeatRequest) -> HoldOutcome:
        self._req = req
        await workflow.wait_condition(lambda: self._state != "new")
        if self._state == "sold_out":
            return HoldOutcome(req.fan_id, "", "SOLD_OUT")

        try:
            await workflow.wait_condition(
                lambda: self._state != "held", timeout=timedelta(seconds=self._hold_seconds)
            )
        except asyncio.TimeoutError:
            self._state = "expired"

        if self._state in ("expired", "released"):
            await workflow.execute_activity(free_seat, req, start_to_close_timeout=ACTIVITY_TIMEOUT)
        else:
            await workflow.wait_condition(lambda: self._state == "confirmed")

        await workflow.wait_condition(workflow.all_handlers_finished)
        status = {"confirmed": "SOLD", "expired": "EXPIRED", "released": "RELEASED"}[self._state]
        return HoldOutcome(req.fan_id, self._seat, status)

    @workflow.update
    async def reserve(self, req: SeatRequest) -> SeatHold:
        if self._state != "new":
            if self._seat:
                return SeatHold(self._seat)
            raise ApplicationError("Sold out", type="SoldOut", non_retryable=True)
        try:
            reserved: ReservedSeat = await workflow.execute_activity(
                reserve_seat, req, start_to_close_timeout=ACTIVITY_TIMEOUT
            )
        except Exception as err:
            if _failure_type(err) == "SoldOut":
                self._state = "sold_out"
                raise ApplicationError("Sold out", type="SoldOut", non_retryable=True)
            raise
        self._seat, self._hold_seconds = reserved.seat, reserved.hold_seconds
        self._state = "held"
        return SeatHold(self._seat)

    @workflow.update
    async def confirm(self) -> SeatDone:
        if self._state != "held":
            raise ApplicationError(f"Hold is {self._state}, not held", type="HoldExpired", non_retryable=True)
        self._state = "confirming"
        assert self._req is not None
        await workflow.execute_activity(mark_sold, self._req, start_to_close_timeout=ACTIVITY_TIMEOUT)
        self._state = "confirmed"
        return SeatDone(self._seat)

    @workflow.signal
    def release(self) -> None:
        if self._state == "held":
            self._state = "released"

    @workflow.query
    def state(self) -> str:
        return self._state
