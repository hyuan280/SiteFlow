import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import grpc
import pytest

from siteflow_proto import plugin_pb2, plugin_pb2_grpc
from worker.plugin_client import cancellable_call, execute_step, recognize_captcha


@pytest.mark.parametrize("kind", ["step", "captcha"])
def test_real_rpc_future_canceled_and_thread_settled(kind):
    entered = threading.Event()
    stopped = threading.Event()

    class Service(plugin_pb2_grpc.PluginExecutionServicer):
        def wait(self, context):
            entered.set()
            while context.is_active():
                stopped.wait(0.01)
            stopped.set()

        def ExecuteStep(self, request, context):
            self.wait(context)
            return plugin_pb2.StepResult(status="success")

        def RecognizeCaptcha(self, request, context):
            self.wait(context)
            return plugin_pb2.CaptchaResult(text="late")

    server = grpc.server(ThreadPoolExecutor(max_workers=2))
    plugin_pb2_grpc.add_PluginExecutionServicer_to_server(Service(), server)
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    async def run():
        kwargs = dict(execution_id="test", plugin_id="demo", feature_key="plugin.demo.test", config={},
                      site_origin="http://fixture", current_url="http://fixture", deadline_ms=30000,
                      broker_url="", broker_token="") if kind == "step" else dict(method_key="demo", image=b"", charset="", length=0)
        task = asyncio.create_task(cancellable_call(execute_step if kind == "step" else recognize_captcha,
            f"127.0.0.1:{port}", "public-test-token", timeout=30, **kwargs))
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await asyncio.to_thread(stopped.wait, 2)
    try:
        asyncio.run(run())
    finally:
        server.stop(0).wait()
