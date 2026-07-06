"""JSON-RPC layer for Deribit private API: response matching and auth.

Unlike the feed client (fire-and-forget subscriptions), order operations need
request/response pairing: each call() awaits the response with the matching id.
Credentials are passed in by the caller (from environment variables); this
module never reads config files.
"""
import asyncio
import json


class DeribitRpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"deribit rpc error {code}: {message}")
        self.code = code
        self.message = message


AUTH_SAFETY_MARGIN_S = 60.0


class RpcClient:
    def __init__(self, ws, on_notification=None) -> None:
        self._ws = ws
        self._on_notification = on_notification  # sync callable(raw dict)
        self._req_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self.auth_expires_at_s: float | None = None

    async def call(self, method: str, params: dict):
        self._req_id += 1
        req_id = self._req_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        await self._ws.send(json.dumps(
            {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
        ))
        try:
            return await fut
        finally:
            self._pending.pop(req_id, None)

    async def read_loop(self) -> None:
        """Pump incoming messages: resolve pending calls, route notifications."""
        while True:
            msg = json.loads(await self._ws.recv())
            msg_id = msg.get("id")
            if msg_id is not None and msg_id in self._pending:
                fut = self._pending[msg_id]
                if fut.done():
                    continue
                if "error" in msg:
                    err = msg["error"]
                    fut.set_exception(DeribitRpcError(err["code"], err["message"]))
                else:
                    fut.set_result(msg.get("result"))
            elif self._on_notification is not None:
                self._on_notification(msg)

    async def authenticate(self, client_id: str, client_secret: str, now_s: float) -> None:
        result = await self.call("public/auth", {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        })
        self.auth_expires_at_s = now_s + result["expires_in"]

    def authenticated(self, now_s: float) -> bool:
        if self.auth_expires_at_s is None:
            return False
        return now_s < self.auth_expires_at_s - AUTH_SAFETY_MARGIN_S
