# Drop Day: learn Temporal Nexus by selling concert tickets

Drop Day is a ticket sale that goes wrong on purpose. Fans rush in, bots sneak into the line, and you get to crash the
payment system mid-checkout. This is a Temporal demo using [Nexus](https://docs.temporal.io/nexus).

![The Drop Day dashboard after a drop](docs/dashboard.png)

## Run it

You'll need the [Temporal CLI](https://docs.temporal.io/cli/setup-cli) (1.9.1 or later) and
[uv](https://docs.astral.sh/uv/).

```bash
./start-server.sh                          # terminal 1: local Temporal server
./setup.sh                                 # terminal 2: create the Namespaces and Nexus Endpoints
uv run python -m drop_day.demo.dashboard   # terminal 2: the app
```

Open http://localhost:8080 and press **Drop tickets**. Seats turn blue when a fan holds them and green once the fan
has paid.

Run `./setup.sh` again whenever you restart the server, because it doesn't keep anything.

This Drop Day app includes several ways of using Nexus, including:

| Choice | Used for | Why |
| --- | --- | --- |
| **Caller Workflow** | `TicketPurchaseWorkflow`, one per fan | Several Operations in a row, plus a refund saga |
| **Standalone Nexus Operation** | Venue manager's `open_drop`, support desk's `issue_credit` | One call from a script or UI backend, so no Workflow is needed |
| **Activity-backed Operation** ([Nexus Standalone Activity](https://docs.temporal.io/nexus/standalone-activity)) | `screen_fan`, `charge`, `refund`, `issue_credit`, `open_drop` | One durable step each, with no Workflow behind it |
| **Workflow-backed Operation** | `hold_seat`, `confirm_seat`, `release_seat` | A seat hold waits for a confirm or a timeout, so it needs a Workflow |

## Break it

Start a drop, then try one of these:

1. Stop the Payments Worker while fans are paying, then start it again. The seats wait until
   Payments is back and then sell, and the ledger still shows 0 duplicate charges.
2. Slide the card failure rate to 80%. The drop slows down, but there are still 0 duplicate charges.
3. Set **Hold** to 10 and stop Payments for 15 seconds. Unpaid seats free themselves, and those fans get refunded.
4. Stop the Seats Worker for 10 seconds. Checkout stalls for about a minute, because the Nexus
   [circuit breaker](https://docs.temporal.io/nexus/operations#circuit-breaking) pauses calls to Seats for 60 seconds.

## Architecture

The app has four teams, each with its own Namespace, and the box office calls the other three through Nexus Endpoints.

```
                        ┌─ bouncer-endpoint ──▶  bouncer   screen_fan ─────────────▶ Activity
 boxoffice              │
 TicketPurchaseWorkflow ├─ seats-endpoint ────▶  seats     hold_seat ──────────────▶ SeatHoldWorkflow (start, then Update)
 (one per fan)          │                                  confirm_seat ───────────▶ Update on the hold
                        │                                  release_seat ───────────▶ Signal to the hold
                        │                                  open_drop ──────────────▶ Activity
                        └─ payments-endpoint ─▶  payments  charge / refund / issue_credit ─▶ Activity

 Venue manager, support desk ── Standalone Nexus Operations (no Workflow) ──▶ seats.open_drop, payments.issue_credit
```

Here's why the demo survives what you threw at it:

| What happens | What handles it |
| --- | --- |
| The card processor fails | The `charge` Activity retries |
| The card is charged but the response is lost | The retry reuses the same Activity ID, so the card processor doesn't charge again |
| The Payments Worker dies mid-charge | Heartbeats stop, and the charge reruns once a Worker is back |
| A bot shows up | `screen_fan` blocks it without retrying |
| A hold runs out before the fan pays | `SeatHoldWorkflow`'s timer frees the seat, and the box office refunds the fan |
| The Seats Worker is down | Calls time out, and the circuit breaker pauses them for 60 seconds |

## See it in the Web UI

Open http://localhost:8233 and switch Namespaces at the top left.

- `boxoffice` has one `TicketPurchaseWorkflow` per fan.
- `seats` has one `SeatHoldWorkflow` per hold.
- `payments` and `bouncer` only have Activities.
- During Break it step 4, a stuck fan's Nexus Operation shows as Blocked because the circuit breaker is open.

To list the Standalone Nexus Operations, run `temporal nexus operation list --namespace boxoffice`.

## Where the Temporal code is

All of it is in [`drop_day/temporal/`](drop_day/temporal), and the other folders are only there to support the demo.

```
drop_day/
  temporal/   Nexus contracts, Workflows, Activities, Nexus handlers, Workers
  systems/    A pretend card processor and seat database, with no Temporal code
  demo/       The dashboard and scripts
```

| File | What it is |
| --- | --- |
| `contracts.py` | The Operations each team offers, and the best place to start |
| `boxoffice.py` | `TicketPurchaseWorkflow`, which calls the other teams |
| `bouncer.py` | Bot screening, backed by one Activity |
| `payments.py` | Charge, refund, and credit, each backed by one Activity |
| `seats.py` | Seat Operations, each backed a different way |
| `seat_hold.py` | `SeatHoldWorkflow`, which holds a seat until it's confirmed or expires |
| `seat_inventory.py` | The Activities the hold uses |

## Without the dashboard

Start each Worker in its own terminal, then run a drop with a text view:

```bash
uv run python -m drop_day.temporal.bouncer
uv run python -m drop_day.temporal.seats
uv run python -m drop_day.temporal.payments
uv run python -m drop_day.temporal.boxoffice
uv run python -m drop_day.demo.drop
```
