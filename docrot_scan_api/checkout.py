"""Self-serve checkout: create an order, verify the payment on-chain.

The direct purchase path for a customer who already received a verified
scan result: pay US$1 in SOL without waiting for a manual invoice.

Design constraints (kept honest on purpose):

- **No accounts, no API keys, no webhooks.** Every order carries a unique
  Solana Pay ``reference`` (a random 32-byte address). Any wallet that
  honours the Solana Pay transfer standard includes the reference in the
  transaction, which makes the payment attributable on-chain.
- **Public RPC only.** Verification talks to a public Solana mainnet RPC
  endpoint. When it is slow or rate-limited the order simply stays
  ``pending`` and the response says ``"verification": "unavailable"`` —
  we never claim "paid" we have not seen, and never fail the request.
- **Persistence is best-effort.** Orders are stored in one JSON file
  (atomic replace). If the path is unwritable the service keeps working
  in memory and logs ``checkout_store_error`` honestly.

Stdlib only, like the rest of the service.
"""

import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.request

_BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_STORE_VERSION = 1
_DEFAULT_RPC = "https://api.mainnet-beta.solana.com"


def b58encode(data: bytes) -> str:
    """Base58 (Bitcoin/Solana alphabet) without dependencies."""
    num = int.from_bytes(data, "big")
    out = []
    while num > 0:
        num, rem = divmod(num, 58)
        out.append(_BASE58[rem])
    pad = len(data) - len(data.lstrip(b"\x00"))
    return "1" * pad + "".join(reversed(out))


def new_reference() -> str:
    """A fresh, unguessable Solana address used only as payment reference."""
    return b58encode(secrets.token_bytes(32))


def new_order_id() -> str:
    return "co_" + secrets.token_hex(8)


class VerificationError(Exception):
    """RPC transport/protocol failure — surfaced honestly, never fatal."""


class RpcVerifier:
    """On-chain payment lookup against a public Solana JSON-RPC endpoint."""

    def __init__(self, rpc_url=None, timeout_s=5.0, opener=None):
        self.rpc_url = rpc_url or _DEFAULT_RPC
        self.timeout_s = timeout_s
        self._opener = opener or urllib.request.urlopen

    def _rpc(self, method, params):
        payload = json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": method, "params": params,
        }).encode("utf-8")
        req = urllib.request.Request(
            self.rpc_url, data=payload,
            headers={"Content-Type": "application/json",
                     "User-Agent": "DocrotScanBot/1.0 (+https://github.com/auroraxo/docrot-api)"},
        )
        try:
            with self._opener(req, timeout=self.timeout_s) as resp:
                body = resp.read(1024 * 1024)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise VerificationError(f"rpc {method} failed: {exc}") from exc
        try:
            doc = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise VerificationError(f"rpc {method}: invalid JSON") from exc
        if not isinstance(doc, dict) or "error" in doc:
            err = doc.get("error") if isinstance(doc, dict) else None
            raise VerificationError(f"rpc {method}: {err or 'malformed response'}")
        return doc.get("result")

    def find_payment(self, reference, pay_to, min_lamports):
        """Return the signature of a confirmed payment >= min_lamports, or None.

        Raises VerificationError when the chain cannot be consulted at all —
        callers keep the order pending and report verification unavailable.
        """
        sigs = self._rpc("getSignaturesForAddress",
                         [reference, {"limit": 16, "commitment": "confirmed"}])
        if not sigs:
            return None
        for item in sigs:
            if not isinstance(item, dict) or item.get("err"):
                continue
            signature = item.get("signature")
            if not signature:
                continue
            tx = self._rpc("getTransaction", [signature, {
                "encoding": "jsonParsed",
                "commitment": "confirmed",
                "maxSupportedTransactionVersion": 0,
            }])
            if not tx or not isinstance(tx, dict):
                continue
            meta = tx.get("meta") or {}
            if meta.get("err") is not None:
                continue
            keys = (tx.get("transaction", {}).get("message", {})
                    .get("accountKeys") or [])
            order = [k.get("account") if isinstance(k, dict) else k
                     for k in keys]
            pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
            if pay_to not in order:
                continue
            idx = order.index(pay_to)
            if idx >= len(pre) or idx >= len(post):
                continue
            if post[idx] - pre[idx] >= min_lamports:
                return signature
        return None


