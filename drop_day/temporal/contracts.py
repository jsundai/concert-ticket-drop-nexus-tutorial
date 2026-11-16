"""Nexus Service contracts for Drop Day.

The box office team imports only this file. It never sees the bouncer, seats,
or payments code, their Workers, or their Namespaces.
"""

from dataclasses import dataclass

import nexusrpc

BOUNCER_ENDPOINT = "bouncer-endpoint"
SEATS_ENDPOINT = "seats-endpoint"
PAYMENTS_ENDPOINT = "payments-endpoint"


# ---------- Bouncer team: bot detection ----------


@dataclass
class ScreenRequest:
    drop_id: str
    fan_id: str
    name: str


@dataclass
class ScreenResult:
    fan_id: str
    verdict: str


@nexusrpc.service
class BouncerService:
    screen_fan: nexusrpc.Operation[ScreenRequest, ScreenResult]


# ---------- Seats team: inventory and holds ----------


@dataclass
class OpenDropRequest:
    drop_id: str
    capacity: int
    hold_seconds: int


@dataclass
class DropOpened:
    drop_id: str
    seats: int


@dataclass
class SeatRequest:
    drop_id: str
    fan_id: str


@dataclass
class SeatHold:
    seat: str


@dataclass
class SeatDone:
    seat: str


@nexusrpc.service
class SeatsService:
    open_drop: nexusrpc.Operation[OpenDropRequest, DropOpened]
    hold_seat: nexusrpc.Operation[SeatRequest, SeatHold]
    confirm_seat: nexusrpc.Operation[SeatRequest, SeatDone]
    release_seat: nexusrpc.Operation[SeatRequest, SeatDone]


# ---------- Payments team: card gateway ----------


@dataclass
class ChargeRequest:
    drop_id: str
    fan_id: str
    amount_cents: int


@dataclass
class ChargeReceipt:
    charge_id: str
    attempts: int


@dataclass
class RefundRequest:
    drop_id: str
    fan_id: str
    charge_id: str
    reason: str


@dataclass
class RefundReceipt:
    refund_id: str


@dataclass
class CreditRequest:
    drop_id: str
    fan_id: str
    amount_cents: int
    reason: str


@dataclass
class CreditReceipt:
    credit_id: str
    amount_cents: int


@nexusrpc.service
class PaymentsService:
    charge: nexusrpc.Operation[ChargeRequest, ChargeReceipt]
    refund: nexusrpc.Operation[RefundRequest, RefundReceipt]
    issue_credit: nexusrpc.Operation[CreditRequest, CreditReceipt]
