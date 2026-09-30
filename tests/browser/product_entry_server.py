"""Production product APIs on disposable data, with scheduling deliberately off.

Imports inspect real committed repositories. Creates/changes stop at the durable
preparing state; this fixture never fabricates successful agents or test results.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import shutil
import socket
import subprocess
from pathlib import Path

import uvicorn
from fastapi import Request
from fastapi.responses import JSONResponse

from agentflow.application import Application
from agentflow.control.product_models import ModelSetupRequest
from agentflow.settings import Settings


def committed_project(destination: Path, *, native=False, unknown=False):
    source = Path(__file__).resolve().parents[2] / 'src/agentflow/resources/web_api_starter'
    if unknown:
        destination.mkdir()
        (destination / 'README.md').write_text('A project without recognized platform markers.\n')
    else:
        shutil.copytree(source, destination)
    if native:
        ios = destination / 'ios/Reading.xcodeproj'
        ios.mkdir(parents=True)
        (ios / 'project.pbxproj').write_text('// Static platform marker; not an executable project.\n')
        macos = destination / 'macos/Reading.xcodeproj'
        macos.mkdir(parents=True)
        (macos / 'project.pbxproj').write_text('SDKROOT = macosx; // Static marker only.\n')
        android = destination / 'android/app'
        android.mkdir(parents=True)
        (android / 'build.gradle').write_text("plugins { id 'com.android.application' }\n")
    environment = {**os.environ, 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull}
    for arguments in [
        ['init', '--quiet', '-b', 'main'], ['add', '--all'],
        ['-c', 'user.name=AgentFlow browser fixture', '-c', 'user.email=fixture@localhost',
         '-c', 'core.hooksPath=/dev/null', 'commit', '--quiet', '-m', 'Static import fixture'],
    ]:
        subprocess.run(['git', *arguments], cwd=destination, env=environment, check=True, capture_output=True)


async def main(directory: Path):
    directory = directory.resolve()
    private_home = directory / 'home'
    private_home.mkdir(parents=True, mode=0o700)
    os.environ['HOME'] = str(private_home)
    os.environ['USERPROFILE'] = str(private_home)
    source_project, multi_project = directory / 'existing-product', directory / 'multiplatform-product'
    unknown_project = directory / 'unknown-product'
    committed_project(source_project)
    committed_project(multi_project, native=True)
    committed_project(unknown_project, unknown=True)
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    settings = Settings(data_dir=directory / 'data', port=sock.getsockname()[1],
        dashboard_dir=Path(os.environ.get('AGENTFLOW_BROWSER_DASHBOARD_DIR') or
                           Path(__file__).resolve().parents[2] / 'src/agentflow/web'))
    application = await Application(settings).start(schedule=False)
    try:
        await application.products.configure_model(ModelSetupRequest(role='both', provider='openai_compatible',
            base_url='https://browser-fixture.invalid/v1', model='explicit-browser-fixture',
            api_key='fixture-credential-never-dispatched'), 'entry-fixture-model')
        app = application.owner_app
        fixture_key = secrets.token_urlsafe(32)

        @app.get('/__fixture/state')
        async def state(request: Request):
            if request.headers.get('x-fixture-key') != fixture_key:
                return JSONResponse({}, 403)
            return {'scope': 'production_api_without_scheduler',
                    **{kind: await application.store.list(kind) for kind in (
                        'product', 'product_change', 'product_management_operation', 'run', 'attempt', 'model_invocation', 'check', 'delivery')},
                    'source_project_exists': source_project.is_dir()}

        print(json.dumps({'origin': settings.origin, 'bootstrap': app.state.tokens.bootstrap_code,
                          'fixture_key': fixture_key, 'directory': str(directory),
                          'source_project': str(source_project), 'multi_project': str(multi_project),
                          'unknown_project': str(unknown_project)}), flush=True)
        server = uvicorn.Server(uvicorn.Config(app, host=settings.host, port=settings.port,
                                              log_level='warning', access_log=False))
        await server.serve(sockets=[sock])
    finally:
        await application.close()
        sock.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--directory', type=Path, required=True)
    asyncio.run(main(parser.parse_args().directory))
