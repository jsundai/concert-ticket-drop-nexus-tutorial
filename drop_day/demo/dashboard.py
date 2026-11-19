"""Drop Day live dashboard.

    uv run python -m drop_day.demo.dashboard          # starts the four team Workers too
    uv run python -m drop_day.demo.dashboard --no-workers   # if you run the Workers yourself

Then open http://localhost:8080. Needs the dev server from ./start-server.sh
and the Namespaces from ./setup.sh.
"""

import argparse
import asyncio
import signal
import subprocess
import sys
from pathlib import Path

from aiohttp import web
from temporalio.client import Client

from drop_day.temporal.boxoffice import NAMESPACE
from drop_day.temporal.contracts import PAYMENTS_ENDPOINT, SEATS_ENDPOINT
from drop_day.demo.engine import HEADLINER, TOUR, DropRun, issue_credit
from drop_day.systems.store import DATA_DIR, read_chaos, write_chaos

STATIC = Path(__file__).resolve().parent / "static"
ROOT = Path(__file__).resolve().parents[2]
TEAMS = ["boxoffice", "bouncer", "seats", "payments"]


class Workers:
    """Runs each team's Worker as a child process, so the dashboard can crash and revive them."""

    def __init__(self, managed: bool) -> None:
        self.managed = managed
        self.procs: dict[str, subprocess.Popen] = {}
        (DATA_DIR / "logs").mkdir(parents=True, exist_ok=True)

    def running(self, team: str) -> bool:
        p = self.procs.get(team)
        return p is not None and p.poll() is None

    def start(self, team: str) -> None:
        if not self.managed or self.running(team):
            return
        log = open(DATA_DIR / "logs" / f"{team}.log", "a")
        self.procs[team] = subprocess.Popen(
            [sys.executable, "-m", f"drop_day.temporal.{team}"], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT
        )

    def kill(self, team: str) -> None:
        p = self.procs.get(team)
        if p and p.poll() is None:
            p.send_signal(signal.SIGKILL)  # a crash, not a graceful shutdown
            p.wait()

    def status(self) -> dict:
        if not self.managed:
            return {t: None for t in TEAMS}
        return {t: self.running(t) for t in TEAMS}

    def stop_all(self) -> None:
        for team in list(self.procs):
            p = self.procs[team]
            if p.poll() is None:
                p.terminate()


class App:
    def __init__(self, client: Client, workers: Workers) -> None:
        self.client = client
        self.workers = workers
        self.run: DropRun | None = None
        self.standalone: list[dict] = []

    async def refresh_standalone(self) -> None:
        query = f'Endpoint = "{PAYMENTS_ENDPOINT}" OR Endpoint = "{SEATS_ENDPOINT}"'
        while True:
            try:
                ops = []
                async for op in self.client.list_nexus_operations(query, limit=50):
                    ops.append(op)
                ops.sort(key=lambda o: o.schedule_time, reverse=True)
                self.standalone = [
                    {"id": o.operation_id, "endpoint": o.endpoint, "operation": o.operation,
                     "status": o.status.name}
                    for o in ops[:8]
                ]
            except Exception:
                pass
            await asyncio.sleep(2)

    async def index(self, _: web.Request) -> web.FileResponse:
        return web.FileResponse(STATIC / "index.html")

    async def state(self, _: web.Request) -> web.Response:
        return web.json_response({
            "headliner": HEADLINER,
            "tour": TOUR,
            "workers": self.workers.status(),
            "chaos": read_chaos().get("gateway_flakiness", 0.3),
            "drop": self.run.snapshot() if self.run else None,
            "standalone": self.standalone,
        })

    async def drop(self, req: web.Request) -> web.Response:
        if self.run and not self.run.done:
            return web.json_response({"error": "A drop is already running"}, status=409)
        body = await req.json()
        run = DropRun(
            self.client,
            n_fans=max(1, min(int(body.get("fans", 150)), 400)),
            n_seats=max(1, int(body.get("seats", 100))),
            n_bots=max(0, int(body.get("bots", 10))),
            flakiness=read_chaos().get("gateway_flakiness", 0.3),
            hold_seconds=max(5, int(body.get("hold_seconds", 120))),
        )
        run.n_bots = min(run.n_bots, run.n_fans)
        try:
            await run.start()
        except Exception as err:
            return web.json_response({"error": f"Could not open the drop: {err}"}, status=500)
        self.run = run
        return web.json_response({"drop_id": run.drop_id})

    async def chaos(self, req: web.Request) -> web.Response:
        body = await req.json()
        write_chaos(gateway_flakiness=max(0.0, min(float(body["flakiness"]), 0.9)))
        return web.json_response({"ok": True})

    async def worker(self, req: web.Request) -> web.Response:
        team, action = req.match_info["team"], req.match_info["action"]
        if team not in TEAMS or action not in ("kill", "start"):
            return web.json_response({"error": "unknown team or action"}, status=404)
        (self.workers.kill if action == "kill" else self.workers.start)(team)
        return web.json_response(self.workers.status())

    async def credit(self, req: web.Request) -> web.Response:
        if not self.run:
            return web.json_response({"error": "No drop yet"}, status=400)
        body = await req.json()
        fan = self.run.fans.get(body.get("fan_id", ""))
        if fan is None:
            return web.json_response({"error": "Unknown fan"}, status=404)
        try:
            credit_id = await issue_credit(self.client, self.run.drop_id, fan.fan_id, 2000, "Sorry about the queue")
        except Exception as err:
            return web.json_response({"error": str(err)}, status=500)
        return web.json_response({"credit_id": credit_id, "name": fan.name})


@web.middleware
async def not_found(req: web.Request, handler):
    """Easter egg: unknown pages get a wrong-gate page instead of a plain 404."""
    try:
        return await handler(req)
    except web.HTTPNotFound:
        if req.path.startswith("/api/"):
            raise
        return web.FileResponse(STATIC / "404.html", status=404)


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--no-workers", action="store_true", help="Don't start the team Workers")
    args = p.parse_args()

    workers = Workers(managed=not args.no_workers)
    for team in TEAMS:
        workers.start(team)

    client = await Client.connect("localhost:7233", namespace=NAMESPACE)
    app = App(client, workers)
    web_app = web.Application(middlewares=[not_found])
    web_app.add_routes([
        web.get("/", app.index),
        web.get("/setlist", app.index),  # easter egg: opens the dashboard with the secret setlist showing
        web.get("/setlist/cli", app.index),
        web.get("/api/state", app.state),
        web.post("/api/drop", app.drop),
        web.post("/api/chaos", app.chaos),
        web.post("/api/workers/{team}/{action}", app.worker),
        web.post("/api/credit", app.credit),
        web.static("/static", STATIC),
    ])
    asyncio.create_task(app.refresh_standalone())

    runner = web.AppRunner(web_app)
    await runner.setup()
    await web.TCPSite(runner, "localhost", args.port).start()
    print(f"Drop Day dashboard at http://localhost:{args.port}", flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        workers.stop_all()
        await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
