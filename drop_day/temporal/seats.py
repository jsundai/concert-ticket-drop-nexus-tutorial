"""Seats team: runs in the `seats` Namespace.

This team uses a different backing for each Operation, picked by what the work is:

  open_drop     one inventory write             → Standalone Activity
  hold_seat     a hold that lives until confirm  → start SeatHoldWorkflow, then a `reserve`
                or expiry                          Update on it
  confirm_seat  finish the hold                  → Workflow Update on the hold
  release_seat  give the seat back               → Signal to the hold
"""

import asyncio
from datetime import timedelta

import nexusrpc
from temporalio import nexus
from concurrent.futures import ThreadPoolExecutor

from temporalio.client import Client, WorkflowUpdateStage
from temporalio.common import RetryPolicy, WorkflowIDConflictPolicy
from temporalio.exceptions import ApplicationError
from temporalio.service import RPCError, RPCStatusCode
from temporalio.worker import Worker

from drop_day.temporal.contracts import DropOpened, OpenDropRequest, SeatDone, SeatHold, SeatRequest, SeatsService
from drop_day.temporal.seat_hold import SeatHoldWorkflow
from drop_day.temporal.seat_inventory import free_seat, mark_sold, open_drop, reserve_seat

NAMESPACE = "seats"
TASK_QUEUE = "seats-tq"


def hold_id(req: SeatRequest) -> str:
    return f"hold-{req.drop_id}-{req.fan_id}"


@nexusrpc.handler.service_handler(service=SeatsService)
class SeatsServiceHandler:
    @nexus.temporal_operation
    async def open_drop(
        self,
        ctx: nexus.TemporalStartOperationContext,
        client: nexus.TemporalNexusClient,
        input: OpenDropRequest,
    ) -> nexus.TemporalOperationResult[DropOpened]:
        return await client.start_activity(
            open_drop,
            input,
            id=f"open-{input.drop_id}-{ctx.request_id}",
            start_to_close_timeout=timedelta(seconds=30),
            # Give up before the venue manager's 1-minute Operation timeout.
            schedule_to_close_timeout=timedelta(seconds=50),
            retry_policy=RetryPolicy(initial_interval=timedelta(seconds=1)),
            summary=f"Open {input.drop_id} with {input.capacity} seats",
        )

    @nexus.temporal_operation
    async def hold_seat(
        self,
        ctx: nexus.TemporalStartOperationContext,
        client: nexus.TemporalNexusClient,
        input: SeatRequest,
    ) -> nexus.TemporalOperationResult[SeatHold]:
        # Start the hold Workflow (or reuse it on a retry). This is a plain client call, so the
        # Operation is backed by the `reserve` Update below, not by the Workflow.
        await client.client.start_workflow(
            SeatHoldWorkflow.run,
            input,
            id=hold_id(input),
            task_queue=TASK_QUEUE,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        )
        # Return once the Update is accepted instead of waiting for the seat inside the handler.
        # The Operation completes with the seat when the Update finishes.
        return await client.start_workflow_update(
            hold_id(input),
            SeatHoldWorkflow.reserve,
            input,
            wait_for_stage=WorkflowUpdateStage.ACCEPTED,
            update_id=f"reserve-{hold_id(input)}",
        )

    @nexus.temporal_operation
    async def confirm_seat(
        self,
        ctx: nexus.TemporalStartOperationContext,
        client: nexus.TemporalNexusClient,
        input: SeatRequest,
    ) -> nexus.TemporalOperationResult[SeatDone]:
        try:
            return await client.start_workflow_update(
                hold_id(input),
                SeatHoldWorkflow.confirm,
                wait_for_stage=WorkflowUpdateStage.ACCEPTED,
                update_id=f"confirm-{hold_id(input)}",
            )
        except RPCError as err:
            if err.status != RPCStatusCode.NOT_FOUND:
                raise
            # The hold expired and its Workflow already closed.
            raise nexusrpc.OperationError(
                "Seat hold expired", state=nexusrpc.OperationErrorState.FAILED
            ) from ApplicationError("Seat hold expired", type="HoldExpired", non_retryable=True)

    @nexus.temporal_operation
    async def release_seat(
        self,
        ctx: nexus.TemporalStartOperationContext,
        client: nexus.TemporalNexusClient,
        input: SeatRequest,
    ) -> nexus.TemporalOperationResult[SeatDone]:
        try:
            await client.client.get_workflow_handle(hold_id(input)).signal(SeatHoldWorkflow.release)
        except RPCError as err:
            # A hold that already closed has freed its seat, so there is nothing to release.
            if err.status != RPCStatusCode.NOT_FOUND:
                raise
        return nexus.TemporalOperationResult.sync(SeatDone(seat=""))


async def main() -> None:
    client = await Client.connect("localhost:7233", namespace=NAMESPACE)
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[SeatHoldWorkflow],
        activities=[open_drop, reserve_seat, mark_sold, free_seat],
        nexus_service_handlers=[SeatsServiceHandler()],
        # The Activities are plain `def` functions, so they run on these threads.
        activity_executor=ThreadPoolExecutor(max_workers=100),
        max_concurrent_activities=100,
    )
    print(f"Seats Worker on {NAMESPACE}/{TASK_QUEUE}", flush=True)
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
