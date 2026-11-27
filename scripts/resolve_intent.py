#!/usr/bin/env python3
"""Operator resolution path for UNKNOWN / BLOCKED intents (B17.1).

An UNKNOWN exit intent blocks its symbol indefinitely with no admin
path. This script is that path. It NEVER submits an order — it only
looks orders up (the normal reconcile path) or records a human
resolution in the intent store.

Usage:
  # List the open alerts / UNKNOWN and BLOCKED intents (no change):
  python scripts/resolve_intent.py --store order_intents.db

  # Resolve one intent (lookup first; adopt if the broker has it):
  python scripts/resolve_intent.py --store order_intents.db \\
      --intent-id <intent_id> --confirm

  # Resolve as "never accepted" with the broker's own orders list as
  # the independent confirmation:
  python scripts/resolve_intent.py --store order_intents.db \\
      --intent-id <intent_id> --decision never_accepted \\
      --reason "Alpaca order history shows no such order" \\
      --check-orders-list --confirm

  # Resolve as "never accepted" with an operator-supplied evidence
  # reference (dashboard / support confirmation). The reference must
  # be specific to THIS order: it must name the symbol AND the
  # client_order_id (or the broker order id) — a bare "the orders list
  # came back empty" is refused and the block stays.
  python scripts/resolve_intent.py --store order_intents.db \\
      --intent-id <intent_id> --decision never_accepted \\
      --reason "dashboard order history checked" \\
      --evidence '{"source":"dashboard","scope":"account orders NVDA 2026-11-25..2026-11-27","result":"no order with client_order_id <intent_id>"}' \\
      --confirm

  # Resolve as "accepted, adopt" with the broker's confirmed state:
  python scripts/resolve_intent.py --store order_intents.db \\
      --intent-id <intent_id> --decision accepted \\
      --reason "Alpaca dashboard shows the order filled 2 @ 167.25" \\
      --evidence '{"source":"dashboard","scope":"account orders NVDA order <intent_id>","result":"filled 2 @ 167.25"}' \\
      --status filled --filled-qty 2 --confirm

Rules (B17.1 + follow-ups):
- The broker order is looked up FIRST (the leg's
  get_by_client_order_id). If the broker has it, the found state is
  adopted through the normal reconcile path and reported — no human
  decision is needed.
- If the lookup is inconclusive, the operator may resolve it ONLY
  with an explicit human decision (--decision) AND a reason
  (--reason) AND a structured evidence reference (--evidence
  {source, scope, result}, or --check-orders-list which builds it
  from the broker's own orders list). A timeout, a dropped response,
  or repeated 404s from the per-order lookup alone are INSUFFICIENT
  evidence to mark an order "never accepted" — and an empty orders
  list ALONE is insufficient too: the evidence must name WHAT was
  checked (the query and its scope — account, symbol, window, status
  filter) and what it returned, AND it must be specific to THIS
  order (it must name the symbol and the client_order_id or broker
  order id). A bare string, or a reference with no scope, or a
  reference that names neither the symbol nor the order, is refused
  and the block stays.
- The resolution is an APPENDED record (the resolutions table): it
  never overwrites or erases the intent's attempts, broker_order_id,
  or prior status. The status change is recorded as a human
  resolution (resolved_by / resolution_reason), not as if the broker
  had rejected it.
- Without --confirm the script only REPORTS and changes nothing.
  With --confirm it prints exactly what it will do, then does it.

Exit codes: 0 = OK (listed, or resolved); 1 = the resolution was
refused (the intent is unchanged, the block stays); 2 = usage error
(unknown intent, missing arguments, broker unavailable).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from order_manager import OrderManager, ResolveRefusedError  # noqa: E402


def _age(created_at: float) -> str:
    try:
        seconds = max(0.0, time.time() - float(created_at))
    except (TypeError, ValueError):
        return "?"
    if seconds < 3600:
        return f"{seconds / 60:.0f}m"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def _fmt_ts(ts) -> str:
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (TypeError, ValueError):
        return "?"


def _list_unresolved(manager: OrderManager) -> None:
    intents = manager.unresolved_intents()
    alerts = manager.get_alerts(unresolved_only=True)
    print("UNKNOWN / BLOCKED intents (the operator's work list):")
    if not intents:
        print("  (none)")
    for row in intents:
        print(
            f"  account={row.get('account') or 'paper'} symbol={row.get('symbol')} "
            f"intent_id={row.get('intent_id')} status={row.get('status')} "
            f"attempts={row.get('attempts')} broker_order_id={row.get('broker_order_id')} "
            f"age={_age(row.get('created_at'))} created={_fmt_ts(row.get('created_at'))}"
        )
    print(f"Open alerts: {len(alerts)}")
    for alert in alerts:
        print(
            f"  intent_id={alert.get('intent_id')} symbol={alert.get('symbol')} "
            f"reason={alert.get('reason')}"
        )


def _build_broker(cfg, broker_name: str):
    """Build the broker (for the lookup + the orders list only)."""
    if broker_name == "fake":
        fixture_dir = os.environ.get("ATS_FAKE_BROKER_DIR")
        if not fixture_dir:
            raise SystemExit("--broker fake requires ATS_FAKE_BROKER_DIR (fixture dir)")
        from fake_dual_broker import FakeDualBroker

        return FakeDualBroker(fixture_dir)
    from main import build_components

    broker, _guardian, _memory = build_components(cfg, broker_name)
    return broker


def _leg_for_account(broker, account: str):
    leg = getattr(broker, account, None)
    if leg is None:
        raise SystemExit(f"broker has no leg for account {account!r}")
    return leg


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Resolve one UNKNOWN intent (B17.1). Never submits an order.")
    parser.add_argument("--store", default=None,
                        help="intent store path (default: ORDER_STORE_PATH or ./order_intents.db)")
    parser.add_argument("--broker", choices=("alpaca", "fake"), default="alpaca",
                        help="broker backend for the lookup / orders list (default: alpaca)")
    parser.add_argument("--intent-id", default=None, help="the intent to resolve")
    parser.add_argument("--decision", choices=("never_accepted", "accepted"), default=None,
                        help="human decision (required when the lookup is inconclusive)")
    parser.add_argument("--reason", default=None,
                        help="why the operator believes this outcome")
    parser.add_argument("--evidence", default=None,
                        help="structured evidence reference as JSON: "
                             '{"source": ..., "scope": ..., "result": ...} '
                             "(what was checked, when, and what it returned)")
    parser.add_argument("--check-orders-list", action="store_true",
                        help="query the leg's full orders list (status=all) as the "
                             "independent confirmation and build the evidence from it")
    parser.add_argument("--status", default=None,
                        help="broker's confirmed status (for --decision accepted)")
    parser.add_argument("--filled-qty", type=float, default=None,
                        help="broker's confirmed filled qty (for --decision accepted)")
    parser.add_argument("--confirm", action="store_true",
                        help="apply the resolution (without it, only report)")
    parser.add_argument("--resolver", default="operator",
                        help="who is resolving it (recorded in the audit trail)")
    args = parser.parse_args(argv)

    store = args.store or os.environ.get("ORDER_STORE_PATH") or "./order_intents.db"
    manager = OrderManager(store)

    if args.intent_id is None:
        _list_unresolved(manager)
        print("\nNo --intent-id given: listed only, changed nothing. "
              "Pass --intent-id to resolve one intent.")
        return 0

    intent = manager.get_intent(args.intent_id)
    if intent is None:
        print(f"error: no intent {args.intent_id} in {store}", file=sys.stderr)
        _list_unresolved(manager)
        return 2
    account = str(intent.get("account") or "paper")
    symbol = str(intent.get("symbol"))
    print(f"Intent {args.intent_id}: account={account} symbol={symbol} "
          f"status={intent.get('status')} attempts={intent.get('attempts')} "
          f"broker_order_id={intent.get('broker_order_id')} "
          f"created={_fmt_ts(intent.get('created_at'))}")

    # Build the broker for the lookup (and the orders list).
    from config import TradingConfig

    cfg = TradingConfig.from_env()
    try:
        broker = _build_broker(cfg, args.broker)
    except SystemExit as exc:
        print(f"error: cannot build the {args.broker} broker: {exc}", file=sys.stderr)
        return 2
    leg = _leg_for_account(broker, account)
    lookup_fn = getattr(leg, "get_by_client_order_id", None)
    if lookup_fn is None:
        print(f"error: the {account} leg has no get_by_client_order_id", file=sys.stderr)
        return 2

    # 1. Look the broker order up FIRST.
    try:
        found = lookup_fn(args.intent_id)
    except Exception as exc:
        print(f"Lookup raised ({exc}); treating it as inconclusive.")
        found = None
    if isinstance(found, dict) and found:
        print(f"Lookup FOUND the order: status={found.get('status')} "
              f"filled_qty={found.get('filled_qty')} "
              f"filled_avg_price={found.get('filled_avg_price')}")
        print("Will adopt the found state through the normal reconcile path "
              "(no human decision needed).")
        if not args.confirm:
            print("No --confirm: reporting only, changed nothing. "
                  "Re-run with --confirm to adopt it.")
            return 0
        try:
            outcome, resolved = manager.resolve_intent(
                args.intent_id, decision="accepted", reason="",
                evidence={"source": "broker_lookup",
                          "scope": f"get_by_client_order_id({args.intent_id})",
                          "result": f"status={found.get('status')} filled_qty={found.get('filled_qty')}"},
                resolver=args.resolver, lookup_fn=lookup_fn)
        except ResolveRefusedError as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 1
        print(f"Adopted by lookup: intent is now {resolved.get('status')} "
              f"(broker_order_id={resolved.get('broker_order_id')}, "
              f"filled_qty={resolved.get('filled_qty')}).")
        return 0

    print("Lookup inconclusive (None / raised): the broker order was not "
          "found by client_order_id. A human decision is required.")
    if args.decision is None:
        print("No --decision given: reporting only, changed nothing. Pass "
              "--decision never_accepted (with --check-orders-list or "
              "--evidence) or --decision accepted (with --status and "
              "--filled-qty), plus --confirm, to resolve it.")
        return 0

    # 2. Build the evidence reference.
    evidence = None
    if args.check_orders_list:
        list_fn = getattr(leg, "list_orders", None)
        if list_fn is None:
            print("error: this leg has no list_orders(); the independent "
                  "orders-list check is unavailable — supply --evidence "
                  "instead", file=sys.stderr)
            return 2
        try:
            orders = list_fn(status="all")
        except TypeError:
            orders = list_fn()
        except Exception as exc:
            print(f"error: the orders list query failed ({exc}); the "
                  "independent check is inconclusive — the block stays",
                  file=sys.stderr)
            return 1
        orders = list(orders or [])
        match = [o for o in orders
                 if str(o.get("client_order_id")) == str(args.intent_id)]
        scope = (
            f"account {account} orders status=all (full history: open, "
            f"filled, canceled, expired, rejected), symbol {symbol}, "
            f"window {intent.get('trading_day')}..now, {len(orders)} orders returned"
        )
        if match:
            # The broker's own history HAS the order: adopt it (the
            # per-order lookup was inconclusive, but the history is
            # the broker's own record).
            entry = match[0]
            print(f"The orders list FOUND the order: status={entry.get('status')} "
                  f"filled_qty={entry.get('filled_qty')}. Adopting it.")
            evidence = {"source": "orders_list", "scope": scope,
                        "result": f"found client_order_id {args.intent_id}: "
                                  f"status={entry.get('status')} "
                                  f"filled_qty={entry.get('filled_qty')}"}
            if args.decision != "accepted":
                print("note: the orders list found the order, so the decision "
                      "is 'accepted' (adopting its state), not 'never_accepted'.")
            args.decision = "accepted"
            args.status = str(entry.get("status"))
            args.filled_qty = float(entry.get("filled_qty") or 0.0)
        else:
            # The full-history list over the relevant window returns
            # no match: that IS the independent confirmation (follow-up
            # 1). An empty list ALONE (no scope) would not be — the
            # scope above is what makes this auditable.
            evidence = {"source": "orders_list", "scope": scope,
                        "result": f"no order with client_order_id {args.intent_id}"}
            print(f"The orders list returned {len(orders)} orders and none "
                  f"matches client_order_id {args.intent_id}.")
    elif args.evidence:
        try:
            evidence = json.loads(args.evidence)
        except json.JSONDecodeError as exc:
            print(f"error: --evidence is not valid JSON: {exc}", file=sys.stderr)
            return 2
    else:
        print("error: a human resolution needs evidence: pass "
              "--check-orders-list or --evidence "
              "'{\"source\":..., \"scope\":..., \"result\":...}'", file=sys.stderr)
        return 2

    if args.decision == "accepted" and (args.status is None or args.filled_qty is None):
        print("error: --decision accepted requires --status and --filled-qty "
              "(the broker's confirmed status and filled qty from the "
              "broker's records)", file=sys.stderr)
        return 2
    if not args.reason:
        print("error: a human resolution requires --reason", file=sys.stderr)
        return 2

    print(f"Will resolve intent {args.intent_id} as {args.decision}: "
          f"reason={args.reason!r} evidence={json.dumps(evidence)}")
    if args.decision == "accepted":
        print(f"  adopting broker status={args.status} filled_qty={args.filled_qty}")
    else:
        print("  new status: REJECTED (recorded as a human resolution, not "
              "as if the broker had rejected it)")
    if not args.confirm:
        print("No --confirm: reporting only, changed nothing. Re-run with "
              "--confirm to apply it.")
        return 0

    try:
        outcome, resolved = manager.resolve_intent(
            args.intent_id, decision=args.decision, reason=args.reason,
            evidence=evidence, resolver=args.resolver,
            status=args.status, filled_qty=args.filled_qty,
            lookup_fn=lookup_fn)
    except ResolveRefusedError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        print("The intent is unchanged and the block stays.")
        return 1
    resolutions = manager.get_resolutions(args.intent_id)
    print(f"Resolved ({outcome}): intent is now {resolved.get('status')} "
          f"(attempts preserved: {resolved.get('attempts')}, "
          f"broker_order_id preserved: {resolved.get('broker_order_id')}, "
          f"resolved_by={resolved.get('resolved_by')}).")
    if resolutions:
        last = resolutions[-1]
        print(f"Resolution record #{last.get('id')}: decision={last.get('decision')} "
              f"resolver={last.get('resolver')} "
              f"previous_status={last.get('previous_status')} "
              f"new_status={last.get('new_status')} "
              f"attempts_at_resolution={last.get('attempts_at_resolution')} "
              f"evidence={last.get('evidence')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
