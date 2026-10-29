"""Authenticated, capability-gated secret handlers, separate from ordinary execution."""
import grpc

from siteflow_proto import plugin_pb2, plugin_pb2_grpc
from .control import _authorize_state


class SecretProviderServicer(plugin_pb2_grpc.SecretProviderServicer):
    def __init__(self, state, credential_handler=None, totp_handler=None):
        self.state = state
        self.credential_handler = credential_handler
        self.totp_handler = totp_handler

    def _resolve(self, request, context, capability, handler):
        _authorize_state(self.state.config, context)
        if capability not in self.state.manifest.get("capabilities", []) or handler is None:
            context.abort(grpc.StatusCode.PERMISSION_DENIED, "Secret provider unavailable")
        if request.job_id <= 0 or request.site_id <= 0 or not request.step_key:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "Invalid secret scope")
        try:
            return handler({"job_id": request.job_id, "site_id": request.site_id,
                            "step_key": request.step_key, "site_origin": request.site_origin,
                            "match_mode": request.match_mode, "custom_match": request.custom_match,
                            "service_hint": request.service_hint, "username_hint": request.username_hint})
        except Exception:
            context.abort(grpc.StatusCode.INTERNAL, "Secret provider failed")

    def ResolveCredential(self, request, context):
        result = self._resolve(request, context, "secret.credentials", self.credential_handler)
        if not isinstance(result, dict) or not all(isinstance(result.get(key), str) and 0 < len(result[key]) <= 4096 for key in ("username", "password")):
            context.abort(grpc.StatusCode.INTERNAL, "Invalid secret provider result")
        return plugin_pb2.CredentialResult(username=result["username"], password=result["password"])

    def GenerateTotp(self, request, context):
        result = self._resolve(request, context, "secret.totp", self.totp_handler)
        if not isinstance(result, dict) or not isinstance(result.get("code"), str) or not result["code"].isascii() or not result["code"].isdigit() or not 4 <= len(result["code"]) <= 10:
            context.abort(grpc.StatusCode.INTERNAL, "Invalid secret provider result")
        return plugin_pb2.TotpResult(code=result["code"])
