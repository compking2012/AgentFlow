"""Internal controller process. End users launch it through agentflow start/run."""
from __future__ import annotations

import asyncio
import logging
import signal
import ssl
import sys
import webbrowser
from contextlib import contextmanager

import uvicorn

from agentflow.application import Application
from agentflow.common import DomainError
from agentflow.control.instance import clear_instance, publish_instance
from agentflow.control.tls import PeerCertificateH11Protocol
from agentflow.settings import Settings

LISTENER_SHUTDOWN_TIMEOUT = 5


class ManagedServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self):
        # The composition root owns shutdown of both listeners and the writer actor.
        yield

    async def shutdown(self, sockets=None):
        await super().shutdown(sockets)
        # Uvicorn cancels timed-out requests without joining their cleanup. Keep the
        # store alive until those handlers finish any accepted accounting writes.
        if self.server_state.tasks:
            await asyncio.gather(*tuple(self.server_state.tasks), return_exceptions=True)


async def serve(settings: Settings, open_browser: bool = False, *, configuration=None):
    application = await (Application(settings, configuration=configuration) if configuration else Application(settings)).start()
    owner = ManagedServer(uvicorn.Config(application.owner_app, host=settings.host, port=settings.port,
        proxy_headers=False, access_log=False, log_level="warning", lifespan="off",
        timeout_graceful_shutdown=LISTENER_SHUTDOWN_TIMEOUT))
    servers = [owner]
    if application.executor_app:
        servers.append(ManagedServer(uvicorn.Config(application.executor_app,
            host=settings.executor_host, port=settings.executor_port, proxy_headers=False,
            access_log=False, log_level="warning", lifespan="off", http=PeerCertificateH11Protocol,
            timeout_graceful_shutdown=LISTENER_SHUTDOWN_TIMEOUT,
            ssl_certfile=str(settings.tls_certificate), ssl_keyfile=str(settings.tls_private_key),
            ssl_ca_certs=str(settings.tls_client_ca), ssl_cert_reqs=ssl.CERT_OPTIONAL)))
    loop = asyncio.get_running_loop()
    def stop():
        # Wake streaming responses before draining HTTP; runtime persistence closes
        # separately below and is not subject to the listener timeout.
        application.owner_app.state.shutdown_event.set()
        for server in servers:
            server.should_exit = True
    application.owner_app.state.shutdown_callback = stop
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop)
    tasks = [asyncio.create_task(server.serve()) for server in servers]
    try:
        for _ in range(200):
            if all(server.started for server in servers):
                break
            if any(task.done() for task in tasks):
                await asyncio.gather(*tasks)
                return
            await asyncio.sleep(.025)
        if not all(server.started for server in servers):
            raise DomainError("listener_start_failed", "Local listeners did not become ready", 503)
        if configuration:
            publish_instance(settings.data_dir)
        url = settings.origin + "/#bootstrap=" + application.owner_app.state.tokens.bootstrap_code
        # One-use local launch information, not an access log or persistent credential.
        print(f"AgentFlow ready: {url}", flush=True)
        if open_browser:
            await asyncio.to_thread(webbrowser.open, url)
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            await task
    finally:
        stop()
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            try:
                await application.close()
            finally:
                if configuration:
                    clear_instance()
                for sig in (signal.SIGINT, signal.SIGTERM):
                    loop.remove_signal_handler(sig)


def main():
    if len(sys.argv) != 1:
        raise SystemExit("内部服务不接受命令行配置；请编辑 ~/.config/agentflow/config.toml")
    from agentflow.configuration import load_configuration
    logging.basicConfig(level=logging.WARNING)
    try:
        configuration = load_configuration(create=True)
        asyncio.run(serve(configuration.settings, configuration=configuration))
    except (DomainError, ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
