"""Telemetría de los agentes (tarea 11).

Los agentes trabajan en GitHub Actions, que no puede enviar nada al clúster local. Este
exportador hace lo contrario: cada pocos minutos lee de la API de GitHub las últimas
ejecuciones de cada agente y su resultado del modelo (claude-output.json, en los artefactos)
y lo publica en /metrics para que Prometheus lo recoja y Grafana lo muestre.

Métricas (etiqueta `agente`: triage, revisor, respuesta, autonomo):
  agentes_ejecuciones{agente, resultado}        ejecuciones recientes por resultado del job
  agentes_duracion_media_segundos{agente}       duración media de las ejecuciones terminadas
  agentes_ultima_ejecucion_timestamp{agente}    inicio de la última ejecución
  agentes_turnos_total{agente}                  turnos del modelo en las ejecuciones recientes
  agentes_coste_equivalente_usd_total{agente}   coste que informa Claude Code (con suscripción no se cobra)
  agentes_errores_modelo_total{agente}          ejecuciones en las que el modelo devolvió error
  agentes_exportador_ok                         1 si la última lectura de GitHub funcionó

Solo biblioteca estándar. Variables: GITHUB_REPO, GITHUB_TOKEN_FILE (Actions: lectura),
PUERTO (9100), INTERVALO (300), EJECUCIONES (30 por agente).
"""

from __future__ import annotations

import io
import json
import os
import sys
import threading
import time
import urllib.request
import zipfile
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

API = "https://api.github.com"
AGENTES = {
    "triage": "triage.yml",
    "revisor": "agent-review.yml",
    "respuesta": "agent-respond.yml",
    "autonomo": "agent-feature.yml",
}
PREFIJOS = ("triage-", "review-", "respond-", "feature-")


def segundos(inicio: str, fin: str) -> float:
    f = "%Y-%m-%dT%H:%M:%SZ"
    return (datetime.strptime(fin, f) - datetime.strptime(inicio, f)).total_seconds()


def resumen_modelo(salida: dict) -> dict:
    return {"turnos": int(salida.get("num_turns") or 0), "coste": float(salida.get("total_cost_usd") or 0.0),
            "error": bool(salida.get("is_error"))}


