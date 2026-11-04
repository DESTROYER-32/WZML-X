import asyncio
import logging
import os
import aiohttp
from aiohttp import web
from pyrogram.enums import ChatType
from pyrogram.types import Chat, Message

from .. import LOGGER, bot_loop
from ..core.config_manager import Config
from ..core.tg_client import TgClient
from ..helper.ext_utils.files_utils import clean_target, get_path_size, split_file
from ..helper.mirror_leech_utils.upload_utils.telegram_uploader import TelegramUploader

logger = logging.getLogger("wzmlx.internal_api")

_active_jobs = {}
_site = None


class SyntheticMessage:
    def __init__(self, chat_id: int, message_id: int = 1):
        self.id = message_id
        self.task_id = message_id
        self.chat = Chat(id=chat_id, type=ChatType.CHANNEL)
        self.from_user = None
        self.sender_chat = self.chat
        self.text = "Internal API Task"
        self.caption = ""
        self.link = f"https://t.me/c/{str(chat_id).replace('-100', '')}/{message_id}"


class InternalTaskListener:
    def __init__(self, job_id: str, dest_chat_id: int, split_size: int, callback_url: str = None):
        self.job_id = job_id
        self.mid = abs(hash(job_id)) % (10**9)
        self.dest_chat_id = dest_chat_id
        self.up_dest = dest_chat_id
        self.chat_thread_id = None
        self.split_size = split_size or 2097152000
        self.max_split_size = 4194304000 if TgClient.user else 2097152000
        self.transmission_mode = "both" if TgClient.user else "bot"
        self.is_cancelled = False
        self.is_file = False
        self.name = ""
        self.thumb = None
        self.user_id = Config.OWNER_ID or 0
        self.user_dict = {}
        self.file_details = {}
        self.excluded_extensions = []
        self.callback_url = callback_url
        self.message = SyntheticMessage(dest_chat_id, self.mid)
        self.client = TgClient.bot
        self.uploader = None
        self.result = None
        self.event = asyncio.Event()

    async def on_upload_complete(self, link, msgs_dict, total_files, corrupted):
        logger.info(f"[{self.job_id}] Upload completed. Total: {total_files}, Corrupted: {corrupted}")
        messages = []
        if self.uploader and hasattr(self.uploader, "_upload_seq"):
            for entry in self.uploader._upload_seq:
                if entry:
                    messages.append({
                        "chat_id": entry.get("chat_id"),
                        "msg_id": entry.get("msg_id"),
                        "link": entry.get("link"),
                        "file_name": entry.get("file_"),
                    })
        self.result = {
            "job_id": self.job_id,
            "status": "completed",
            "total_files": total_files,
            "corrupted": corrupted,
            "messages": messages,
            "error": None,
        }
        self.event.set()
        await self._notify_callback()

    async def on_upload_error(self, error):
        logger.error(f"[{self.job_id}] Upload failed: {error}")
        self.result = {
            "job_id": self.job_id,
            "status": "failed",
            "total_files": 0,
            "messages": [],
            "error": str(error),
        }
        self.event.set()
        await self._notify_callback()

    async def _notify_callback(self):
        if not self.callback_url:
            return
        try:
            async with aiohttp.ClientSession() as session:
                await session.post(self.callback_url, json=self.result, timeout=15)
                logger.info(f"[{self.job_id}] Notified callback {self.callback_url}")
        except Exception as e:
            logger.error(f"[{self.job_id}] Failed to notify callback {self.callback_url}: {e}")


async def _run_upload(listener: InternalTaskListener, file_path: str):
    try:
        if not os.path.exists(file_path):
            await listener.on_upload_error(f"Path does not exist: {file_path}")
            return

        is_file = os.path.isfile(file_path)
        listener.is_file = is_file
        listener.name = os.path.basename(file_path)

        # Handle splitting if needed
        if is_file:
            f_size = os.path.getsize(file_path)
            if f_size > listener.split_size:
                logger.info(f"[{listener.job_id}] File {file_path} ({f_size} bytes) exceeds split size, splitting...")
                work_dir = os.path.dirname(file_path)
                split_success = await split_file(file_path, listener.split_size, listener)
                if split_success:
                    file_path = work_dir
                    listener.is_file = False

        uploader = TelegramUploader(listener, file_path)
        listener.uploader = uploader
        await uploader.upload()

    except Exception as e:
        logger.exception(f"[{listener.job_id}] Exception during upload: {e}")
        await listener.on_upload_error(str(e))
    finally:
        _active_jobs.pop(listener.job_id, None)


async def handle_health(request):
    return web.json_response({
        "status": "ok",
        "service": "wzmlx-internal-api",
        "active_jobs": len(_active_jobs),
        "user_session": TgClient.user is not None,
    })


async def handle_status(request):
    job_id = request.query.get("job_id")
    if job_id:
        job = _active_jobs.get(job_id)
        if not job:
            return web.json_response({"error": "Job not found"}, status=404)
        return web.json_response({
            "job_id": job.job_id,
            "status": "uploading",
            "is_cancelled": job.is_cancelled,
        })
    return web.json_response({
        "active_jobs": list(_active_jobs.keys()),
    })


async def handle_upload(request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    job_id = data.get("job_id")
    file_path = data.get("file_path")
    dest_chat_id = data.get("dest_chat_id")
    split_size = data.get("split_size", 2097152000)
    callback_url = data.get("callback_url")

    if not job_id or not file_path or not dest_chat_id:
        return web.json_response({"error": "Missing required fields: job_id, file_path, dest_chat_id"}, status=400)

    if job_id in _active_jobs:
        return web.json_response({"error": f"Job {job_id} already in progress"}, status=409)

    listener = InternalTaskListener(
        job_id=job_id,
        dest_chat_id=int(dest_chat_id),
        split_size=split_size,
        callback_url=callback_url,
    )
    _active_jobs[job_id] = listener

    bot_loop.create_task(_run_upload(listener, file_path))

    return web.json_response({
        "status": "accepted",
        "job_id": job_id,
        "file_path": file_path,
        "dest_chat_id": dest_chat_id,
    }, status=202)


async def handle_cancel(request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    job_id = data.get("job_id")
    job = _active_jobs.get(job_id)
    if not job:
        return web.json_response({"error": "Job not found"}, status=404)

    job.is_cancelled = True
    if job.uploader:
        await job.uploader.cancel_task()

    return web.json_response({"status": "cancelled", "job_id": job_id})


async def start_internal_api():
    app = web.Application()
    app.router.add_get("/api/internal/health", handle_health)
    app.router.add_get("/api/internal/status", handle_status)
    app.router.add_post("/api/internal/upload", handle_upload)
    app.router.add_post("/api/internal/cancel", handle_cancel)

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8085)
    await site.start()
    LOGGER.info("WZML-X Internal REST API listening on 0.0.0.0:8085")
    return site


# Auto-start on module import in bot_loop
bot_loop.create_task(start_internal_api())
