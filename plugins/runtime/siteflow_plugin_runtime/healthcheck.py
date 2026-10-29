from __future__ import annotations

import sys

import grpc

from siteflow_proto import plugin_pb2, plugin_pb2_grpc

from .config import Config


def main() -> int:
    config = Config.from_env()
    channel = grpc.insecure_channel(
        f"{config.bind if config.bind != '0.0.0.0' else '127.0.0.1'}:{config.port}",
        options=(("grpc.enable_http_proxy", 0),),
    )
    try:
        stub = plugin_pb2_grpc.PluginControlStub(channel)
        response = stub.Health(
            plugin_pb2.Empty(),
            timeout=2,
            metadata=(("x-plugin-token", config.token()),),
        )
    except Exception as exc:
        print(f"plugin health check failed: {exc}", file=sys.stderr)
        return 1
    finally:
        channel.close()
    return 0 if response.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
