"""Seats team: runs in the `seats` Namespace.

This team uses a different backing for each Operation, picked by what the work is:

  open_drop     one inventory write             → Standalone Activity
  hold_seat     a hold that lives until confirm  → SeatHoldWorkflow, via Update-with-Start
                or expiry                          (returns the seat right away)
  confirm_seat  finish the hold                  → Workflow Update on the hold
  release_seat  give the seat back               → Signal to the hold
"""

import asyncio
from datetime import timedelta

import nexusrpc
from temporalio import nexus
from temporalio.client import Client, WithStartWorkflowOperation, WorkflowUpdateFailedError, WorkflowUpdateStage
from temporalio.common import RetryPolicy, WorkflowIDConflictPolicy
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
        # Start the hold Workflow (or reuse it on a retry) and reserve a seat in one call.
        # The Operation completes with the seat now; the hold keeps running in the background.
        try:
            seat = await client.client.execute_update_with_start_workflow(
                SeatHoldWorkflow.reserve,
                input,
                id=f"reserve-{hold_id(input)}",
                start_workflow_operation=WithStartWorkflowOperation(
                    SeatHoldWorkflow.run,
                    input,
                    id=hold_id(input),
                    task_queue=TASK_QUEUE,
                    id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
                ),
            )
        except WorkflowUpdateFailedError as err:
            raise nexusrpc.OperationError(
                str(err.cause), state=nexusrpc.OperationErrorState.FAILED
            ) from err.cause
        return nexus.TemporalOperationResult.sync(seat)

    @nexus.temporal_operation
    async def confirm_seat(
        self,
        ctx: nexus.TemporalStartOperationContext,
        client: nexus.TemporalNexusClient,
        input: SeatRequest,
    ) -> nexus.TemporalOperationResult[SeatDone]:
        return await client.start_workflow_update(
            hold_id(input),
            SeatHoldWorkflow.confirm,
            wait_for_stage=WorkflowUpdateStage.ACCEPTED,
            update_id=f"confirm-{hold_id(input)}",
        )

    @nexus.temporal_operation
    async def release_seat(
        self,
        ctx: nexus.TemporalStartOperationContext,
        client: nexus.TemporalNexusClient,
        input: SeatRequest,
    ) -> nexus.TemporalOperationResult[SeatDone]:
        await client.client.get_workflow_handle(hold_id(input)).signal(SeatHoldWorkflow.release)
        return nexus.TemporalOperationResult.sync(SeatDone(seat=""))


async def main() -> None:
    client = await Client.connect("localhost:7233", namespace=NAMESPACE)
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[SeatHoldWorkflow],
        activities=[open_drop, reserve_seat, mark_sold, free_seat],
        nexus_service_handlers=[SeatsServiceHandler()],
    )
    print(f"Seats Worker on {NAMESPACE}/{TASK_QUEUE}", flush=True)
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
