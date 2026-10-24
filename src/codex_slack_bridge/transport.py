from __future__ import annotations

import json
import urllib.error
import urllib.request

from .config import BridgeError, identifier, routerUrl


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(_self,
                         _req,
                         _fp,
                         _code,
                         _msg,
                         _headers,
                         _newurl):
        #Codex: Do not forward the Agent bearer credential to another origin.
        return None


class RouterClient:
    def __init__(_self,
                 _url: str,
                 _agent_id: str,
                 _token: str):
        _self.base_ = f"{routerUrl(_url)}/v1/agents/{identifier(_agent_id)}"
        _self.token_ = _token
        _self.opener_ = urllib.request.build_opener(NoRedirect)

    def call(_self,
             _operation: str,
             _payload: dict | None = None) -> dict:
        if _operation not in {"bindings",
                              "notify",
                              "claim",
                              "result",
                              "response",
                              "unbind",
                              "rebind"}:
            raise BridgeError("Unknown Router operation")
        body = json.dumps(_payload or {}).encode()
        request = urllib.request.Request(f"{_self.base_}/{_operation}",
                                         data=body,
                                         method="POST",
                                         headers={"Authorization": f"Bearer {_self.token_}", "Content-Type": "application/json"})
        try:
            with _self.opener_.open(request, timeout=15) as response:
                return json.loads(response.read(1024 * 1024))
        except urllib.error.HTTPError as exc:
            raise BridgeError(f"Router rejected {_operation} (HTTP {exc.code}); inspect Router logs/status") from None
        except (OSError, ValueError, urllib.error.URLError):
            raise BridgeError(f"Router {_operation} acknowledgement unavailable; delivery may be unknown") from None