class OrderStore:
    """Thread-safe, file-backed order registry with best-effort persistence."""

    def __init__(self, path=None, logger=None, max_orders=2000):
        self.path = path or None
        self.logger = logger
        self.max_orders = max_orders
        self._orders = {}
        self._lock = threading.Lock()
        self._load()

    # -- persistence ---------------------------------------------------------

    def log(self, event, **fields):
        if self.logger:
            self.logger.log(event, **fields)

    def _log(self, event, **fields):
        self.log(event, **fields)

    def _load(self):
        if not self.path:
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                doc = json.load(fh)
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError) as exc:
            self._log("checkout_store_error", op="load", error=str(exc))
            return
        if isinstance(doc, dict) and doc.get("version") == _STORE_VERSION:
            orders = doc.get("orders")
            if isinstance(orders, dict):
                self._orders = {k: v for k, v in orders.items()
                                if isinstance(v, dict)}

    def _save(self):
        if not self.path:
            return
        doc = {"version": _STORE_VERSION, "orders": self._orders}
        tmp = f"{self.path}.tmp.{os.getpid()}"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(doc, fh, separators=(",", ":"), sort_keys=True)
            os.replace(tmp, self.path)
        except OSError as exc:
            self._log("checkout_store_error", op="save", error=str(exc))
            try:
                os.unlink(tmp)
            except OSError:
                pass

    # -- operations ----------------------------------------------------------

    def create(self, order):
        with self._lock:
            self._prune_locked()
            self._orders[order["orderId"]] = order
            self._save()
            return dict(order)

    def get(self, order_id):
        with self._lock:
            order = self._orders.get(order_id)
            return dict(order) if order else None

    def touch(self, order_id, checked_at):
        with self._lock:
            order = self._orders.get(order_id)
            if order is not None:
                order["lastCheckedAt"] = checked_at
                self._save()

    def mark_paid(self, order_id, signature, paid_at):
        with self._lock:
            order = self._orders.get(order_id)
            if order is None:
                return None
            if order.get("status") != "paid":
                order["status"] = "paid"
                order["transaction"] = signature
                order["paidAt"] = paid_at
                self._save()
                self._log("checkout_paid", orderId=order_id,
                          transaction=signature)
            return dict(order)

    def _prune_locked(self):
        if len(self._orders) < self.max_orders:
            return
        now = time.time()
        # oldest expired pending first, then oldest overall; paid orders
        # are revenue records and only dropped as a last resort.
        def age(item):
            return item[1].get("createdAt", 0)
        expired = [k for k, v in self._orders.items()
                   if v.get("status") != "paid" and v.get("expiresAt", 0) < now]
        for key in sorted(expired, key=lambda k: age((k, self._orders[k]))):
            del self._orders[key]
            if len(self._orders) < self.max_orders:
                return
        for key in sorted(self._orders, key=lambda k: age((k, self._orders[k]))):
            del self._orders[key]
            if len(self._orders) < self.max_orders:
                return


def solana_pay_uri(pay_to, amount, reference, label, message):
    from urllib.parse import urlencode
    query = urlencode({"amount": amount, "reference": reference,
                       "label": label, "message": message})
    return f"solana:{pay_to}?{query}"


def build_order(config, repository, ref, request_id=None):
    """Fresh pending order for one completed scan (US$1 in SOL)."""
    now = time.time()
    amount = config.sol_reference_quote
    return {
        "orderId": new_order_id(),
        "status": "pending",
        "createdAt": now,
        "expiresAt": now + config.checkout_ttl_s,
        "repository": repository,
        "ref": ref,
        "requestId": request_id,
        "reference": new_reference(),
        "payTo": config.sol_pay_to,
        "amount": amount,
        "amountLamports": int(round(float(amount) * 1_000_000_000)),
        "priceUsd": config.price_usd_per_scan,
        "lastCheckedAt": 0,
    }


def order_payload(order, config, verification):
    """API representation of one order (timestamps as UTC ISO strings)."""
    iso = lambda ts: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))
    uri = solana_pay_uri(
        order["payTo"], order["amount"], order["reference"],
        "Docrot Scan API",
        f"US${order['priceUsd']:.2f} for scan {order['requestId'] or order['orderId']}",
    )
    payload = {
        "orderId": order["orderId"],
        "status": order["status"],
        "createdAt": iso(order["createdAt"]),
        "expiresAt": iso(order["expiresAt"]),
        "verification": verification,
        "order": {
            "repository": order["repository"],
            "ref": order["ref"],
            "requestId": order["requestId"],
        },
        "payment": {
            "network": "solana",
            "asset": "SOL",
            "payTo": order["payTo"],
            "amount": order["amount"],
            "amountLamports": order["amountLamports"],
            "amountUsd": order["priceUsd"],
            "reference": order["reference"],
            "solanaPayUri": uri,
            "terms": ("USD is the contractual price; the SOL amount is the "
                      "reference quote for this order. Pay to the address "
                      "with this reference attached (Solana Pay wallets do "
                      "it automatically)."),
        },
        "statusUrl": f"/v1/checkout/{order['orderId']}",
    }
    if order.get("transaction"):
        payload["transaction"] = order["transaction"]
        payload["paidAt"] = iso(order["paidAt"])
    if config.billing_model:
        payload["billingModel"] = config.billing_model
    return payload


def check_payment(order, store, verifier, now=None, cooldown_s=10):
    """Advance one pending order against the chain; returns verification flag.

    Returns one of:
      - "verified"          payment found and recorded
      - "pending"           chain consulted, no qualifying payment yet
      - "unavailable"       chain could not be consulted
      - "skipped"           not applicable (already paid / expired / cooldown)
    """
    now = time.time() if now is None else now
    if order.get("status") == "paid":
        return "skipped"
    if order.get("expiresAt", 0) < now:
        return "skipped"
    if now - order.get("lastCheckedAt", 0) < cooldown_s:
        return "skipped"
    store.touch(order["orderId"], now)
    try:
        signature = verifier.find_payment(order["reference"], order["payTo"],
                                          order["amountLamports"])
    except VerificationError as exc:
        store.log("checkout_verify_error", orderId=order["orderId"],
                  error=str(exc))
        return "unavailable"
    if signature:
        store.mark_paid(order["orderId"], signature, time.time())
        return "verified"
    return "pending"
