"""Box office team: runs in the `boxoffice` Namespace.

TicketPurchaseWorkflow is the only Workflow in the whole app. It calls three
other teams through Nexus, and every one of those Operations is backed by a
single Standalone Activity on the other side.
"""

import asyncio
from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow
from temporalio.client import Client
from temporalio.exceptions import ApplicationError
from temporalio.worker import Worker

with workflow.unsafe.imports_passed_through():
    from drop_day.temporal.contracts import (
        BOUNCER_ENDPOINT,
        PAYMENTS_ENDPOINT,
        SEATS_ENDPOINT,
        BouncerService,
        ChargeRequest,
        PaymentsService,
        RefundRequest,
        ScreenRequest,
        SeatRequest,
        SeatsService,
    )

NAMESPACE = "boxoffice"
TASK_QUEUE = "boxoffice-tq"
OP_TIMEOUT = timedelta(minutes=5)


@dataclass
class Fan:
    drop_id: str
    fan_id: str
    name: str
    price_cents: int


@dataclass
class PurchaseResult:
    fan_id: str
    name: str
    status: str  # TICKETED, SOLD_OUT, BOT_BLOCKED, REFUNDED, PAYMENT_FAILED
    seat: str = ""
    charge_attempts: int = 0
    detail: str = ""


def failure_type(err: BaseException) -> tuple[str, str]:
    """Walk the cause chain to the ApplicationError the remote team raised.

    A synchronous Nexus handler's failure arrives wrapped in a generic
    `OperationError`, so skip that wrapper and keep looking.
    """
    cur: BaseException | None = err
    while cur is not None:
        if isinstance(cur, ApplicationError) and cur.type and cur.type != "OperationError":
            return cur.type, cur.message
        cur = cur.__cause__
    return "Unknown", str(err)


@workflow.defn
class TicketPurchaseWorkflow:
    def __init__(self) -> None:
        self._stage = "queued"

    @workflow.query
    def stage(self) -> str:
        return self._stage

    @workflow.run
    async def run(self, fan: Fan) -> PurchaseResult:
        bouncer = workflow.create_nexus_client(service=BouncerService, endpoint=BOUNCER_ENDPOINT)
        seats = workflow.create_nexus_client(service=SeatsService, endpoint=SEATS_ENDPOINT)
        payments = workflow.create_nexus_client(service=PaymentsService, endpoint=PAYMENTS_ENDPOINT)

        # 1. Bouncer team: are you a human?
        self._stage = "screening"
        try:
            await bouncer.execute_operation(
                BouncerService.screen_fan,
                ScreenRequest(fan.drop_id, fan.fan_id, fan.name),
                schedule_to_close_timeout=OP_TIMEOUT,
            )
        except Exception as err:
            kind, msg = failure_type(err)
            if kind == "BotDetected":
                return PurchaseResult(fan.fan_id, fan.name, "BOT_BLOCKED", detail=msg)
            raise

        # 2. Seats team: grab a seat before someone else does. The hold expires on its own.
        self._stage = "holding"
        seat_request = SeatRequest(fan.drop_id, fan.fan_id)
        try:
            hold = await seats.execute_operation(
                SeatsService.hold_seat, seat_request, schedule_to_close_timeout=OP_TIMEOUT
            )
        except Exception as err:
            kind, msg = failure_type(err)
            if kind == "SoldOut":
                return PurchaseResult(fan.fan_id, fan.name, "SOLD_OUT", detail=msg)
            raise

        # 3. Payments team: charge the card. Retries and idempotency live over there.
        self._stage = "paying"
        try:
            receipt = await payments.execute_operation(
                PaymentsService.charge,
                ChargeRequest(fan.drop_id, fan.fan_id, fan.price_cents),
                schedule_to_close_timeout=OP_TIMEOUT,
            )
        except Exception as err:
            _, msg = failure_type(err)
            await seats.execute_operation(
                SeatsService.release_seat, seat_request, schedule_to_close_timeout=OP_TIMEOUT
            )
            return PurchaseResult(fan.fan_id, fan.name, "PAYMENT_FAILED", detail=msg)

        # 4. Seats team: turn the hold into a ticket. If the hold expired, refund (saga).
        self._stage = "confirming"
        try:
            done = await seats.execute_operation(
                SeatsService.confirm_seat, seat_request, schedule_to_close_timeout=OP_TIMEOUT
            )
        except Exception:
            # Any confirm failure means the hold is gone: it expired, or its Workflow already closed.
            msg = "Seat hold expired"
            await payments.execute_operation(
                PaymentsService.refund,
                RefundRequest(fan.drop_id, fan.fan_id, receipt.charge_id, msg),
                schedule_to_close_timeout=OP_TIMEOUT,
            )
            return PurchaseResult(fan.fan_id, fan.name, "REFUNDED", hold.seat, receipt.attempts, msg)

        self._stage = "ticketed"
        return PurchaseResult(fan.fan_id, fan.name, "TICKETED", done.seat, receipt.attempts)


async def main() -> None:
    client = await Client.connect("localhost:7233", namespace=NAMESPACE)
    worker = Worker(client, task_queue=TASK_QUEUE, workflows=[TicketPurchaseWorkflow])
    print(f"Box office Worker on {NAMESPACE}/{TASK_QUEUE}", flush=True)
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
