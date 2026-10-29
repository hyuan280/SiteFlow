from __future__ import annotations

import logging
import signal
import sys
import threading
from concurrent import futures

import grpc

from siteflow_proto import plugin_pb2_grpc

from .config import Config
from .control import PluginControlServicer, PluginState
from .execution import (
    CaptchaHandler,
    PluginExecutionServicer,
    StepHandler,
    load_handler,
)
from .registration import RegistrationClient
from .secrets import SecretProviderServicer

logger = logging.getLogger(__name__)


def build_server(
    config: Config,
    state: PluginState,
    *,
    port: int | None = None,
    execute_handler: StepHandler | None = None,
    captcha_handler: CaptchaHandler | None = None,
) -> tuple[grpc.Server, int]:
    if config.app_path and config.app_path not in sys.path:
        sys.path.insert(0, config.app_path)
    if execute_handler is None and config.handler:
        execute_handler = load_handler(config.handler)  # type: ignore[assignment]
    if captcha_handler is None and config.captcha_handler:
        captcha_handler = load_handler(config.captcha_handler)  # type: ignore[assignment]
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    plugin_pb2_grpc.add_SecretProviderServicer_to_server(
        SecretProviderServicer(state,
            load_handler(config.credential_handler) if config.credential_handler else None,
            load_handler(config.totp_handler) if config.totp_handler else None), server)
    plugin_pb2_grpc.add_PluginControlServicer_to_server(
        PluginControlServicer(state), server
    )
    plugin_pb2_grpc.add_PluginExecutionServicer_to_server(
        PluginExecutionServicer(state, execute_handler, captcha_handler), server
    )
    listen_port = config.port if port is None else port
    bound_port = server.add_insecure_port(f"{config.bind}:{listen_port}")
    if bound_port == 0:
        raise RuntimeError(f"cannot bind plugin gRPC server on port {listen_port}")
    return server, bound_port


def serve(config: Config) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    state = PluginState.load(config)
    server, port = build_server(config, state)
    server.start()
    logger.info("plugin %s gRPC server listening on %s:%s", config.plugin_id, config.bind, port)

    stop = threading.Event()
    registration_thread = None
    if config.register_enabled:
        registrar = RegistrationClient(config, state.manifest)
        registration_thread = threading.Thread(
            target=registrar.run_forever,
            args=(stop,),
            name=f"plugin-register-{config.plugin_id}",
            daemon=True,
        )
        registration_thread.start()

    def _stop(signum, frame):  # type: ignore[no-untyped-def]
        logger.info("plugin %s stopping on signal %s", config.plugin_id, signum)
        stop.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    try:
        stop.wait()
    finally:
        stop.set()
        if registration_thread is not None:
            registration_thread.join(timeout=5)
        server.stop(grace=2).wait()
