"""Direct purchase path: /v1/checkout create + status + on-chain verify.

Covers the order lifecycle (create -> pending -> paid/expired), persistence
across restarts, honest degradation when the public RPC cannot be reached,
and the server routes over real HTTP (loopback, no external network).
"""

import io
import json
import os
import tempfile
import time
import unittest
from unittest import mock

from docrot_scan_api import checkout as checkout_mod
from docrot_scan_api import server as server_mod
from docrot_scan_api.jsonl import JsonlLogger
from docrot_scan_api.server import make_server
from docrot_scan_api.service import ScanService
from tests.helpers import make_config


class FakeRpc:
    """Stands in for RpcVerifier.find_payment with scripted outcomes."""

    def __init__(self, result=None, raise_error=False):
        self.result = result
        self.raise_error = raise_error
        self.calls = []

    def find_payment(self, reference, pay_to, min_lamports):
        self.calls.append((reference, pay_to, min_lamports))
        if self.raise_error:
            raise checkout_mod.VerificationError("rpc getSignaturesForAddress failed: 429")
        return self.result


def make_order(config, **overrides):
    order = checkout_mod.build_order(config, "https://github.com/owner/repo",
                                     "main", "REQ1")
    order.update(overrides)
    return order


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self.tmp.close()
        os.unlink(self.tmp.name)
        self.config = make_config(DOCROT_CHECKOUT_STORE=self.tmp.name)
        self.stream = io.StringIO()
        self.logger = JsonlLogger("job", stream=self.stream)

    def tearDown(self):
        if os.path.exists(self.tmp.name):
            os.unlink(self.tmp.name)

    def test_create_get_roundtrip_and_persistence(self):
        store = checkout_mod.OrderStore(self.tmp.name, logger=self.logger)
        order = make_order(self.config)
        store.create(order)
        self.assertEqual(store.get(order["orderId"])["status"], "pending")

        # A new Store instance (service restart) sees the same order.
        reloaded = checkout_mod.OrderStore(self.tmp.name, logger=self.logger)
        got = reloaded.get(order["orderId"])
        self.assertIsNotNone(got)
        self.assertEqual(got["reference"], order["reference"])
        self.assertEqual(got["amountLamports"], order["amountLamports"])

    def test_mark_paid_is_recorded_once(self):
        store = checkout_mod.OrderStore(self.tmp.name, logger=self.logger)
        order = make_order(self.config)
        store.create(order)
        store.mark_paid(order["orderId"], "sigAAA", time.time())
        store.mark_paid(order["orderId"], "sigBBB", time.time())
        got = store.get(order["orderId"])
        self.assertEqual(got["status"], "paid")
        self.assertEqual(got["transaction"], "sigAAA")

    def test_unwritable_store_degrades_to_memory(self):
        bad = "/nonexistent-dir-for-test/orders.json"
        store = checkout_mod.OrderStore(bad, logger=self.logger)
        order = make_order(self.config)
        store.create(order)  # must not raise
        self.assertEqual(store.get(order["orderId"])["status"], "pending")
        self.assertIn("checkout_store_error", self.stream.getvalue())

    def test_prune_keeps_store_bounded(self):
        store = checkout_mod.OrderStore(self.tmp.name, logger=self.logger,
                                        max_orders=5)
        old = time.time() - 10_000
        for i in range(5):
            order = make_order(self.config)
            order["createdAt"] = old + i
            order["expiresAt"] = old + i  # long expired
            store.create(order)
        fresh = make_order(self.config)
        store.create(fresh)
        self.assertLessEqual(len(store._orders), 5)


class OrderLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.config = make_config()
        self.stream = io.StringIO()
        self.logger = JsonlLogger("job", stream=self.stream)
        self.store = checkout_mod.OrderStore(path=None, logger=self.logger)

    def test_amount_matches_published_reference_quote(self):
        order = make_order(self.config)
        self.assertEqual(order["amount"], "0.0065")
        self.assertEqual(order["amountLamports"], 6_500_000)
        self.assertEqual(order["priceUsd"], 1.0)
        self.assertEqual(order["payTo"],
                         "CGVHjxwMadDvLB8qGYYyD2TEwB4E8wimg68SUy1vvbzn")

    def test_reference_is_unique_and_base58(self):
        refs = {checkout_mod.new_reference() for _ in range(50)}
        self.assertEqual(len(refs), 50)
        for ref in refs:
            self.assertTrue(all(c in checkout_mod._BASE58 for c in ref))

    def test_solana_pay_uri_carries_reference_and_amount(self):
        order = make_order(self.config)
        payload = checkout_mod.order_payload(order, self.config, "not_checked")
        uri = payload["payment"]["solanaPayUri"]
        self.assertTrue(uri.startswith("solana:" + order["payTo"]))
        self.assertIn("amount=0.0065", uri)
        self.assertIn("reference=" + order["reference"], uri)

    def test_check_payment_marks_paid(self):
        order = make_order(self.config)
        self.store.create(order)
        rpc = FakeRpc(result="5VisibleSignature")
        flag = checkout_mod.check_payment(order, self.store, rpc, now=time.time())
        self.assertEqual(flag, "verified")
        got = self.store.get(order["orderId"])
        self.assertEqual(got["status"], "paid")
        self.assertEqual(got["transaction"], "5VisibleSignature")

    def test_check_payment_pending_when_chain_has_nothing(self):
        order = make_order(self.config)
        self.store.create(order)
        flag = checkout_mod.check_payment(order, self.store, FakeRpc(result=None),
                                          now=time.time())
        self.assertEqual(flag, "pending")
        self.assertEqual(self.store.get(order["orderId"])["status"], "pending")

    def test_check_payment_unavailable_on_rpc_failure(self):
        order = make_order(self.config)
        self.store.create(order)
        flag = checkout_mod.check_payment(order, self.store,
                                          FakeRpc(raise_error=True),
                                          now=time.time())
        self.assertEqual(flag, "unavailable")
        self.assertEqual(self.store.get(order["orderId"])["status"], "pending")
        self.assertIn("checkout_verify_error", self.stream.getvalue())

    def test_check_payment_cooldown_skips_rpc(self):
        order = make_order(self.config)
        self.store.create(order)
        now = time.time()
        rpc = FakeRpc(result=None)
        checkout_mod.check_payment(order, self.store, rpc, now=now, cooldown_s=10)
        checkout_mod.check_payment(order, self.store, rpc, now=now + 5,
                                   cooldown_s=10)
        self.assertEqual(len(rpc.calls), 1)

    def test_expired_order_is_not_verified(self):
        order = make_order(self.config, expiresAt=time.time() - 1)
        self.store.create(order)
        rpc = FakeRpc(result="should-not-be-called")
        flag = checkout_mod.check_payment(order, self.store, rpc, now=time.time())
        self.assertEqual(flag, "skipped")
        self.assertEqual(rpc.calls, [])


class CheckoutHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = make_config(DOCROT_PORT="0")
        cls.config.host = "127.0.0.1"
        cls.config.port = 0
        cls.config.checkout_store_path = ""  # orders kept in memory only
        cls.access_stream = io.StringIO()
        cls.store = checkout_mod.OrderStore(
            path=None, logger=JsonlLogger("job", stream=io.StringIO()))
        cls.rpc = FakeRpc(result=None)
        cls.verifier = cls.rpc
        service = ScanService(cls.config)
        import http.client
        cls.httpd = make_server(
            cls.config, service,
            access_logger=JsonlLogger("access", stream=cls.access_stream),
            wellknown_body=None,
            checkout_store=cls.store,
            checkout_verifier=cls.verifier)
        import threading
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.httpd.server_address[1]
        cls.conn = None

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def _request(self, method, path, body=None, headers=None):
        status, _ctype, data = self._raw(method, path, body, headers)
        try:
            doc = json.loads(data)
        except (json.JSONDecodeError, UnicodeDecodeError):
            doc = None
        return status, doc

    def _raw(self, method, path, body=None, headers=None):
        """Request returning (status, content_type, decoded_body_text)."""
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = dict(headers or {})
        payload = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers.setdefault("Content-Type", "application/json")
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        ctype = resp.getheader("Content-Type", "")
        conn.close()
        return resp.status, ctype, data.decode("utf-8", "replace")

    def test_01_create_order(self):
        status, doc = self._request(
            "POST", "/v1/checkout",
            {"repository": "https://github.com/owner/repo",
             "requestId": "REQ1"})
        self.assertEqual(status, 201)
        self.assertEqual(doc["status"], "pending")
        self.assertEqual(doc["verification"], "not_checked")
        self.assertEqual(doc["payment"]["amount"], "0.0065")
        self.assertEqual(doc["payment"]["amountUsd"], 1.0)
        self.assertIn("solanaPayUri", doc["payment"])
        self.assertEqual(doc["order"]["requestId"], "REQ1")

        # status endpoint: chain reachable, nothing there yet -> pending
        status, doc2 = self._request("GET", doc["statusUrl"])
        self.assertEqual(status, 200)
        self.assertEqual(doc2["orderId"], doc["orderId"])
        self.assertEqual(doc2["status"], "pending")
        self.assertEqual(doc2["verification"], "pending")

    def test_11_html_payment_page_for_browsers(self):
        status, doc = self._request(
            "POST", "/v1/checkout",
            {"repository": "https://github.com/owner/repo",
             "requestId": "REQ1"})
        self.assertEqual(status, 201)
        page_url = doc["statusUrl"]

        # Browser-style Accept -> self-contained HTML payment page
        status, ctype, body = self._raw(
            "GET", page_url, headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"})
        self.assertEqual(status, 200)
        self.assertIn("text/html", ctype)
        # the wallet deep link is HTML-escaped (& -> &amp;) like everything else
        from html import escape as _esc
        for needle in (doc["orderId"], doc["payment"]["payTo"],
                       doc["payment"]["reference"], "0.0065",
                       _esc(doc["payment"]["solanaPayUri"]),
                       "Waiting for payment"):
            self.assertIn(needle, body)
        self.assertIn('http-equiv="refresh"', body)  # auto-refresh while pending
        # no scripts, no external assets — self-contained by construction
        self.assertNotIn("<script", body)

        # API clients keep JSON: explicit application/json wins over html
        status, ctype, body = self._raw(
            "GET", page_url, headers={"Accept": "application/json, text/html"})
        self.assertIn("application/json", ctype)
        parsed = json.loads(body)
        self.assertEqual(parsed["orderId"], doc["orderId"])

        # default (no Accept) also stays JSON — existing contract unchanged
        status, ctype, body = self._raw("GET", page_url)
        self.assertIn("application/json", ctype)
        self.assertEqual(json.loads(body)["orderId"], doc["orderId"])

    def test_12_html_page_shows_paid_state(self):
        status, doc = self._request(
            "POST", "/v1/checkout",
            {"repository": "https://github.com/owner/repo"})
        order_id = doc["orderId"]
        self.__class__.rpc.result = "PaidSignatureHtml"
        status, _ctype, body = self._raw(
            "GET", f"/v1/checkout/{order_id}",
            headers={"Accept": "text/html"})
        self.assertEqual(status, 200)
        self.assertIn("Payment confirmed", body)
        self.assertIn("PaidSignatureHtml", body)
        self.assertNotIn('http-equiv="refresh"', body)  # stops on paid
        self.__class__.rpc.result = None

    def test_13_self_serve_scan_form(self):
        """GET /v1/scan-form serves the browser form (1.11.0+; pathPrefix 1.12.0+)."""
        status, ctype, body = self._raw("GET", "/v1/scan-form")
        self.assertEqual(status, 200)
        self.assertIn("text/html", ctype)
        # form posts to the same-origin API and opens the payment page
        for needle in ("/v1/scan", "/v1/checkout", "Run free scan",
                       "Pay US$1.00", "docrot-scan-api", "1.12.1"):
            self.assertIn(needle, body)
        # no external assets in the head/style: every URL there is in the
        # footer link, which is the only intentional outbound reference
        head = body.split("<footer>")[0]
        self.assertNotIn("https://", head.replace(
            "https://github.com/&lt;owner&gt;/&lt;repo&gt;", "").replace(
            "https://github.com/auroraxo/aurora-node-auditor", ""))
        self.assertIn("<script", body)  # inline JS only
        self.assertNotIn('src="http', body)  # no external script src
        self.assertNotIn('@import', body)   # no external stylesheets
        # default Accept (none) still returns the HTML — this route is HTML
        status, ctype2, _ = self._raw("GET", "/v1/scan-form",
                                      headers={"Accept": "application/json"})
        self.assertEqual(status, 200)
        self.assertIn("text/html", ctype2)

    def test_14_scan_form_absent_from_404(self):
        status, doc = self._request("GET", "/v1/no-such-route")
        self.assertEqual(status, 404)
        self.assertEqual(doc["error"]["code"], "not_found")

    def test_02_create_missing_repository(self):
        status, doc = self._request("POST", "/v1/checkout", {})
        self.assertEqual(status, 400)
        self.assertEqual(doc["error"]["code"], "missing_field")

    def test_03_create_unknown_field(self):
        status, doc = self._request("POST", "/v1/checkout",
                                    {"repository": "https://github.com/o/r",
                                     "amount": "999"})
        self.assertEqual(status, 400)
        self.assertEqual(doc["error"]["code"], "unknown_field")

    def test_04_create_invalid_repository(self):
        status, doc = self._request("POST", "/v1/checkout",
                                    {"repository": "https://gitlab.com/o/r"})
        self.assertEqual(status, 422)

    def test_05_create_invalid_request_id(self):
        status, doc = self._request("POST", "/v1/checkout",
                                    {"repository": "https://github.com/o/r",
                                     "requestId": "spaces are bad"})
        self.assertEqual(status, 400)
        self.assertEqual(doc["error"]["code"], "invalid_field")

    def test_06_unknown_order_404(self):
        status, doc = self._request("GET", "/v1/checkout/co_deadbeefdeadbeef")
        self.assertEqual(status, 404)
        self.assertEqual(doc["error"]["code"], "order_not_found")

    def test_07_get_without_id_405(self):
        status, doc = self._request("GET", "/v1/checkout")
        self.assertEqual(status, 405)
        self.assertEqual(doc["error"]["code"], "method_not_allowed")

    def test_08_create_without_post_405(self):
        status, doc = self._request("POST", "/health")
        self.assertEqual(status, 404)

    def test_09_paid_flow(self):
        # create, then flip the fake chain to "payment seen"
        status, doc = self._request(
            "POST", "/v1/checkout",
            {"repository": "https://github.com/owner/repo"})
        self.assertEqual(status, 201)
        order_id = doc["orderId"]

        self.__class__.rpc.result = "PaidSignature111"
        # bypass cooldown: order was just checked? no — create sets
        # lastCheckedAt=0, so the first GET verifies immediately.
        status, doc2 = self._request("GET", f"/v1/checkout/{order_id}")
        self.assertEqual(status, 200)
        self.assertEqual(doc2["status"], "paid")
        self.assertEqual(doc2["verification"], "verified")
        self.assertEqual(doc2["transaction"], "PaidSignature111")
        self.__class__.rpc.result = None  # restore for other tests

    def test_10_rpc_failure_reports_unavailable(self):
        status, doc = self._request(
            "POST", "/v1/checkout",
            {"repository": "https://github.com/owner/repo"})
        order_id = doc["orderId"]
        self.__class__.rpc.raise_error = True
        status, doc2 = self._request("GET", f"/v1/checkout/{order_id}")
        self.assertEqual(status, 200)
        self.assertEqual(doc2["status"], "pending")
        self.assertEqual(doc2["verification"], "unavailable")
        self.__class__.rpc.raise_error = False


class PaymentWatcherTests(unittest.TestCase):
    """Server-side sweep: revenue is found even if the buyer never polls."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self.tmp.close()
        os.unlink(self.tmp.name)
        self.config = make_config(DOCROT_CHECKOUT_STORE=self.tmp.name,
                                  DOCROT_CHECKOUT_WATCH_INTERVAL_S="30")
        self.stream = io.StringIO()
        self.logger = JsonlLogger("job", stream=self.stream)
        self.store = checkout_mod.OrderStore(self.tmp.name, logger=self.logger)

    def tearDown(self):
        if os.path.exists(self.tmp.name):
            os.unlink(self.tmp.name)

    def _order(self, **overrides):
        order = checkout_mod.build_order(self.config,
                                         "https://github.com/owner/repo",
                                         "main", "REQ1")
        order.update(overrides)
        self.store.create(order)
        return order

    def test_config_interval_defaults_to_60_and_clamps(self):
        cfg = make_config()
        self.assertEqual(cfg.checkout_watch_interval_s, 60)
        self.assertEqual(
            make_config(DOCROT_CHECKOUT_WATCH_INTERVAL_S="0")
            .checkout_watch_interval_s, 0)
        self.assertEqual(
            make_config(DOCROT_CHECKOUT_WATCH_INTERVAL_S="99999999")
            .checkout_watch_interval_s, 86400)
        # garbage env falls back to the default, like every other knob
        self.assertEqual(
            make_config(DOCROT_CHECKOUT_WATCH_INTERVAL_S="soon")
            .checkout_watch_interval_s, 60)

    def test_tick_finds_payment_without_client_poll(self):
        order = self._order()
        rpc = FakeRpc(result="SigFromWatcher")
        watcher = checkout_mod.PaymentWatcher(self.store, rpc, self.config,
                                              logger=self.logger)
        results = watcher.tick()
        self.assertEqual(results, {order["orderId"]: "verified"})
        got = self.store.get(order["orderId"])
        self.assertEqual(got["status"], "paid")
        self.assertEqual(got["transaction"], "SigFromWatcher")
        self.assertEqual(got["paidVia"], "server-watch")
        self.assertIn("checkout_paid", self.stream.getvalue())
        self.assertIn('"via":"server-watch"', self.stream.getvalue()
                      .replace(" ", ""))

    def test_tick_skips_paid_and_expired_orders(self):
        paid = self._order()
        self.store.mark_paid(paid["orderId"], "SigOld", time.time(),
                             source="status-poll")
        self._order(expiresAt=time.time() - 1)
        rpc = FakeRpc(result="should-not-be-called")
        watcher = checkout_mod.PaymentWatcher(self.store, rpc, self.config)
        self.assertEqual(watcher.tick(), {})
        self.assertEqual(rpc.calls, [])

    def test_tick_respects_shared_cooldown_with_status_polls(self):
        order = self._order()
        rpc = FakeRpc(result=None)
        now = time.time()
        # a buyer status poll checked the chain a moment ago
        checkout_mod.check_payment(self.store.get(order["orderId"]),
                                   self.store, rpc, now=now,
                                   cooldown_s=self.config
                                   .checkout_verify_cooldown_s)
        watcher = checkout_mod.PaymentWatcher(self.store, rpc, self.config)
        watcher.tick(now=now)  # inside cooldown -> no second RPC draw
        self.assertEqual(len(rpc.calls), 1)

    def test_rpc_outage_keeps_order_pending_and_logs_honestly(self):
        order = self._order()
        rpc = FakeRpc(raise_error=True)
        watcher = checkout_mod.PaymentWatcher(self.store, rpc, self.config,
                                              logger=self.logger)
        results = watcher.tick()
        self.assertEqual(results, {order["orderId"]: "unavailable"})
        self.assertEqual(self.store.get(order["orderId"])["status"], "pending")
        self.assertIn("checkout_verify_error", self.stream.getvalue())

    def test_run_is_inert_when_interval_is_zero(self):
        self.config.checkout_watch_interval_s = 0
        rpc = FakeRpc(result="should-not-be-called")
        self._order()
        watcher = checkout_mod.PaymentWatcher(self.store, rpc, self.config,
                                              logger=self.logger)
        watcher.run()  # returns immediately, no loop, no RPC
        self.assertEqual(rpc.calls, [])

    def test_start_stop_thread_lifecycle(self):
        self.config.checkout_watch_interval_s = 3600
        rpc = FakeRpc(result=None)
        self._order()
        watcher = checkout_mod.PaymentWatcher(self.store, rpc, self.config,
                                              logger=self.logger)
        thread = watcher.start()
        try:
            thread.join(timeout=5)
            self.assertTrue(thread.is_alive())
            self.assertGreaterEqual(len(rpc.calls), 1)  # first tick ran
        finally:
            watcher.stop()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())

    def test_run_survives_a_tick_that_raises(self):
        self.config.checkout_watch_interval_s = 0.05  # fast loop for the test
        self._order()

        class ExplodingRpc:
            def find_payment(self, *args, **kwargs):
                raise RuntimeError("boom")

        watcher = checkout_mod.PaymentWatcher(self.store, ExplodingRpc(),
                                              self.config, logger=self.logger)
        thread = watcher.start()
        time.sleep(0.2)
        watcher.stop()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertIn("checkout_watch_error", self.stream.getvalue())
        # and the order is untouched — no payment ever fabricated
        status = [v["status"] for v in self.store.pending()]
        self.assertEqual(status, ["pending"])

    def test_status_poll_records_paid_via_poll(self):
        order = self._order()
        rpc = FakeRpc(result="SigFromPoll")
        checkout_mod.check_payment(self.store.get(order["orderId"]),
                                   self.store, rpc, now=time.time(),
                                   cooldown_s=0, source="status-poll")
        got = self.store.get(order["orderId"])
        self.assertEqual(got["paidVia"], "status-poll")
        payload = checkout_mod.order_payload(got, self.config, "verified")
        self.assertEqual(payload["paidVia"], "status-poll")


class RpcVerifierParsingTests(unittest.TestCase):
    """Transport-level parsing of public Solana RPC responses (fake transport)."""

    def _verifier(self, responses):
        calls = []

        class FakeResp:
            def __init__(self, payload):
                self._payload = payload

            def read(self, n=-1):
                return self._payload

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def opener(req, timeout=None):
            import urllib.request
            body = json.loads(req.data.decode("utf-8"))
            calls.append(body["method"])
            payload = responses[body["method"]]
            if isinstance(payload, Exception):
                raise payload
            # Real JSON-RPC wraps the value under "result"; error objects
            # already carry the key and pass through unwrapped.
            if isinstance(payload, dict) and "error" in payload:
                doc = payload
            else:
                doc = {"jsonrpc": "2.0", "id": 1, "result": payload}
            return FakeResp(json.dumps(doc).encode("utf-8"))

        return checkout_mod.RpcVerifier("https://rpc.test", timeout_s=1,
                                        opener=opener), calls

    def test_finds_incoming_payment(self):
        sigs = [{"signature": "sig1", "err": None}]
        tx = {
            "meta": {"err": None,
                     "preBalances": [10_000_000, 1000],
                     "postBalances": [10_000_000 - 6_500_000 - 5000,
                                      1000 + 6_500_000]},
            "transaction": {"message": {"accountKeys": [
                {"account": "Payer111"},
                {"account": "OurPayToAddress"},
            ]}},
        }
        verifier, calls = self._verifier({"getSignaturesForAddress": sigs,
                                          "getTransaction": tx})
        got = verifier.find_payment("RefAddr", "OurPayToAddress", 6_500_000)
        self.assertEqual(got, "sig1")
        self.assertEqual(calls, ["getSignaturesForAddress", "getTransaction"])

    def test_rejects_underpayment_and_failed_tx(self):
        sigs = [{"signature": "sig1", "err": None},
                {"signature": "sig2", "err": None}]
        tx = {
            "meta": {"err": None,
                     "preBalances": [1000, 0],
                     "postBalances": [900, 100]},  # payTo got only 100
            "transaction": {"message": {"accountKeys": [
                {"account": "Payer"}, {"account": "OurPayToAddress"}]}},
        }
        verifier, _ = self._verifier({"getSignaturesForAddress": sigs,
                                      "getTransaction": tx})
        self.assertIsNone(verifier.find_payment("Ref", "OurPayToAddress",
                                                6_500_000))

    def test_transport_error_raises_verification_error(self):
        import urllib.error
        verifier, _ = self._verifier({
            "getSignaturesForAddress": urllib.error.URLError("timed out")})
        with self.assertRaises(checkout_mod.VerificationError):
            verifier.find_payment("Ref", "PayTo", 1)

    def test_rpc_error_object_raises_verification_error(self):
        verifier, _ = self._verifier({
            "getSignaturesForAddress": {"error": {"code": 429,
                                                  "message": "Too many requests"}}})
        with self.assertRaises(checkout_mod.VerificationError):
            verifier.find_payment("Ref", "PayTo", 1)


if __name__ == "__main__":
    unittest.main()
