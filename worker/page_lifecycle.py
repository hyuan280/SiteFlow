from __future__ import annotations

import asyncio
from urllib.parse import urlsplit


class JobPages:
    """Own only the root page and its popup descendants, never context siblings."""

    def __init__(self, page, lane, job_key):
        self.active = page
        self.lane = lane
        self.job_key = job_key
        self.closed = False
        self.pages = []
        self.tasks = set()
        self.listeners = []
        self.own(page)

    def own(self, page):
        if page in self.pages:
            return
        self.pages.append(page)

        def popup(child):
            self.own(child)
            task = asyncio.create_task(self.adopt(child, page.url))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)

        page.on("popup", popup)
        self.listeners.append((page, popup))

    async def adopt(self, page, opener_url):
        try:
            if self.closed:
                await page.close()
                return
            await page.wait_for_load_state("domcontentloaded", timeout=5000)
            source = urlsplit(opener_url)
            target = urlsplit(page.url)
            if not self.closed and (source.scheme, source.netloc) == (target.scheme, target.netloc):
                self.active = page
                if self.job_key:
                    self.lane.task_pages[self.job_key] = page
            else:
                await page.close()
        except Exception:
            await page.close()

    async def close(self):
        self.closed = True
        # Keep popup listeners until roots close so queued late popups are owned.
        while True:
            pages = list(self.pages)
            await asyncio.gather(*(page.close() for page in pages), return_exceptions=True)
            if self.tasks:
                await asyncio.gather(*list(self.tasks), return_exceptions=True)
            if len(pages) == len(self.pages):
                break
        for page, listener in self.listeners:
            page.remove_listener("popup", listener)
        if self.job_key:
            self.lane.task_pages.pop(self.job_key, None)
