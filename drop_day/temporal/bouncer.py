"""Bouncer team: runs in the `bouncer` Namespace.

`screen_fan` is a Nexus Operation backed by one Standalone Activity.
"""

import asyncio
import random
from datetime import timedelta

import nexusrpc
from temporalio import activity, nexus
from temporalio.client import Client
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError
from temporalio.worker import Worker

from drop_day.temporal.contracts import BouncerService, ScreenRequest, ScreenResult

NAMESPACE = "bouncer"
TASK_QUEUE = "bouncer-tq"

BOT_TELLS = ("bot", "scalp", ".exe", "resell")


@activity.defn
async def screen_fan(req: ScreenRequest) -> ScreenResult:
    await asyncio.sleep(random.uniform(0.2, 0.8))  # "Analyzing mouse movements..."
    if any(tell in req.name.lower() for tell in BOT_TELLS):
        # Retrying will not turn a bot into a fan.
        raise ApplicationError(f"{req.name} is a bot", type="BotDetected", non_retryable=True)
    return ScreenResult(fan_id=req.fan_id, verdict="human, probably")


@nexusrpc.handler.service_handler(service=BouncerService)
class BouncerServiceHandler:
    @nexus.temporal_operation
    async def screen_fan(
        self,
        ctx: nexus.TemporalStartOperationContext,
        client: nexus.TemporalNexusClient,
        input: ScreenRequest,
    ) -> nexus.TemporalOperationResult[ScreenResult]:
        return await client.start_activity(
            screen_fan,
            input,
            id=f"screen-{input.drop_id}-{input.fan_id}-{ctx.request_id}",
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=RetryPolicy(initial_interval=timedelta(seconds=1)),
            summary=f"Screen {input.name}",
        )


async def main() -> None:
    client = await Client.connect("localhost:7233", namespace=NAMESPACE)
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        activities=[screen_fan],
        nexus_service_handlers=[BouncerServiceHandler()],
    )
    print(f"Bouncer Worker on {NAMESPACE}/{TASK_QUEUE}", flush=True)
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
