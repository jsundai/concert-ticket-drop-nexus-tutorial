# Drop Day: learn Temporal Nexus by selling concert tickets

You know that moment when tickets go on sale, the site falls over, and half your friends get charged twice? This app
recreates it on purpose.

Drop Day sells tickets for a made-up band, Ziggy & The Durable Executions. A hundred and fifty fans hit the box office at
the same second, some scalper bots sneak into the line, and the card processor fails on about 30% of attempts. Then you
get to pull the plug on the payments system while people are mid-checkout and watch what happens.

Spoiler: everyone who paid gets a seat, nobody who lost a seat keeps paying, and the duplicate-charge counter stays at
zero. The rest of this page shows you how that works with [Temporal Nexus](https://docs.temporal.io/nexus).

![The Drop Day dashboard after a drop](docs/dashboard.png)

## Get it running

It takes about two minutes. You'll need the [Temporal CLI](https://docs.temporal.io/cli/setup-cli) (version 1.9.1 or
later) and [uv](https://docs.astral.sh/uv/), which handles Python and the dependencies for you.

Start a local Temporal server in one terminal and leave it running:

```bash
./start-server.sh
```

In a second terminal, set up the Namespaces and Nexus Endpoints, then start the dashboard:

```bash
./setup.sh
uv run python -m drop_day.demo.dashboard
```

Open http://localhost:8080. The dashboard starts all the Workers for you, so that's everything.

(The local server forgets everything when it stops, so run `./setup.sh` again after a restart.)

## Let me show you something

Press **Drop tickets** and keep your eye on the seat map. Open seats turn blue when a fan grabs one and green once
they've paid. The bar across the top counts fans at every step, and the column on the left shows what each team is
working on.

Now comes the fun part. Start another drop, and while fans are paying, hit **Stop Worker** under Payments. You just
killed the process that charges cards, exactly as if its server died. Payments goes red, the seats stay blue, and
everybody's stuck.

![Payments stopped in the middle of a drop](docs/dashboard-worker-down.png)

Hit **Start Worker**. Give it a few seconds, and the stalled charges run again on the new Worker. Now look at the ledger
in the bottom left: duplicate charges, zero. That's with a card processor that fails on purpose, including the nasty
case where it charges the card and then loses the response, which is how people usually end up paying twice.

Want to try a harder one? Set **Hold** to 10 seconds, start a drop, stop Payments, and count to fifteen before you start it
again. Watch the blue seats go back to open by themselves, because the holds ran out. When Payments comes back, those
fans get charged, find out their seat's gone, and get their money back automatically. Nobody wrote a cleanup job for
that.

A couple more things to click on before you go. **Credit $20** next to any fan sends them a goodwill credit straight
from the dashboard. And if you open the Temporal Web UI at http://localhost:8233 and switch Namespaces at the top left,
you'll see that each team only sees its own side of the story.

## So what just happened?

There are four teams in this app, and each one has its own Temporal Namespace, the way different teams at a real
company would. The box office sells tickets, the bouncer keeps bots out, Seats knows which seats are taken, and
Payments charges cards.

Here's the important bit: the box office never touches the other teams' code. It calls them through **Nexus**. Each
team publishes a short menu of things it can do, called Nexus Operations. Payments, for instance, offers `charge`,
`refund`, and `issue_credit`. The box office orders off those menus through a **Nexus Endpoint**, which routes each call
to the right team. The menus are the only thing the teams share, and they all fit in one file,
`drop_day/temporal/contracts.py`.

### Follow one fan through

Every fan gets their own `TicketPurchaseWorkflow` in the box office. It asks the bouncer whether the fan is human, asks
Seats to hold a seat, asks Payments to charge the card, and asks Seats to lock the seat in. If the seat's gone by the
time the card clears, it asks Payments for a refund.

Every one of those requests is a Nexus Operation. The cool part is that each team decides, on its own, what runs behind
each one.

### Most Operations are just one Activity

Checking for a bot is one quick check, and charging a card is one call to the card processor. Neither needs a Workflow
behind it, so each of those Operations runs a single Activity and finishes when the Activity does. Temporal calls this
a [Nexus Standalone Activity](https://docs.temporal.io/nexus/standalone-activity).

You still get the Temporal safety net. If the card processor errors out, the Activity retries. If the Payments Worker
dies mid-charge, its heartbeats stop, Temporal notices about 3 seconds later, and the charge runs again as soon as a Worker
is back.

And the reason nobody pays twice fits in one line of `payments.py`. The card processor accepts an idempotency key, and
the app passes it the Activity's ID. That ID stays the same across every retry, so a retry looks to the processor like
the same request, and it doesn't charge again.

### Holding a seat takes a Workflow

A seat hold is different, because it has to wait. Either the fan finishes paying, or time runs out and the seat goes
back on sale. Waiting is exactly what Workflows are good at, so Seats backs `hold_seat` with a `SeatHoldWorkflow`. The
fan gets their seat number immediately, and the hold keeps ticking in the background with its own timer. When the box
office is ready to lock the seat in, `confirm_seat` sends that hold an Update.

That's why the holds expired on their own in the harder demo, even with the rest of the purchase stuck. The hold never
depended on anyone else remembering to let go of the seat.

### And some calls skip the Workflow completely

Opening the drop is one call the venue manager makes before doors open. Giving a fan a $20 credit is one call from the
support desk. A Workflow wouldn't add anything to either one, so the app makes them as **Standalone Nexus Operations**,
where the Temporal Client calls the Operation directly. Both live in `drop_day/demo/engine.py`. The Web UI may not list
them yet, but this command will:

```bash
temporal nexus operation list --namespace boxoffice
```

## Where to look in the code

Head straight for `drop_day/temporal/`, because every line of Temporal code lives there. The other two folders are
props: `systems/` pretends to be the outside world (the flaky card processor and a seat database), and `demo/` is the
dashboard plus a couple of scripts.

Inside `drop_day/temporal/`, here's the order I'd read things in:

- `contracts.py` has the menus, so start there.
- `boxoffice.py` has the `TicketPurchaseWorkflow` that calls everyone else.
- `payments.py` and `bouncer.py` have the Operations backed by a single Activity.
- `seats.py` is worth slowing down for, because its Operations are backed four different ways: an Activity, a Workflow,
  an Update, and a Signal.
- `seat_hold.py` has the `SeatHoldWorkflow` and its timer.
- `seat_inventory.py` has the small Activities the hold Workflow uses.

## Prefer the terminal?

You can skip the dashboard. Start each team's Worker in its own window:

```bash
uv run python -m drop_day.temporal.bouncer
uv run python -m drop_day.temporal.seats
uv run python -m drop_day.temporal.payments
uv run python -m drop_day.temporal.boxoffice
```

Then run a drop with a live text view:

```bash
uv run python -m drop_day.demo.drop --fans 200 --seats 150 --bots 15
```

To break things here, press Ctrl+C in a Worker's window and then start it again. The support desk works from the
command line too:

```bash
uv run python -m drop_day.demo.support credit --drop drop-123456 --fan fan-0007 --amount 20
```

Each team keeps its data in SQLite files under `data/`. Delete that folder whenever you want a clean slate.

---

The dashboard uses the Geist and Geist Mono fonts under the SIL Open Font License
(see `drop_day/demo/static/fonts/Geist-OFL.txt`).
