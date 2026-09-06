"""
Servidor MCP para administrar proyectos y colecciones de SmallDB.

Le da a cualquier cliente MCP (Claude, etc.) herramientas para:
  - listar / crear / renombrar / borrar proyectos (apps)
  - regenerar la API key de un proyecto
  - listar / crear / borrar colecciones dentro de un proyecto

Habla por HTTP con la API administrativa de tu SmallDB (endpoints /api/admin/...),
usando una clave maestra (ADMIN_API_KEY) que NUNCA se expone al cliente MCP.

Variables de entorno requeridas:
  SMALLDB_BASE_URL   -> ej: https://juansmalldb.pythonanywhere.com
  SMALLDB_ADMIN_KEY  -> el mismo valor que pusiste como ADMIN_API_KEY en SmallDB

Variable opcional (fuertemente recomendada):
  MCP_SECRET         -> si se define, todas las peticiones a este servidor MCP
                         deben incluir el header  X-MCP-Secret: <valor>
                         (en Claude esto se configura como "custom header" al
                         añadir el conector remoto).

Ejecutar localmente:
  pip install -r requirements.txt
  python server.py
"""

import os
from typing import Any

import httpx
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from mcp.server.mcpserver import MCPServer

# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------
SMALLDB_BASE_URL = os.environ.get("SMALLDB_BASE_URL", "").rstrip("/")
SMALLDB_ADMIN_KEY = os.environ.get("SMALLDB_ADMIN_KEY", "")
MCP_SECRET = os.environ.get("MCP_SECRET", "")
PORT = int(os.environ.get("PORT", "8000"))

if not SMALLDB_BASE_URL or not SMALLDB_ADMIN_KEY:
    raise RuntimeError(
        "Faltan variables de entorno: SMALLDB_BASE_URL y SMALLDB_ADMIN_KEY son obligatorias."
    )

mcp = MCPServer(
    name="smalldb-admin",
    instructions=(
        "Herramientas para crear y administrar proyectos (apps) y colecciones "
        "en SmallDB, la base de datos JSON personal del usuario. Usa estas "
        "herramientas cuando el usuario pida crear una app/proyecto nuevo, "
        "ver sus API keys, regenerarlas, o crear/borrar colecciones."
    ),
)


def _headers() -> dict:
    return {"X-Admin-Key": SMALLDB_ADMIN_KEY, "Content-Type": "application/json"}


async def _request(method: str, path: str, json: dict | None = None) -> Any:
    url = f"{SMALLDB_BASE_URL}{path}"
    async with httpx.AsyncClient(timeout=15) as client:
        res = await client.request(method, url, headers=_headers(), json=json)
    if res.status_code >= 400:
        try:
            detail = res.json().get("error", res.text)
        except Exception:
            detail = res.text
        raise RuntimeError(f"SmallDB respondió {res.status_code}: {detail}")
    if res.status_code == 204 or not res.content:
        return {}
    return res.json()


# ---------------------------------------------------------------------------
# Herramientas MCP
# ---------------------------------------------------------------------------
@mcp.tool()
async def list_projects() -> list[dict]:
    """Lista todos los proyectos (apps) que existen en SmallDB, con su id, nombre y api_key."""
    data = await _request("GET", "/api/admin/projects")
    return data["projects"]


@mcp.tool()
async def create_project(name: str) -> dict:
    """Crea un nuevo proyecto (app) en SmallDB y devuelve su id y su api_key recién generada.

    Args:
        name: Nombre del proyecto, por ejemplo 'Cronómetro Pro Max' o 'mi-app-flutter'.
    """
    return await _request("POST", "/api/admin/projects", json={"name": name})


@mcp.tool()
async def get_project(project_id: int) -> dict:
    """Obtiene el detalle de un proyecto (nombre, api_key, fecha) junto con sus colecciones.

    Args:
        project_id: El id numérico del proyecto (lo devuelve list_projects / create_project).
    """
    return await _request("GET", f"/api/admin/projects/{project_id}")


@mcp.tool()
async def rename_project(project_id: int, new_name: str) -> dict:
    """Cambia el nombre de un proyecto existente.

    Args:
        project_id: id del proyecto a renombrar.
        new_name: nuevo nombre para el proyecto.
    """
    return await _request("PATCH", f"/api/admin/projects/{project_id}", json={"name": new_name})


@mcp.tool()
async def delete_project(project_id: int) -> dict:
    """Elimina un proyecto por completo, incluyendo todas sus colecciones y documentos.
    Esta acción no se puede deshacer, úsala solo si el usuario confirma explícitamente.

    Args:
        project_id: id del proyecto a eliminar.
    """
    return await _request("DELETE", f"/api/admin/projects/{project_id}")


@mcp.tool()
async def regenerate_api_key(project_id: int) -> dict:
    """Invalida la API key actual de un proyecto y genera una nueva.
    Cualquier app que use la key anterior dejará de poder conectarse hasta
    que se actualice con la nueva key devuelta aquí.

    Args:
        project_id: id del proyecto.
    """
    return await _request("POST", f"/api/admin/projects/{project_id}/regenerate_key")


@mcp.tool()
async def list_collections(project_id: int) -> list[dict]:
    """Lista las colecciones ('tablas') de un proyecto, con cuántos documentos tiene cada una.

    Args:
        project_id: id del proyecto.
    """
    data = await _request("GET", f"/api/admin/projects/{project_id}/collections")
    return data["collections"]


@mcp.tool()
async def create_collection(project_id: int, name: str) -> dict:
    """Crea una colección nueva y vacía dentro de un proyecto.
    (No es obligatorio: las colecciones también se crean solas al guardar el
    primer documento vía la API pública. Usa esto solo si el usuario quiere
    dejarla preparada de antemano.)

    Args:
        project_id: id del proyecto.
        name: nombre de la colección, por ejemplo 'usuarios' o 'historial'.
    """
    return await _request("POST", f"/api/admin/projects/{project_id}/collections", json={"name": name})


@mcp.tool()
async def delete_collection(project_id: int, collection_id: int) -> dict:
    """Elimina una colección y todos sus documentos. No se puede deshacer.

    Args:
        project_id: id del proyecto dueño de la colección.
        collection_id: id de la colección a eliminar.
    """
    return await _request("DELETE", f"/api/admin/projects/{project_id}/collections/{collection_id}")


# ---------------------------------------------------------------------------
# App ASGI (Starlette) + protección opcional con MCP_SECRET
# ---------------------------------------------------------------------------
app = mcp.streamable_http_app()


class SecretHeaderMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if MCP_SECRET:
            auth_header = request.headers.get("authorization", "")
            token_from_header = (
                auth_header[7:] if auth_header.lower().startswith("bearer ") else ""
            )
            token_from_query = request.query_params.get("secret", "")
            if MCP_SECRET not in (token_from_header, token_from_query):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


if MCP_SECRET:
    app.add_middleware(SecretHeaderMiddleware)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
