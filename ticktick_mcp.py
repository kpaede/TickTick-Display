"""Read-only connection to the official TickTick MCP server."""

import asyncio
import fcntl
import json
import logging
import os
import tempfile
import threading
import time
import webbrowser
from contextlib import asynccontextmanager
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from agenda_format import TIMEZONE, format_agenda, habit_due, habit_period, result_items, task_overdue
from runtime_paths import runtime_root

import httpx2
from mcp import Client
from mcp.client.auth import AuthorizationCodeResult, OAuthClientProvider
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
from mcp.shared.exceptions import MCPError

SERVER = "https://mcp.ticktick.com/"
CALLBACK = "http://127.0.0.1:8765/callback"
AUTH_FILE = runtime_root() / "data" / "ticktick-oauth.json"
READ_TOOLS = frozenset({
    "list_undone_tasks_by_time_query", "list_habits", "get_habit",
    "get_habit_checkins", "list_habit_sections",
    "list_projects", "get_project_with_undone_tasks",
})


class TickTickOAuthProvider(OAuthClientProvider):
    allow_write_scope = False

    def _select_authorization_server(self, advertised):
        selected = super()._select_authorization_server(advertised)
        # TickTick's resource metadata has a trailing slash, its issuer does not.
        # Pin the known issuer; keep the SDK's issuer, state and PKCE checks.
        if selected not in {"https://ticktick.com", "https://ticktick.com/"}:
            raise RuntimeError("Unerwarteter TickTick-Anmeldeserver.")
        metadata = self.context.protected_resource_metadata
        if "tasks:read" not in (metadata.scopes_supported or []):
            raise RuntimeError("TickTick bietet keinen Lesezugriff an.")
        if not self.allow_write_scope:
            metadata.scopes_supported = ["tasks:read"]
        return "https://ticktick.com"


class FileStorage:
    def __init__(self, path=AUTH_FILE):
        self.path = path
        self.data = json.loads(path.read_text()) if path.exists() else {}

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=self.path.parent)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self.data, f)
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    async def get_tokens(self):
        data = self.data.get("tokens")
        if data is None:
            return None
        data = dict(data)
        if self.data.get("expires_at") is not None:
            remaining = int(self.data["expires_at"] - time.time())
            data["expires_in"] = remaining if remaining else -1
        return OAuthToken.model_validate(data)

    async def set_tokens(self, tokens):
        self.data["tokens"] = tokens.model_dump(mode="json")
        self.data["expires_at"] = time.time() + tokens.expires_in if tokens.expires_in is not None else None
        self.save()

    async def get_client_info(self):
        data = self.data.get("client_info")
        return OAuthClientInformationFull.model_validate(data) if data else None

    async def set_client_info(self, client_info):
        self.data["client_info"] = client_info.model_dump(mode="json")
        self.save()


