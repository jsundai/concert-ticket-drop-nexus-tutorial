"""Shared drop engine for the terminal board (drop.py) and the web dashboard.

Opens a drop with a Standalone Nexus Operation, starts one
TicketPurchaseWorkflow per fan, and tracks every fan's stage.
"""

import asyncio
import random
import time
from dataclasses import asdict, dataclass, field
from datetime import timedelta

from temporalio.client import Client, WorkflowHandle

from drop_day.temporal.boxoffice import TASK_QUEUE, Fan, PurchaseResult, TicketPurchaseWorkflow
from drop_day.temporal.contracts import (
    PAYMENTS_ENDPOINT,
    SEATS_ENDPOINT,
    CreditRequest,
    OpenDropRequest,
    PaymentsService,
    SeatsService,
)
from drop_day.systems import card_gateway
from drop_day.systems.seat_db import seat_map
from drop_day.systems.store import write_chaos

HEADLINER = "Ziggy & The Durable Executions"
TOUR = "The Eternal Retry Tour"
PRICE_CENTS = 8950 + 3499  # face value + "convenience fee"

# Fans are named after people who made history in computing and math.
PIONEERS = [
    # Computing
    "Ada Lovelace", "Grace Hopper", "Jean Bartik", "Kathleen Booth", "Mary Kenneth Keller",
    "Frances Allen", "Margaret Hamilton", "Barbara Liskov", "Radia Perlman", "Adele Goldberg",
    "Lynn Conway", "Anita Borg", "Karen Spärck Jones", "Hedy Lamarr", "Jeannette Wing",
    "Maria Klawe", "Shafi Goldwasser", "Latanya Sweeney", "Marian Croak", "Kimberly Bryant",
    "Juliana Rotich",
    "Valerie Thomas", "Raye Montague", "Mary Golda Ross", "Alan Turing", "Clarence Ellis",
    "Mark Dean", "Jerry Lawson", "Raj Reddy", "Andrew Yao", "Edsger Dijkstra", "Donald Knuth",
    "Dennis Ritchie", "Tim Berners-Lee", "Vint Cerf", "Luis von Ahn", "Yukihiro Matsumoto",
    "Roy Clay", "Jesse Russell", "John Henry Thompson", "An Wang", "Ajay Bhatt",
    "Miguel de Icaza", "Hector Garcia-Molina", "Kwabena Boahen",
    # NASA's human computers
    "Katherine Johnson", "Dorothy Vaughan", "Mary Jackson", "Annie Easley", "Gladys West",
    # AI and robotics
    "Fei-Fei Li", "Timnit Gebru", "Joy Buolamwini", "Rediet Abebe", "Daphne Koller",
    "Ayanna Howard", "Cynthia Breazeal", "Ruzena Bajcsy", "Manuela Veloso", "Lotfi Zadeh",
    "Rana el Kaliouby", "Pardis Sabeti",
    "Jaime Carbonell",
    # Math
    "Emmy Noether", "Sophie Germain", "Sofya Kovalevskaya", "Maryam Mirzakhani",
    "Karen Uhlenbeck", "Ingrid Daubechies", "Evelyn Boyd Granville", "Fern Hunt",
    "Shakuntala Devi", "Srinivasa Ramanujan", "Muhammad al-Khwarizmi", "Benjamin Banneker",
    "David Blackwell", "J. Ernest Wilkins Jr.", "Terence Tao", "Manjul Bhargava", "Claude Shannon",
    "Elbert Frank Cox", "Omar Khayyam", "Hasan Ibn al-Haytham", "Avi Wigderson",
    "Seki Takakazu", "Kiyosi Itô", "Shing-Tung Yau", "Manindra Agrawal", "Narendra Karmarkar",
    "Prasanta Chandra Mahalanobis",
]
BOTS = ["TicketBot9000", "ScalpMaster3000", "BuyAllTheSeats.exe", "ResellRanger", "NotABot_bot",
        "ScalperKing", "QueueJumper.exe", "TotallyHumanBot"]

FINAL = {"TICKETED", "SOLD_OUT", "BOT_BLOCKED", "REFUNDED", "PAYMENT_FAILED", "ERROR"}


def make_fans(drop_id: str, n_fans: int, n_bots: int) -> list[Fan]:
    # Go through the whole list before anyone repeats.
    pool = random.sample(PIONEERS, len(PIONEERS))
    names = [pool[i % len(pool)] for i in range(n_fans - n_bots)]
    names += [f"{random.choice(BOTS)}-{random.randint(1, 99)}" for _ in range(n_bots)]
    random.shuffle(names)
    return [Fan(drop_id, f"fan-{i:04d}", name, PRICE_CENTS) for i, name in enumerate(names, 1)]


async def open_drop(client: Client, drop_id: str, capacity: int, hold_seconds: int) -> None:
    """The venue manager opens the drop: one call, so a Standalone Nexus Operation, no Workflow."""
    seats = client.create_nexus_client(service=SeatsService, endpoint=SEATS_ENDPOINT)
    await seats.execute_operation(
        SeatsService.open_drop,
        OpenDropRequest(drop_id, capacity, hold_seconds),
        id=f"open-{drop_id}",
        schedule_to_close_timeout=timedelta(minutes=1),
        summary=f"Venue manager opens {drop_id}",
    )


