"""Dashboard 图片库与学习档案管理；访问权限由 AstrBot 面板统一认证。"""

import time
import asyncio
import base64

from ..vector_dashboard import vector_graph, vector_page

from ..errors import PhoneControlError
from ..saved_images import image_library
from .compat import error_response, json_response, plugin_main_module


class LibraryDashboardMixin:
    def _library_dashboard_routes(self, base):
        return (
            (f"{base}/tasks", self.dashboard_tasks, ["GET"], "后台任务进度与结果"),
            (f"{base}/tasks/delete", self.dashboard_task_delete, ["POST"], "停止并删除后台任务及证据"),
            (f"{base}/tasks/evidence", self.dashboard_task_evidence, ["GET"], "后台任务截图证据"),
            (f"{base}/mesh/ca", self.dashboard_mesh_ca, ["GET"], "下载抓包 CA"),
            (f"{base}/mesh", self.dashboard_mesh, ["GET"], "组网状态"),
            (f"{base}/mesh/action", self.dashboard_mesh_action, ["POST"], "组网接入操作"),
            (f"{base}/diary/delete", self.dashboard_diary_delete, ["POST"], "删除浏览日志"),
            (f"{base}/vectors/graph", self.dashboard_vector_graph, ["GET"], "记忆相似关系图谱"),
            (f"{base}/vectors/delete", self.dashboard_vector_delete, ["POST"], "删除记忆索引"),
            (f"{base}/vectors", self.dashboard_vectors, ["GET"], "手机记忆索引"),
            (
                f"{base}/learning/delete",
                self.dashboard_learning_delete,
                ["POST"],
                "删除手机学习记录",
            ),
            (f"{base}/images", self.dashboard_images, ["GET"], "保存的图片"),
            (
                f"{base}/images/preview",
                self.dashboard_image_preview,
                ["GET"],
                "图片预览",
            ),
            (
                f"{base}/images/update",
                self.dashboard_image_update,
                ["POST"],
                "编辑图片语义",
            ),
            (
                f"{base}/images/delete",
                self.dashboard_image_delete,
                ["POST"],
                "删除图片",
            ),
        )

    async def dashboard_tasks(self):
        query = plugin_main_module().request.query
        try:
            records = self.service.background_tasks.records
            job_id = str(query.get('job_id') or '')
            if job_id:
                if job_id not in records:
                    return error_response('未找到任务记录', status_code=404)
                return json_response(records[job_id])
            offset, limit = int(query.get('offset', 0)), int(query.get('limit', 20))
            if offset < 0 or not 1 <= limit <= 50:
                raise ValueError('分页超出范围')
            items = sorted(records.values(), key=lambda r: r['created_at'], reverse=True)
            return json_response({'tasks': items[offset:offset + limit], 'total': len(items)})
        except ValueError:
            return error_response('分页参数无效', status_code=400)
        except PhoneControlError as exc:
            return error_response(str(exc), status_code=500)

    async def dashboard_task_delete(self):
        from ..task_management import delete_background_task
        try:
            data = await self._library_request()
            return json_response(await delete_background_task(self.service, data['id']))
        except PhoneControlError as exc:
            return error_response(str(exc), status_code=400)

    async def dashboard_task_evidence(self):
        query = plugin_main_module().request.query
        try:
            row = self.service.background_tasks.evidence(str(query.get('job_id') or ''), str(query.get('id') or ''))
            if row is None:
                return error_response('未找到任务截图证据', status_code=404)
            return json_response({'data_url': 'data:' + row[0] + ';base64,' + base64.b64encode(row[1]).decode('ascii')})
        except PhoneControlError as exc:
            return error_response(str(exc), status_code=500)

    async def dashboard_vector_graph(self):
        query = plugin_main_module().request.query
        kind = str(query.get("kind", "app") or "app")
        if kind not in {"app", "chat"}:
            return error_response("请选择有效的记忆索引", status_code=400)
        index = self.app_recall_index if kind == "app" else self.semantic_index
        try:
            payload = await asyncio.to_thread(
                vector_graph, index, query=query.get("query", ""), source=query.get("source", ""),
                limit=int(query.get("limit", 80)), min_score=float(query.get("min_score", 0.45)),
            )
        except (TypeError, ValueError) as exc:
            return error_response(f"图谱参数无效：{exc}", status_code=400)
        except PhoneControlError as exc:
            return error_response(str(exc), status_code=400)
        config = self._config()
        payload["kind"] = kind
        payload["enabled"] = config.prompt.app_memory_enabled if kind == "app" else (
            config.memory.local_enabled and config.recall.enabled
        )
        return json_response(payload)

    async def dashboard_vector_delete(self):
        try:
            data = await self._library_request()
            kind = data.get("kind")
            if not isinstance(kind, str) or kind not in {"app", "chat"}:
                return error_response("请选择有效的记忆索引", status_code=400)
            index = self.app_recall_index if kind == "app" else self.semantic_index
            await asyncio.to_thread(index.delete_document, data["id"])
            return json_response({"deleted": data["id"], "source_preserved": True})
        except PhoneControlError as exc:
            return error_response(str(exc), status_code=400)
        except OSError:
            return error_response("索引删除未保存，请检查存储", status_code=500)

    async def dashboard_vectors(self):
        query = plugin_main_module().request.query
        kind = str(query.get("kind", "app") or "app")
        if kind not in {"app", "chat"}:
            return error_response("请选择 App 操控经验或聊天学习索引", status_code=400)
        index = self.app_recall_index if kind == "app" else self.semantic_index
        try:
            payload = await asyncio.to_thread(
                vector_page, index, query=query.get("query", ""),
                source=str(query.get("source", "") or ""),
                offset=int(query.get("offset", 0)), limit=int(query.get("limit", 20)),
            )
        except (TypeError, ValueError):
            return error_response("分页参数需要整数", status_code=400)
        config = self._config()
        payload["kind"] = kind
        payload["enabled"] = bool(config.prompt.app_memory_enabled) if kind == "app" else bool(
            config.memory.local_enabled and config.recall.enabled
        )
        return json_response(payload)

    async def _library_request(self):
        data = await plugin_main_module().request.json()
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("id"), str)
            or not data["id"]
        ):
            raise PhoneControlError("请提供有效的记录编号")
        return data

    async def dashboard_diary_delete(self):
        try:
            data = await self._library_request()
            await self.service.diary.delete_async(
                data["id"], commit=lambda writer: self.semantic_index.deletions.commit_source_delete("diary", data["id"], writer)
            )
            self.semantic_maintainer.request_refresh()
            return json_response({"deleted": data["id"]})
        except PhoneControlError as exc:
            return error_response(str(exc), status_code=400)
        except OSError:
            return error_response("浏览日志删除未保存，请检查存储", status_code=500)

    async def dashboard_learning_delete(self):
        try:
            data = await self._library_request()
            await asyncio.to_thread(
                self.service.learning.delete, data["id"],
                commit=lambda writer: self.semantic_index.deletions.commit_source_delete("learning", data["id"], writer),
            )
            self.semantic_maintainer.request_refresh()
            return json_response({"deleted": data["id"]})
        except PhoneControlError as exc:
            return error_response(str(exc), status_code=400)
        except OSError:
            return error_response("删除学习记录失败，请检查存储", status_code=500)

    async def dashboard_images(self):
        try:
            query = str(
                plugin_main_module().request.query.get("query", "") or ""
            ).strip()[:100]
            library = image_library(self.service)
            records = list(library.load().values())
            needle = query.lower()
            matched = [
                r
                for r in records
                if not needle
                or needle
                in str(
                    [
                        r.get(k)
                        for k in ("id", "description", "reason", "tags", "sources")
                    ]
                ).lower()
            ]
            matched.sort(key=lambda r: r.get("updated_at", 0), reverse=True)
            raw_offset = plugin_main_module().request.query.get("offset", 0, type=int)
            offset = max(0, int(raw_offset or 0))
            return json_response(
                {
                    "entries": matched[offset : offset + 30],
                    "total": len(matched),
                    "offset": offset,
                    "query": query,
                }
            )
        except PhoneControlError as exc:
            return error_response(str(exc), status_code=400)
        except OSError:
            return error_response("读取图片库失败，请检查存储", status_code=500)

    async def dashboard_image_preview(self):
        try:
            image_id = str(plugin_main_module().request.query.get("id", "") or "")
            data, suffix = await image_library(self.service).preview(image_id)
            mime = "image/" + ("jpeg" if suffix == ".jpg" else suffix[1:])
            return json_response(
                {"url": f"data:{mime};base64," + base64.b64encode(data).decode()}
            )
        except PhoneControlError as exc:
            return error_response(str(exc), status_code=400)
        except OSError:
            return error_response("读取图片失败，请检查存储", status_code=500)

    async def dashboard_image_update(self):
        try:
            data = await self._library_request()
            record = await image_library(self.service).update_semantics(
                data["id"],
                description=data.get("description"),
                reason=data.get("reason", ""),
                tags=data.get("tags", []),
                kind=data.get("kind", "image"),
            )
            return json_response(record)
        except PhoneControlError as exc:
            return error_response(str(exc), status_code=400)
        except OSError:
            return error_response("保存图片语义失败，请检查存储", status_code=500)

    async def dashboard_image_delete(self):
        try:
            data = await self._library_request()
            await image_library(self.service).delete(data["id"])
            return json_response({"deleted": data["id"]})
        except PhoneControlError as exc:
            return error_response(str(exc), status_code=400)
        except OSError:
            return error_response("删除图片失败，请检查存储", status_code=500)


    async def _network_status(self):
        from ..network_config import unified_network
        provider = unified_network(self.raw_config).get("provider", "tailscale")
        if provider == "wireguard":
            status = self._wireguard.status()
            recent_handshake = 0 <= time.time() - status["last_handshake_unix"] <= 180
            result = {
                "provider": provider,
                "ready": bool(status["ok"] and recent_handshake),
                "running": status["running"],
                "enabled": status["enabled"],
                "node_id": status["public_key"],
                "addresses": status["interface_addresses"],
                "adb_forward": status["adb_over_wg"],
                "app_port": status.get("app_port", 0),
                "error": status["error"] or status["health_reason"],
            }
        else:
            result = await self._mesh.status()
        result["components"] = self._components.status()
        return result

    async def _network_control(self, action):
        if action not in {"start", "stop", "login", "status"}:
            raise PhoneControlError("组网操作需为 start、stop、login 或 status")
        if action == "status":
            return await self._network_status()
        was_enabled = self.service.config.mesh.enabled or self.service.config.wireguard.enabled
        await self._apply_config_patch({"network": {"enabled": action != "stop"}})
        if action == "stop":
            if not was_enabled:
                await self._mesh.stop()
                await self._wireguard.stop()
        elif self._components.needs_prepare(self.service.config):
            raise PhoneControlError("运行组件尚未就绪，请保存配置并重载插件；ZeroTier SDK 需手动提供路径")
        elif self.service.config.wireguard.enabled:
            await self._wireguard.start()
        else:
            await self._mesh.start()
        return await self._network_status()

    async def dashboard_mesh(self):
        try:
            return json_response(await self._network_status())
        except (PhoneControlError, OSError, asyncio.TimeoutError):
            return error_response("组网状态读取失败，请检查组件与网络", status_code=500)

    async def dashboard_mesh_action(self):
        data = await plugin_main_module().request.json()
        if not isinstance(data, dict) or not isinstance(data.get("action"), str) or data["action"] not in {"start", "stop"}:
            return error_response("action 需为 start 或 stop", status_code=400)
        try:
            result = await self._network_control(data["action"])
            return json_response(result)
        except (PhoneControlError, OSError, asyncio.TimeoutError):
            return error_response("组网操作未完成，请检查客户端路径、节点授权与网络配置", status_code=400)


    async def dashboard_mesh_ca(self):
        from ..traffic import CA_FILE_NAME
        path = self._traffic.confdir / CA_FILE_NAME
        if not path.is_file():
            return error_response("请先启动抓包以生成 CA 证书", status_code=400)
        data = await asyncio.to_thread(path.read_bytes)
        if len(data) > 100000 or b"PRIVATE KEY" in data or b"BEGIN CERTIFICATE" not in data:
            return error_response("抓包 CA 证书格式异常", status_code=500)
        return json_response({"name": "phone-capture-ca.crt", "content_base64": base64.b64encode(data).decode("ascii")})