def agregar(agente: str, ejecuciones: list[dict], modelo: dict[int, dict]) -> list[tuple[str, dict, float]]:
    """Muestras de Prometheus para un agente a partir de sus ejecuciones y del resumen del modelo."""
    muestras: list[tuple[str, dict, float]] = []
    # «skipped»: el evento no iba con el agente (comentario sin @claude, otra etiqueta…).
    ejecuciones = [e for e in ejecuciones if e.get("conclusion") != "skipped"]
    terminadas = [e for e in ejecuciones if e.get("status") == "completed"]
    por_resultado: dict[str, int] = {}
    for e in terminadas:
        r = e.get("conclusion") or "desconocido"
        por_resultado[r] = por_resultado.get(r, 0) + 1
    for r, n in sorted(por_resultado.items()):
        muestras.append(("agentes_ejecuciones", {"agente": agente, "resultado": r}, n))
    duraciones = [segundos(e["run_started_at"], e["updated_at"]) for e in terminadas
                  if e.get("run_started_at") and e.get("updated_at")]
    if duraciones:
        muestras.append(("agentes_duracion_media_segundos", {"agente": agente}, sum(duraciones) / len(duraciones)))
    if ejecuciones:
        ultima = max(e["run_started_at"] for e in ejecuciones if e.get("run_started_at"))
        muestras.append(("agentes_ultima_ejecucion_timestamp", {"agente": agente},
                         datetime.strptime(ultima, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()))
    datos = [modelo[e["id"]] for e in terminadas if e["id"] in modelo]
    muestras.append(("agentes_turnos_total", {"agente": agente}, sum(d["turnos"] for d in datos)))
    muestras.append(("agentes_coste_equivalente_usd_total", {"agente": agente}, round(sum(d["coste"] for d in datos), 4)))
    muestras.append(("agentes_errores_modelo_total", {"agente": agente}, sum(d["error"] for d in datos)))
    return muestras


def exposicion(muestras: list[tuple[str, dict, float]]) -> str:
    """Formato de texto de Prometheus: las muestras de cada métrica van juntas."""
    orden = list(dict.fromkeys(n for n, _, _ in muestras))
    muestras = sorted(muestras, key=lambda m: orden.index(m[0]))
    lineas, vistos = [], set()
    for nombre, etiquetas, valor in muestras:
        if nombre not in vistos:
            vistos.add(nombre)
            lineas.append(f"# TYPE {nombre} gauge")
        et = ",".join(f'{k}="{v}"' for k, v in sorted(etiquetas.items()))
        lineas.append(f"{nombre}{{{et}}} {valor}" if et else f"{nombre} {valor}")
    return "\n".join(lineas) + "\n"


class GitHub:
    def __init__(self, repo: str, token: str) -> None:
        self.repo, self.token = repo, token

    def leer(self, url: str) -> bytes:
        peticion = urllib.request.Request(url if url.startswith("http") else f"{API}{url}", headers={
            "Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "almagentic-exportador-agentes"})
        with urllib.request.urlopen(peticion, timeout=30) as r:  # noqa: S310 (API de GitHub)
            return r.read()

    def ejecuciones(self, fichero: str, n: int) -> list[dict]:
        datos = json.loads(self.leer(f"/repos/{self.repo}/actions/workflows/{fichero}/runs?per_page={n}"))
        return datos.get("workflow_runs", [])

    def salida_modelo(self, run_id: int) -> dict | None:
        artefactos = json.loads(self.leer(f"/repos/{self.repo}/actions/runs/{run_id}/artifacts")).get("artifacts", [])
        for a in artefactos:
            if a.get("expired") or not a.get("name", "").startswith(PREFIJOS):
                continue
            with zipfile.ZipFile(io.BytesIO(self.leer(a["archive_download_url"]))) as z:
                if "claude-output.json" in z.namelist():
                    try:
                        return resumen_modelo(json.loads(z.read("claude-output.json") or b"{}"))
                    except json.JSONDecodeError:
                        return None
        return None


class Estado:
    def __init__(self) -> None:
        self.texto = "agentes_exportador_ok 0\n"
        self.modelo: dict[int, dict] = {}  # caché: una ejecución terminada no cambia
        self.sin_modelo: set[int] = set()
        self.cerrojo = threading.Lock()


def actualizar(github: GitHub, estado: Estado, n: int) -> None:
    muestras: list[tuple[str, dict, float]] = []
    for agente, fichero in AGENTES.items():
        ejecuciones = github.ejecuciones(fichero, n)
        for e in ejecuciones:
            if e.get("status") == "completed" and e.get("conclusion") != "skipped" \
                    and e["id"] not in estado.modelo and e["id"] not in estado.sin_modelo:
                datos = github.salida_modelo(e["id"])
                if datos is None:
                    estado.sin_modelo.add(e["id"])
                else:
                    estado.modelo[e["id"]] = datos
        muestras += agregar(agente, ejecuciones, estado.modelo)
    muestras.append(("agentes_exportador_ok", {}, 1))
    with estado.cerrojo:
        estado.texto = exposicion(muestras)


def bucle(github: GitHub, estado: Estado, intervalo: int, n: int) -> None:
    while True:
        try:
            actualizar(github, estado, n)
            print("telemetría actualizada", flush=True)
        except Exception as exc:  # noqa: BLE001 (el exportador no debe caerse por un fallo de red)
            print(f"error leyendo GitHub: {exc}", file=sys.stderr, flush=True)
            with estado.cerrojo:
                estado.texto = estado.texto.replace("agentes_exportador_ok 1", "agentes_exportador_ok 0")
        time.sleep(intervalo)


def main() -> None:
    repo = os.environ["GITHUB_REPO"]
    with open(os.environ.get("GITHUB_TOKEN_FILE", "/run/secrets/github/token"), encoding="utf-8") as f:
        github = GitHub(repo, f.read().strip())
    estado = Estado()
    threading.Thread(target=bucle, args=(github, estado, int(os.environ.get("INTERVALO", "300")),
                                         int(os.environ.get("EJECUCIONES", "30"))), daemon=True).start()

    class Manejador(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path != "/metrics":
                self.send_response(404)
                self.end_headers()
                return
            with estado.cerrojo:
                cuerpo = estado.texto.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.end_headers()
            self.wfile.write(cuerpo)

        def log_message(self, *args: object) -> None:
            pass

    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PUERTO", "9100"))), Manejador).serve_forever()  # noqa: S104


if __name__ == "__main__":
    main()