async def issue_credit(client: Client, drop_id: str, fan_id: str, cents: int, reason: str) -> str:
    """The support desk credits one fan: also a Standalone Nexus Operation."""
    payments = client.create_nexus_client(service=PaymentsService, endpoint=PAYMENTS_ENDPOINT)
    receipt = await payments.execute_operation(
        PaymentsService.issue_credit,
        CreditRequest(drop_id, fan_id, cents, reason),
        id=f"credit-{drop_id}-{fan_id}-{int(time.time() * 1000) % 10**8}",
        schedule_to_close_timeout=timedelta(minutes=1),
        summary=f"Support credit for {fan_id}",
    )
    return receipt.credit_id


def ledger(drop_id: str) -> dict:
    try:
        return card_gateway.ledger(drop_id)
    except Exception:
        return {"charges": 0, "duplicates": 0, "refunds": 0, "credits": 0, "hiccups_503": 0, "hiccups_lost": 0}


@dataclass
class FanView:
    fan_id: str
    name: str
    stage: str = "queued"
    seat: str = ""
    charge_attempts: int = 0
    detail: str = ""


@dataclass
class DropRun:
    client: Client
    n_fans: int
    n_seats: int
    n_bots: int
    flakiness: float
    hold_seconds: int
    drop_id: str = field(default_factory=lambda: f"drop-{int(time.time()) % 1_000_000}")
    fans: dict[str, FanView] = field(default_factory=dict)
    feed: list[dict] = field(default_factory=list)
    started: float = 0.0
    finished: float = 0.0

    @property
    def done(self) -> bool:
        return bool(self.fans) and all(f.stage in FINAL for f in self.fans.values())

    async def start(self) -> None:
        write_chaos(gateway_flakiness=self.flakiness)
        await open_drop(self.client, self.drop_id, self.n_seats, self.hold_seconds)
        fans = make_fans(self.drop_id, self.n_fans, self.n_bots)
        self.fans = {f.fan_id: FanView(f.fan_id, f.name) for f in fans}
        self.started = time.monotonic()
        sem = asyncio.Semaphore(50)

        async def start_one(fan: Fan) -> tuple[Fan, WorkflowHandle]:
            async with sem:
                h = await self.client.start_workflow(
                    TicketPurchaseWorkflow.run, fan, id=f"{self.drop_id}-{fan.fan_id}", task_queue=TASK_QUEUE
                )
                return fan, h

        pairs = await asyncio.gather(*(start_one(f) for f in fans))
        self._handles = pairs
        for fan, h in pairs:
            asyncio.create_task(self._await_result(fan, h))
        asyncio.create_task(self._poll_stages())

    async def _await_result(self, fan: Fan, h: WorkflowHandle) -> None:
        try:
            r: PurchaseResult = await h.result()
        except Exception as err:
            r = PurchaseResult(fan.fan_id, fan.name, "ERROR", detail=str(getattr(err, "cause", err)))
        v = self.fans[fan.fan_id]
        v.stage, v.seat, v.charge_attempts, v.detail = r.status, r.seat, r.charge_attempts, r.detail
        self.feed.append({"fan_id": r.fan_id, "name": r.name, "status": r.status, "seat": r.seat,
                          "retries": max(r.charge_attempts - 1, 0), "detail": r.detail})
        if self.done and not self.finished:
            self.finished = time.monotonic()

    async def _poll_stages(self) -> None:
        async def poll(fan: Fan, h: WorkflowHandle) -> None:
            if self.fans[fan.fan_id].stage in FINAL:
                return
            try:
                stage = await h.query(TicketPurchaseWorkflow.stage)
            except Exception:
                return
            if self.fans[fan.fan_id].stage not in FINAL:
                self.fans[fan.fan_id].stage = stage

        while not self.done:
            await asyncio.gather(*(poll(f, h) for f, h in self._handles))
            await asyncio.sleep(0.4)

    def elapsed(self) -> float:
        end = self.finished or time.monotonic()
        return end - self.started if self.started else 0.0

    def sections(self) -> list[dict]:
        """The venue, grouped by section, with each seat's live state."""
        out: list[dict] = []
        for label, status, fan_id in seat_map(self.drop_id):
            section, row, seat = label.split(" · ")
            if not out or out[-1]["name"] != section:
                out.append({"name": section, "seats": []})
            fan = self.fans.get(fan_id) if fan_id else None
            out[-1]["seats"].append({"l": f"{row}, {seat}", "s": status, "f": fan.name if fan else ""})
        return out

    def snapshot(self) -> dict:
        return {
            "sections": self.sections(),
            "drop_id": self.drop_id,
            "fans": [asdict(f) for f in self.fans.values()],
            "feed": self.feed[-12:],
            "ledger": ledger(self.drop_id),
            "seats": self.n_seats,
            "elapsed": round(self.elapsed(), 1),
            "done": self.done,
        }
