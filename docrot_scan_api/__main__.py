"""python -m docrot_scan_api : run the service.

Environment variables are documented in docs/API.md and config.py.
"""

import json
import os
import sys

from . import checkout as checkout_mod
from .config import Config
from .jsonl import make_access_logger, make_job_logger
from .server import make_server
from .service import ScanService
from .version import SERVICE_NAME, VERSION

_WELLKNOWN_PATH = os.path.join(os.path.dirname(__file__), "..", "public",
                               ".well-known", "agent-service.json")


def load_wellknown():
    path = os.environ.get("DOCROT_WELLKNOWN_PATH", os.path.abspath(_WELLKNOWN_PATH))
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def main() -> int:
    config = Config()
    access_logger = make_access_logger(config)
    job_logger = make_job_logger(config)
    service = ScanService(config, job_logger=job_logger)
    wellknown = load_wellknown()
    checkout_store = checkout_mod.OrderStore(config.checkout_store_path,
                                             logger=job_logger)
    checkout_verifier = checkout_mod.RpcVerifier(
        config.solana_rpc_url, timeout_s=config.solana_rpc_timeout_s)

    httpd = make_server(config, service, access_logger=access_logger,
                        wellknown_body=wellknown,
                        checkout_store=checkout_store,
                        checkout_verifier=checkout_verifier)
    job_logger.log("server_started", service=SERVICE_NAME, version=VERSION,
                   host=config.host, port=config.port,
                   billingModel=config.billing_model,
                   checkoutStore=config.checkout_store_path,
                   solanaRpc=config.solana_rpc_url)
    print(f"{SERVICE_NAME} {VERSION} listening on http://{config.host}:{config.port}",
          file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        job_logger.log("server_stopped", reason="keyboard_interrupt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
