#!/usr/bin/env python3
"""
truenas-csi-api-guard
A filtering WebSocket proxy that sits between the truenas-csi driver and
TrueNAS middleware. It allows only whitelisted JSON-RPC methods and enforces
that all dataset/snapshot operations target a configured path prefix.

All other calls are rejected with a JSON-RPC error — the upstream never sees them.
"""

import asyncio
import json
import logging
import ssl
import sys
from pathlib import Path
from typing import Optional

import websockets
import yaml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("truenas-csi-api-guard")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config(path: str) -> dict:
    with open(path) as f:
        cfg = yaml.safe_load(f)
    required = [
        "listen_port", "cert", "key", "upstream",
        "allowed_dataset_prefix", "allowed_methods",
    ]
    for key in required:
        if key not in cfg:
            raise ValueError(f"Missing required config key: {key}")
    # Normalise allowed_dataset_prefix to always be a list internally
    p = cfg["allowed_dataset_prefix"]
    cfg["allowed_dataset_prefix"] = [p] if isinstance(p, str) else list(p)
    return cfg


# ---------------------------------------------------------------------------
# Frame filtering
# ---------------------------------------------------------------------------

# Methods where params[0] is a string dataset/snapshot path.
DATASET_PATH_METHODS_STR = {
    "pool.dataset.delete",
    "pool.dataset.update",
    "pool.snapshot.delete",
}

# Methods where params[0] is a dict; value is the field that holds the dataset path.
DATASET_PATH_METHODS_DICT = {
    "pool.dataset.create": "name",
    "pool.snapshot.create": "dataset",
    "pool.snapshot.clone": "dataset_dst",
    "pool.snapshottask.create": "dataset",
}

# These methods carry a dict as first param with a "path" or "dataset" key.
SHARING_PATH_METHODS = {
    "sharing.nfs.create",
}

# Query methods — params are filter arrays, we allow them freely
# (they only read, and the CSI user's RBAC already limits scope).
QUERY_METHODS = {
    "pool.dataset.query",
    "pool.dataset.get_instance",
    "pool.snapshot.query",
    "pool.snapshottask.query",
    "pool.snapshottask.get_instance",
    "sharing.nfs.query",
    "iscsi.auth.query",
    "iscsi.extent.query",
    "iscsi.target.query",
    "iscsi.targetextent.query",
    "iscsi.portal.query",
    "zfs.resource.query",
}

# iSCSI write methods — first param is a dict with a "path" key for extents.
ISCSI_EXTENT_METHODS = {
    "iscsi.extent.create",
}


def make_error(id_, code: int, message: str) -> str:
    return json.dumps({
        "jsonrpc": "2.0",
        "id": id_,
        "error": {
            "code": code,
            "message": message,
        }
    })


def extract_dataset_path(method: str, params: list) -> Optional[str]:
    """
    Best-effort extraction of the dataset/zvol path from a JSON-RPC params list.
    Returns None if the path cannot be determined (caller should allow through).
    """
    if not params:
        return None

    if method in DATASET_PATH_METHODS_STR:
        if isinstance(params[0], str):
            return params[0]

    if method in DATASET_PATH_METHODS_DICT:
        key = DATASET_PATH_METHODS_DICT[method]
        if isinstance(params[0], dict):
            return params[0].get(key)

    if method in SHARING_PATH_METHODS:
        if isinstance(params[0], dict):
            return params[0].get("path") or params[0].get("dataset")

    if method in ISCSI_EXTENT_METHODS:
        if isinstance(params[0], dict):
            return params[0].get("path")  # zvol path like /dev/zvol/tank/k8s/...

    return None


