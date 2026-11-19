"""Optional X11 transport. No browser-specific launch or profile knowledge."""
import asyncio
import os
import subprocess


class X11Desktop:
    def __init__(self, display: str):
        self.display = display

    async def read_clipboard(self) -> str:
        process = None
        try:
            async with asyncio.timeout(5):
                process = await asyncio.create_subprocess_exec(
                    "xclip", "-selection", "clipboard", "-o", "-target", "UTF8_STRING",
                    env={**os.environ, "DISPLAY": self.display},
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                )
                body = bytearray()
                while chunk := await process.stdout.read(65536):
                    if len(body) + len(chunk) > 4 * 1_048_576:
                        raise ValueError
                    body.extend(chunk)
                if await process.wait():
                    raise ValueError
                text = body.decode("utf-8")
                if len(text) > 1_048_576:
                    raise ValueError
                return text
        except TimeoutError:
            raise TimeoutError("Clipboard request timed out") from None
        except Exception:
            raise RuntimeError("Clipboard unavailable") from None
        finally:
            if process is not None and process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                # Drain without retaining output so a full pipe cannot block reaping.
                while await process.stdout.read(65536):
                    pass
                await process.wait()

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