class BrowserLogin:
    def __init__(self, allowed):
        self.allowed = allowed
        self.server = None
        self.future = None

    async def redirect(self, url):
        if not self.allowed:
            raise RuntimeError("TickTick-Anmeldung erforderlich: ./anzeigen --mcp-login --allow-write-scope starten.")
        loop = asyncio.get_running_loop()
        self.future = loop.create_future()
        future = self.future
        expected_state = parse_qs(urlparse(url).query)["state"][0]

        class CallbackHandler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass  # Authorization codes must never be logged.

            def do_GET(self):
                parsed = urlparse(self.path)
                params = parse_qs(parsed.query)
                valid = parsed.path == "/callback" and params.get("state") == [expected_state]
                if not valid or future.done():
                    self.send_response(400)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write("Anmeldung empfangen. Du kannst dieses Fenster schließen.".encode())
                def finish():
                    if not future.done():
                        future.set_result(params)
                loop.call_soon_threadsafe(finish)

        self.server = HTTPServer(("127.0.0.1", 8765), CallbackHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        print("TickTick-Anmeldung im Browser geöffnet. Bitte die Freigabe bestätigen.", flush=True)
        webbrowser.open(url)

    async def callback(self):
        try:
            params = await asyncio.wait_for(self.future, 600)
        except asyncio.TimeoutError as exc:
            raise RuntimeError("TickTick-Anmeldung nicht innerhalb von 10 Minuten abgeschlossen.") from exc
        if "error" in params or "code" not in params:
            raise RuntimeError("Die TickTick-Anmeldung wurde nicht freigegeben.")
        return AuthorizationCodeResult(code=params["code"][0], state=params["state"][0],
                                       iss=params.get("iss", [None])[0])

    def close(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()


@asynccontextmanager
async def oauth_lock(path=AUTH_FILE, timeout=180):
    """Serialize token refreshes between the service and manual CLI calls."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as lock:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Der TickTick-Zugriff ist gerade belegt.")
                await asyncio.sleep(0.1)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


@asynccontextmanager
async def connection(login=False, allow_write_scope=False):
    async with oauth_lock():
        async with _connection(login, allow_write_scope) as client:
            yield client


@asynccontextmanager
async def _connection(login=False, allow_write_scope=False):
    browser = BrowserLogin(login)
    logging.getLogger("mcp.client.auth.oauth2").setLevel(logging.CRITICAL)
    storage = FileStorage()
    if allow_write_scope and not storage.data.get("write_scope_authorized"):
        storage.data["write_scope_authorized"] = True
        storage.save()
    allow_write_scope = allow_write_scope or storage.data.get("write_scope_authorized", False)
    if login and allow_write_scope and storage.data.get("tokens"):
        scopes = set((storage.data["tokens"].get("scope") or "").split())
        if not {"tasks:read", "tasks:write"} <= scopes:
            storage.data.pop("tokens", None)
            storage.data.pop("expires_at", None)
            storage.save()
    oauth = TickTickOAuthProvider(
        server_url=SERVER,
        client_metadata=OAuthClientMetadata(
            client_name="TickTick USB Display", redirect_uris=[CALLBACK],
            # The server requires tasks:write even for initialize (HTTP 403).
            # Actual tool calls are restricted by READ_TOOLS above.
            scope="tasks:read tasks:write" if allow_write_scope else "tasks:read",
            token_endpoint_auth_method="none",
        ),
        storage=storage, redirect_handler=browser.redirect, callback_handler=browser.callback,
    )
    oauth.allow_write_scope = allow_write_scope
    try:
        async with httpx2.AsyncClient(auth=oauth, timeout=httpx2.Timeout(30, read=120)) as http:
            transport = streamable_http_client(SERVER, http_client=http)
            async with Client(transport, mode="legacy", read_timeout_seconds=120) as client:
                yield client
    except ExceptionGroup as exc:
        error = exc
        while isinstance(error, ExceptionGroup) and len(error.exceptions) == 1:
            error = error.exceptions[0]
        if isinstance(error, RuntimeError):
            raise error from None
        if isinstance(error, MCPError):
            raise RuntimeError(f"TickTick-MCP: {error.error.message}") from None
        raise RuntimeError(f"TickTick-MCP-Verbindung fehlgeschlagen ({type(error).__name__}).") from None
    finally:
        browser.close()


async def read_tool(client, name, arguments):
    if name not in READ_TOOLS:
        raise RuntimeError(f"MCP-Werkzeug {name} ist nicht als Lesezugriff freigegeben.")
    result = await client.call_tool(name, arguments)
    if result.is_error:
        raise RuntimeError(f"TickTick konnte {name} nicht ausführen.")
    if result.structured_content is not None:
        return result.structured_content
    blocks = [b.text for b in result.content if b.type == "text"]
    if len(blocks) == 1:
        try:
            return json.loads(blocks[0])
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Unerwartetes Datenformat bei {name}.") from exc
    raise RuntimeError(f"Unerwartetes Datenformat bei {name}.")


async def inspect_tools(allow_write_scope=False):
    async with connection(login=True, allow_write_scope=allow_write_scope) as client:
        tools = []
        cursor = None
        while True:
            result = await client.list_tools(cursor=cursor)
            tools.extend(result.tools)
            cursor = result.next_cursor
            if not cursor:
                break
        path = Path(__file__).parent / ".tools" / "ticktick-tools.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps([t.model_dump(mode="json") for t in tools], ensure_ascii=False, indent=2))
        names = {t.name for t in tools}
        print(f"TickTick verbunden. {len(tools)} MCP-Werkzeuge verfügbar.")
        print("Habits verfügbar:", "ja" if "list_habits" in names else "nein")
        print("Kalenderereignisse:", "Werkzeuge vorhanden" if any(
            w in t.name.lower() for t in tools for w in ("calendar", "event")
        ) else "keine Werkzeuge angeboten")


async def agenda_text():
    try:
        async with asyncio.timeout(180):
            return await _agenda_text()
    except TimeoutError as exc:
        raise RuntimeError("TickTick-Abruf dauert zu lange; die bisherige Anzeige bleibt erhalten.") from exc


async def _agenda_text():
    now = datetime.now(TIMEZONE)
    today = now.date()
    stamp = int(today.strftime("%Y%m%d"))
    async with connection() as client:
        task_data, habit_data, project_data = await asyncio.gather(
            read_tool(client, "list_undone_tasks_by_time_query", {
                "query_command": "today", "client_timezone": str(TIMEZONE),
            }),
            read_tool(client, "list_habits", {}),
            read_tool(client, "list_projects", {}),
        )
        tasks = result_items(task_data)
        # Date-range searches are limited to 14 days. Read every list (including
        # the inbox) so even much older overdue tasks are included.
        semaphore = asyncio.Semaphore(4)
        async def project_tasks(project):
            async with semaphore:
                data = await read_tool(client, "get_project_with_undone_tasks", {
                    "project_id": project["id"],
                })
            items = data.get("tasks") if isinstance(data, dict) else None
            if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                raise RuntimeError("TickTick hat ein unerwartetes Aufgabenformat geliefert.")
            return items
        projects = result_items(project_data)
        lists = await asyncio.gather(*(project_tasks(p) for p in projects if p.get("kind") != "NOTE"))
        overdue = {}
        for task in [*tasks, *(t for items in lists for t in items)]:
            if task.get("status") in (None, 0) and task.get("kind") != "NOTE" and task_overdue(task, today):
                overdue[task["id"]] = task
        tasks = [t for t in tasks if not task_overdue(t, today) and t.get("status") in (None, 0)
                 and t.get("kind") != "NOTE"]
        habits = [h for h in result_items(habit_data) if habit_due(h, today)]
        checkins = []
        if habits:
            start = min(habit_period(h, today)[0] for h in habits)
            checkins = result_items(await read_tool(client, "get_habit_checkins", {
                "habit_ids": [h["id"] for h in habits],
                "from_stamp": int(start.strftime("%Y%m%d")), "to_stamp": stamp,
                "client_timezone": str(TIMEZONE),
            }))
    updated = datetime.now(TIMEZONE)
    if updated.date() != today:
        raise RuntimeError("Tageswechsel während des TickTick-Abrufs; neuer Abruf erforderlich.")
    return format_agenda(tasks, habits, checkins, updated, overdue_tasks=list(overdue.values()))
