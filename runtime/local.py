"""The tule-agent's local control endpoint: a Unix socket for commands run on the machine.

`tule snapshot` on the machine connects here (the control plane is not
needed). One JSON object per line in each direction:

    {"op": "snapshot", "reason": "manual"}  -> {"snapshot": id, "changed": bool}
    {"op": "status"}                        -> {"workspace", "ready", "connected", "snapshot"}

Errors come back as {"error": "..."}. The socket is root-only
(TULE_AGENT_SOCKET, default /run/tule/agent.sock).
"""

from __future__ import annotations

import asyncio
import json
import os

SOCKET = os.environ.get("TULE_AGENT_SOCKET", "/run/tule/agent.sock")


async def serve(agent, path: str = SOCKET):
    """Serve until cancelled; `agent` is the tule_agent.Agent (its lock, workspace and booted state)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    server = await asyncio.start_unix_server(lambda r, w: _client(agent, r, w), path)
    os.chmod(path, 0o600)
    async with server:
        await server.serve_forever()


async def _client(agent, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    try:
        while line := await reader.readline():
            try:
                msg = json.loads(line)
                out = await OPS[msg.get("op")](agent, msg)
            except KeyError:
                out = {"error": f"unknown op {msg.get('op')!r}; known: {', '.join(sorted(OPS))}"}
            except Exception as e:  # every failure goes back to the caller
                out = {"error": f"{type(e).__name__}: {e}"}
            writer.write(json.dumps(out).encode() + b"\n")
            await writer.drain()
    except (ConnectionError, ValueError):
        pass
    finally:
        writer.close()


async def _snapshot(agent, msg: dict) -> dict:
    if agent.booted is None:
        return {"error": "the workspace is not restored yet; try again in a few seconds"}
    async with agent.lock:
        sid = await asyncio.to_thread(agent.workspace.snapshot, str(msg.get("reason") or "manual"))
    return {"snapshot": sid or agent.workspace.last_id, "changed": sid is not None}


async def _status(agent, msg: dict) -> dict:
    return {"workspace": agent.workspace.root, "ready": agent.booted is not None,
            "connected": agent.ws is not None, "snapshot": agent.workspace.last_id}


OPS = {"snapshot": _snapshot, "status": _status}
