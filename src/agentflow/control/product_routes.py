"""Authenticated owner commands shared by CLI and WebUI product creation."""
import asyncio
from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse

from agentflow.common import DomainError
from agentflow.control.api import body, command_key
from agentflow.control.product_models import (
    ModelSetupRequest,
    ProductChangeRequest,
    ProductDiagnosisRequest,
    ProductLanguageRequest,
    ProductManagementRequest,
    ProductRequest,
    ProductTestRuntimeRepairRequest,
    ProductUpdateRequest,
)


def product_router(service):
    router = APIRouter()

    @router.get('/api/v1/product_setup')
    async def setup():
        return await service.setup_status()

    def execution_configuration():
        if service.configuration is None:
            raise DomainError('configuration_unavailable', '请通过 agentflow start 加载固定配置文件后使用执行设置。', 409)
        return service.configuration

    @router.get('/api/v1/settings/execution')
    async def execution_settings():
        return execution_configuration().execution_settings()

    @router.post('/api/v1/settings/execution')
    async def update_execution_settings(request: Request):
        return await execution_configuration().update_execution_settings(
            await body(request), command_key(request), service.store)

    @router.post('/api/v1/controller/shutdown')
    async def shutdown(request: Request):
        callback = getattr(request.app.state, 'shutdown_callback', None)
        if callback is None:
            raise DomainError('shutdown_unavailable', 'This listener is managed by its host process')
        asyncio.get_running_loop().call_later(.25, callback)
        return {'state': 'stopping'}

    @router.post('/api/v1/product_setup/models')
    async def models(request: Request):
        return await service.configure_model(ModelSetupRequest.model_validate(await body(request)), command_key(request))

    @router.post('/api/v1/product_setup/local_execution', status_code=202)
    async def prepare():
        return await service.prepare_local()

    @router.post('/api/v1/products', status_code=202)
    async def create(request: Request):
        return await service.submit(ProductRequest.model_validate(await body(request)), command_key(request))

    @router.post('/api/v1/products/diagnose')
    async def diagnose(request: Request):
        payload = ProductDiagnosisRequest.model_validate(await body(request))
        return await service.lifecycle.diagnose(payload.project_path)

    @router.get('/api/v1/products/{product_id}/changes')
    async def changes(product_id: str):
        return {'items': await service.lifecycle.changes(product_id)}

    @router.post('/api/v1/products/{product_id}/changes', status_code=202)
    async def create_change(product_id: str, request: Request):
        return await service.lifecycle.submit_change(product_id,
            ProductChangeRequest.model_validate(await body(request)), command_key(request))

    @router.post('/api/v1/products/{product_id}/documents/migrate')
    async def migrate_documents(product_id: str):
        from agentflow.control.project_documents import ProjectDocumentService
        return await ProjectDocumentService(service.store, service.workflow.artifacts,
                                            service.settings).migrate(product_id)

    @router.get('/api/v1/products')
    async def products(view: Literal['active', 'deleted', 'all'] = 'active'):
        selected = [p for p in await service.store.list('product')
                    if view == 'all' or bool(p.get('deleted_at')) == (view == 'deleted')]
        management = await service.management.read_view() if selected else None
        return {'items': [await service.detail(p['id'], management_view=management) for p in selected]}

    @router.get('/api/v1/products/{product_id}')
    async def detail(product_id: str):
        return await service.detail(product_id)

    @router.patch('/api/v1/products/{product_id}')
    async def update(product_id: str, request: Request):
        return await service.management.mutate(product_id,
            ProductUpdateRequest.model_validate(await body(request)), command_key(request))

    @router.delete('/api/v1/products/{product_id}')
    async def delete(product_id: str, request: Request):
        return await service.management.mutate(product_id,
            ProductManagementRequest.model_validate(await body(request)), command_key(request), 'delete')

    @router.post('/api/v1/products/{product_id}/restore')
    async def restore(product_id: str, request: Request):
        return await service.management.mutate(product_id,
            ProductManagementRequest.model_validate(await body(request)), command_key(request), 'restore')

    @router.post('/api/v1/products/{product_id}/restart', status_code=202)
    async def restart(product_id: str, request: Request):
        return await service.management.restart(product_id,
            ProductManagementRequest.model_validate(await body(request)), command_key(request))

    @router.post('/api/v1/products/{product_id}/language')
    async def language(product_id: str, request: Request):
        return await service.update_language(product_id,
            ProductLanguageRequest.model_validate(await body(request)), command_key(request))

    @router.post('/api/v1/products/{product_id}/retry', status_code=202)
    async def retry(product_id: str, request: Request):
        return await service.retry_prepare(product_id, command_key(request))

    @router.post('/api/v1/products/{product_id}/test-runtime-repair', status_code=202)
    async def test_runtime_repair(product_id: str, request: Request):
        return await service.test_repair.repair_runtime(product_id,
            ProductTestRuntimeRepairRequest.model_validate(await body(request)), command_key(request))

    @router.get('/api/v1/products/{product_id}/download')
    async def download(product_id: str, run_id: str | None = None):
        product = await service.detail(product_id)
        if run_id:
            receipt = await service.exporter.verify_version(product, run_id)
            return FileResponse(receipt['archive_path'], media_type='application/zip', filename='agentflow-product.zip')
        if product.get('needs_restart'):
            raise DomainError('product_restart_required', '当前配置尚未交付；可指定旧运行下载历史版本。')
        if product['state'] != 'completed' or not product.get('delivery'):
            raise DomainError('product_not_delivered', 'Product has not passed its delivery gates')
        await service.exporter.verify_product(product)
        return FileResponse(product['delivery']['archive_path'], media_type='application/zip', filename='agentflow-product.zip')

    @router.post('/api/v1/products/{product_id}/launch')
    async def launch(product_id: str):
        if not service.launcher:
            raise DomainError('preview_unavailable', 'Local product preview is unavailable')
        async with service.lifecycle.lock(product_id):
            product = await service.detail(product_id)
            if product.get('deleted_at') or product.get('needs_restart'):
                raise DomainError('product_deleted' if product.get('deleted_at') else 'product_restart_required',
                                  '请先恢复产品。' if product.get('deleted_at') else '当前配置尚未完成从头运行。')
            return await service.launcher.launch(product)

    @router.post('/api/v1/products/{product_id}/stop')
    async def stop(product_id: str):
        await service.detail(product_id)
        if not service.launcher:
            raise DomainError('preview_unavailable', 'Local product preview is unavailable')
        async with service.lifecycle.lock(product_id):
            return await service.launcher.stop(product_id)

    return router
