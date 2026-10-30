"""Optional X11 transport. No browser-specific launch or profile knowledge."""
import asyncio
import os
import subprocess


class X11Desktop:
    def __init__(self, display: str):
        self.display = display

    async def screenshot(self) -> bytes:
        def capture():
            import mss
            from mss.tools import to_png
            with mss.mss(display=self.display) as screen:
                frame = screen.grab(screen.monitors[0])
                return to_png(frame.rgb, frame.size)
        return await asyncio.to_thread(capture)

    async def click(self, x: float, y: float) -> None:
        process = await asyncio.create_subprocess_exec(
            "xdotool", "mousemove", str(round(x)), str(round(y)), "sleep", "0.1", "click", "1",
            env={**os.environ, "DISPLAY": self.display},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
            if process.returncode:
                raise RuntimeError("Desktop input failed")
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