def is_allowed(frame: dict, cfg: dict) -> tuple[bool, str]:
    """
    Returns (allowed, reason).
    """
    method = frame.get("method")
    if method is None:
        # Not a call frame (could be a response or notification) — pass through
        return True, ""

    allowed_methods: list = cfg["allowed_methods"]
    prefixes: list = cfg["allowed_dataset_prefix"]

    if method not in allowed_methods:
        return False, f"method '{method}' is not in the allowlist"

    # Query methods are read-only; no path enforcement needed
    if method in QUERY_METHODS:
        return True, ""

    params = frame.get("params", [])
    path = extract_dataset_path(method, params)

    if path is None:
        # Can't extract a path — allow and let TrueNAS enforce its own RBAC
        return True, ""

    # Normalise: strip filesystem prefixes so paths match the dataset prefix
    normalised = path.removeprefix("/dev/zvol/").removeprefix("/mnt/")

    if not any(normalised == p or normalised.startswith(p + "/") for p in prefixes):
        return False, (
            f"method '{method}' targets '{normalised}' "
            f"which is outside the allowed prefixes {prefixes}"
        )

    return True, ""


# ---------------------------------------------------------------------------
# Proxy connection handler
# ---------------------------------------------------------------------------

async def handle_client(client_ws, cfg: dict):
    upstream_url: str = cfg["upstream"]
    client_addr = client_ws.remote_address
    client_ip = client_addr[0]

    allowed_sources = cfg.get("allowed_sources")
    if allowed_sources is not None and client_ip not in allowed_sources:
        log.warning("REJECTED connection from %s — not in allowed_sources", client_ip)
        await client_ws.close()
        return

    log.info("Client connected: %s", client_addr)

    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE  # upstream is localhost

    try:
        async with websockets.connect(upstream_url, ssl=ssl_ctx) as upstream_ws:
            log.info("Upstream connection established for %s", client_addr)

            async def client_to_upstream():
                try:
                    async for raw in client_ws:
                        try:
                            frame = json.loads(raw)
                        except json.JSONDecodeError:
                            log.warning("Non-JSON frame from client %s, dropping", client_addr)
                            continue

                        allowed, reason = is_allowed(frame, cfg)
                        if not allowed:
                            log.warning(
                                "BLOCKED [%s] %s — %s",
                                client_addr,
                                frame.get("method"),
                                reason,
                            )
                            error = make_error(frame.get("id"), -32603, f"Proxy: {reason}")
                            await client_ws.send(error)
                            continue

                        log.debug("ALLOW [%s] %s", client_addr, frame.get("method"))
                        await upstream_ws.send(raw)
                except websockets.exceptions.ConnectionClosed:
                    pass

            async def upstream_to_client():
                async for raw in upstream_ws:
                    await client_ws.send(raw)

            # Run both directions concurrently; stop when either side closes
            done, pending = await asyncio.wait(
                [
                    asyncio.create_task(client_to_upstream()),
                    asyncio.create_task(upstream_to_client()),
                ],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()

    except websockets.exceptions.ConnectionClosedOK:
        pass
    except Exception as exc:
        log.error("Error handling client %s: %s", client_addr, exc)
    finally:
        log.info("Client disconnected: %s", client_addr)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main(config_path: str):
    cfg = load_config(config_path)

    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_ctx.load_cert_chain(certfile=cfg["cert"], keyfile=cfg["key"])

    host = cfg.get("listen_host", "0.0.0.0")
    if isinstance(host, str):
        host = [host]
    port = cfg["listen_port"]
    log.info(
        "truenas-csi-api-guard starting on %s:%d, upstream: %s, prefixes: %s",
        ", ".join(host), port, cfg["upstream"], cfg["allowed_dataset_prefix"],
    )

    try:
        server = await websockets.serve(
            lambda ws: handle_client(ws, cfg),
            host=host,
            port=port,
            ssl=ssl_ctx,
        )
    except OSError as exc:
        log.error("Failed to bind on %s:%d — %s", ", ".join(host), port, exc)
        sys.exit(1)

    await server.serve_forever()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: proxy.py <config.yaml>", file=sys.stderr)
        sys.exit(1)
    config_path = sys.argv[1]
    asyncio.run(main(config_path))
