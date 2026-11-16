"""Drop Day in the terminal: release the tickets and watch the rush on a live board.

    uv run python -m drop_day.demo.drop --fans 200 --seats 150 --bots 15 --gateway-flakiness 0.3

For the browser version, run `uv run python -m drop_day.demo.dashboard`.
"""

import argparse
import asyncio
from collections import Counter

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from temporalio.client import Client

from drop_day.temporal.boxoffice import NAMESPACE
from drop_day.demo.engine import HEADLINER, TOUR, DropRun

ROWS = [
    ("queued", "In the queue", "dim"),
    ("screening", "Screening", "cyan"),
    ("holding", "Holding a seat", "blue"),
    ("paying", "Paying", "yellow"),
    ("confirming", "Confirming", "magenta"),
    ("TICKETED", "Ticketed", "bold green"),
    ("SOLD_OUT", "Sold out", "dim"),
    ("BOT_BLOCKED", "Bots blocked", "red"),
    ("REFUNDED", "Refunded", "yellow"),
    ("PAYMENT_FAILED", "Payment failed", "red"),
    ("ERROR", "Workflow failed", "bold red"),
]


def feed_line(e: dict) -> str:
    if e["status"] == "TICKETED":
        retries = f" [dim](card retried {e['retries']}×)[/dim]" if e["retries"] else ""
        return f"[green]{e['name']}[/]  ticketed, {e['seat']}{retries}"
    if e["status"] == "BOT_BLOCKED":
        return f"[red]{e['name']}[/]  blocked as a bot"
    if e["status"] == "SOLD_OUT":
        return f"{e['name']}  sold out"
    return f"[yellow]{e['name']}[/]  {e['status'].lower().replace('_', ' ')}: {e['detail']}"


def render(run: DropRun):
    snap = run.snapshot()
    stages = Counter(f["stage"] for f in snap["fans"])
    total = len(snap["fans"]) or 1
    table = Table.grid(padding=(0, 2))
    table.add_column(justify="left")
    table.add_column(justify="right")
    table.add_column()
    for key, label, style in ROWS:
        n = stages.get(key, 0)
        table.add_row(f"[{style}]{label}", f"[{style}]{n}", f"[{style}]{'█' * round(40 * n / total)}")

    led = snap["ledger"]
    dup_style = "bold green" if led["duplicates"] == 0 else "bold red blink"
    audit = (
        f"[b]Auditor[/b]  charges: [b]{led['charges']}[/b]   "
        f"duplicate charges: [{dup_style}]{led['duplicates']}[/{dup_style}]   refunds: {led['refunds']}\n"
        f"Gateway chaos survived: {led['hiccups_503']} × 503, "
        f"{led['hiccups_lost']} × charged-but-response-lost"
    )
    feed_text = "\n".join(feed_line(e) for e in snap["feed"][-6:]) or "[dim]waiting for the first fans...[/dim]"
    return Panel(
        Group(table, "", Panel(audit, border_style="green"), "", feed_text),
        title=f"Drop Day: {HEADLINER}, {TOUR}",
        subtitle=f"{snap['drop_id']} · {len(snap['fans'])} fans · {snap['seats']} seats · {snap['elapsed']:5.1f}s",
        border_style="grey37",
    )


async def main() -> None:
    p = argparse.ArgumentParser(description="Release the tickets.")
    p.add_argument("--fans", type=int, default=200)
    p.add_argument("--seats", type=int, default=150)
    p.add_argument("--bots", type=int, default=15)
    p.add_argument("--gateway-flakiness", type=float, default=0.3,
                   help="Chance (0-1) that a charge attempt hits a gateway failure")
    p.add_argument("--hold-seconds", type=int, default=120,
                   help="How long a seat hold lasts before it expires on its own")
    args = p.parse_args()

    client = await Client.connect("localhost:7233", namespace=NAMESPACE)
    run = DropRun(client, args.fans, args.seats, args.bots, args.gateway_flakiness, args.hold_seconds)
    console = Console()
    await run.start()
    console.print(f"\nVenue manager opened [b]{run.drop_id}[/b] with {args.seats} seats "
                  f"[dim](Standalone Nexus Operation → seats.open_drop)[/dim]")
    console.print(f"[bold]Tickets for {HEADLINER} are on sale.[/]\n")

    with Live(render(run), console=console, refresh_per_second=4) as live:
        while not run.done:
            await asyncio.sleep(0.5)
            live.update(render(run))
        live.update(render(run))

    led = run.snapshot()["ledger"]
    ticketed = sum(f.stage == "TICKETED" for f in run.fans.values())
    console.print(
        f"\n[bold]Sold {ticketed}/{args.seats} seats. {led['charges']} charges, "
        f"{led['duplicates']} duplicates, {led['refunds']} refunds.[/bold]"
    )
    console.print("Web UI: http://localhost:8233 (switch Namespaces to see each team's side)\n")


if __name__ == "__main__":
    asyncio.run(main())
