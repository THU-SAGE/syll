"""Status API route."""

import time

from fastapi import APIRouter, Request

from syll import __version__

router = APIRouter(tags=["status"])

_start_time = time.time()


@router.get("/status")
async def get_status(request: Request):
    """Return system status."""
    config = request.app.state.config
    uptime = int(time.time() - _start_time)

    return {
        "version": __version__,
        "model": config.models.chat.model,
        "workspace": str(config.workspace_path),
        "uptime_seconds": uptime,
        "gateway": {
            "host": config.gateway.host,
            "port": config.gateway.port,
        },
        "channels": {
            "telegram": config.channels.telegram.enabled,
            "discord": config.channels.discord.enabled,
            "whatsapp": config.channels.whatsapp.enabled,
            "feishu": config.channels.feishu.enabled,
        },
    }
