"""Support desk: one-off actions on a single fan.

Each action is one Nexus Operation, so it runs as a Standalone Nexus
Operation straight from this script. No caller Workflow needed.

    uv run python -m drop_day.demo.support credit --drop drop-123 --fan fan-0007 --amount 20 --reason "queue melted"
    uv run python -m drop_day.demo.support list
"""

import argparse
import asyncio

from temporalio.client import Client

from drop_day.temporal.boxoffice import NAMESPACE
from drop_day.temporal.contracts import PAYMENTS_ENDPOINT, SEATS_ENDPOINT
from drop_day.demo.engine import issue_credit


async def credit(client: Client, drop_id: str, fan_id: str, dollars: float, reason: str) -> None:
    credit_id = await issue_credit(client, drop_id, fan_id, round(dollars * 100), reason)
    print(f"Credited {fan_id} ${dollars:.2f} ({credit_id}): {reason}")


async def list_ops(client: Client) -> None:
    query = f'Endpoint = "{PAYMENTS_ENDPOINT}" OR Endpoint = "{SEATS_ENDPOINT}"'
    print("Standalone Nexus Operations started from the boxoffice Namespace:")
    async for op in client.list_nexus_operations(query):
        print(f"  {op.status.name:<10} {op.endpoint:<18} {op.operation:<13} {op.operation_id}")


async def main() -> None:
    p = argparse.ArgumentParser(description="Support desk")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("credit", help="Give a fan a goodwill credit")
    c.add_argument("--drop", required=True)
    c.add_argument("--fan", required=True)
    c.add_argument("--amount", type=float, default=20)
    c.add_argument("--reason", default="Sorry about the queue")
    sub.add_parser("list", help="List Standalone Nexus Operations")
    args = p.parse_args()

    client = await Client.connect("localhost:7233", namespace=NAMESPACE)
    if args.cmd == "credit":
        await credit(client, args.drop, args.fan, args.amount, args.reason)
    else:
        await list_ops(client)


if __name__ == "__main__":
    asyncio.run(main())
