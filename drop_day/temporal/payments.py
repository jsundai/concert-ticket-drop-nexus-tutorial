"""Payments team: runs in the `payments` Namespace.

Every Operation here is backed by one Standalone Activity, with no Workflow.
`charge` calls a flaky card gateway that honors idempotency keys. The key is
the Activity ID: Temporal keeps it the same across every retry, and the Nexus
handler derives it from the Nexus request ID. So a fan is never charged twice,
even when the gateway charges the card and then drops the response.
"""

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import nexusrpc
from temporalio import activity, nexus
from temporalio.client import Client
from temporalio.common import RetryPolicy
from temporalio.worker import Worker

from drop_day.systems import card_gateway
from drop_day.temporal.contracts import (
    ChargeReceipt,
    ChargeRequest,
    CreditReceipt,
    CreditRequest,
    PaymentsService,
    RefundReceipt,
    RefundRequest,
)

NAMESPACE = "payments"
TASK_QUEUE = "payments-tq"


@activity.defn
def charge(req: ChargeRequest) -> ChargeReceipt:
    info = activity.info()
    # "Waiting on the bank": heartbeat so Temporal notices fast if this Worker dies.
    for step in range(4):
        activity.heartbeat(f"waiting on the bank ({step + 1}/4)")
        time.sleep(0.5)
    # A gateway error raises here, and the Retry Policy tries again with the same Activity ID.
    charge_id = card_gateway.charge(info.activity_id, req.drop_id, req.fan_id, req.amount_cents)
    return ChargeReceipt(charge_id=charge_id, attempts=info.attempt)


@activity.defn
def refund(req: RefundRequest) -> RefundReceipt:
    key = activity.info().activity_id
    refund_id = card_gateway.refund(key, req.drop_id, req.fan_id, req.charge_id, req.reason)
    return RefundReceipt(refund_id=refund_id)


@activity.defn
def issue_credit(req: CreditRequest) -> CreditReceipt:
    key = activity.info().activity_id
    credit_id = card_gateway.credit(key, req.drop_id, req.fan_id, req.amount_cents, req.reason)
    return CreditReceipt(credit_id=credit_id, amount_cents=req.amount_cents)


@nexusrpc.handler.service_handler(service=PaymentsService)
class PaymentsServiceHandler:
    @nexus.temporal_operation
    async def charge(
        self,
        ctx: nexus.TemporalStartOperationContext,
        client: nexus.TemporalNexusClient,
        input: ChargeRequest,
    ) -> nexus.TemporalOperationResult[ChargeReceipt]:
        return await client.start_activity(
            charge,
            input,
            # Derived from the Nexus request ID: a retried Nexus request reuses this Activity.
            id=f"charge-{input.drop_id}-{input.fan_id}-{ctx.request_id}",
            start_to_close_timeout=timedelta(seconds=30),
            heartbeat_timeout=timedelta(seconds=3),
            # Give up before the box office's 5-minute Operation timeout, so a charge
            # can't succeed after the fan has already been marked PAYMENT_FAILED.
            schedule_to_close_timeout=timedelta(minutes=4),
            retry_policy=RetryPolicy(initial_interval=timedelta(seconds=1), backoff_coefficient=1.5),
            summary=f"Charge {input.fan_id} ${input.amount_cents / 100:.2f}",
        )

    @nexus.temporal_operation
    async def refund(
        self,
        ctx: nexus.TemporalStartOperationContext,
        client: nexus.TemporalNexusClient,
        input: RefundRequest,
    ) -> nexus.TemporalOperationResult[RefundReceipt]:
        return await client.start_activity(
            refund,
            input,
            id=f"refund-{input.drop_id}-{input.fan_id}-{ctx.request_id}",
            start_to_close_timeout=timedelta(seconds=30),
            schedule_to_close_timeout=timedelta(minutes=4),
            summary=f"Refund {input.fan_id}",
        )

    @nexus.temporal_operation
    async def issue_credit(
        self,
        ctx: nexus.TemporalStartOperationContext,
        client: nexus.TemporalNexusClient,
        input: CreditRequest,
    ) -> nexus.TemporalOperationResult[CreditReceipt]:
        return await client.start_activity(
            issue_credit,
            input,
            id=f"credit-{input.drop_id}-{input.fan_id}-{ctx.request_id}",
            start_to_close_timeout=timedelta(seconds=30),
            # Give up before the support desk's 1-minute Operation timeout.
            schedule_to_close_timeout=timedelta(seconds=50),
            summary=f"Credit {input.fan_id} ${input.amount_cents / 100:.2f}: {input.reason}",
        )


async def main() -> None:
    client = await Client.connect("localhost:7233", namespace=NAMESPACE)
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        activities=[charge, refund, issue_credit],
        nexus_service_handlers=[PaymentsServiceHandler()],
        # The Activities are plain `def` functions, so they run on these threads.
        activity_executor=ThreadPoolExecutor(max_workers=100),
        max_concurrent_activities=100,
    )
    print(f"Payments Worker on {NAMESPACE}/{TASK_QUEUE}", flush=True)
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
