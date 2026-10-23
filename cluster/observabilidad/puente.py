"""Puente alerta → issue (tarea 11).

Recibe los avisos de Alertmanager (webhook, versión 4) y los convierte en issues del repo
de la aplicación con el formato de la plantilla de peticiones, para que sigan el mismo
camino que cualquier petición: triage, decisión humana (`aprobado`) y agente.

- Una alerta que se dispara y no tiene issue abierto crea uno (etiquetas `alerta` y
  `triage:pendiente`). Si ya lo tiene, se comenta, sin duplicar.
- Una alerta resuelta se comenta en su issue; cerrarlo es cosa de una persona o del PR
  que lo arregle (`Closes #N`).
- Solo usa la biblioteca estándar: corre sobre la imagen oficial de Python, sin imagen propia.

Variables: GITHUB_REPO (owner/repo), GITHUB_TOKEN_FILE (token de grano fino con Issues:
escritura solo en ese repo), PUERTO (8080).
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

API = "https://api.github.com"
MAX_BODY = 1_000_000
ETIQUETAS = ["alerta", "triage:pendiente"]
NOMBRE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


def marcador(alerta: str) -> str:
    return f"<!-- alerta:{alerta} -->"


def limpio(texto: object, limite: int = 2000) -> str:
    """Texto de la alerta antes de publicarlo: sin HTML ni menciones."""
    s = str(texto or "").replace("<", "&lt;").replace(">", "&gt;")
    s = re.sub(r"@(?=[A-Za-z0-9_-])", "@​", s)
    return s[:limite]


def cuerpo_issue(alerta: dict) -> str:
    """Issue con los campos de la plantilla de peticiones, para que el triage lo entienda."""
    etiquetas, notas = alerta.get("labels", {}), alerta.get("annotations", {})
    nombre = etiquetas["alertname"]
    return "\n".join([
        "### ¿Qué tipo de petición es?", "", "Algo no funciona", "",
        "### ¿Qué necesitas?", "", limpio(notas.get("description") or notas.get("summary")), "",
        "### ¿Para quién es?", "", limpio(notas.get("para_quien") or "Operación de la API en staging"), "",
        "### ¿Qué se gana si se hace?", "", limpio(notas.get("impacto") or "Que la alerta deje de dispararse."), "",
        "### ¿Cómo sabremos que está hecho? (opcional)", "",
        f"- La alerta `{nombre}` deja de dispararse en staging con el mismo tráfico.", "",
        "### Urgencia", "", limpio(notas.get("urgencia") or "Este mes"), "",
        "---", "",
        "| Alerta | Severidad | Desde | Valor |", "|---|---|---|---|",
        f"| `{nombre}` | {limpio(etiquetas.get('severity', '-'), 40)} | {limpio(alerta.get('startsAt', '-'), 40)} "
        f"| {limpio(notas.get('valor', '-'), 80)} |", "",
        "<sub>Issue abierto por el puente de alertas (`poc-almagentic-gitops/cluster/observabilidad/puente.py`) "
        "a partir de una regla de Prometheus. Sigue el camino normal: triage, decisión humana y, si se aprueba, el agente.</sub>",
        marcador(nombre),
    ])


def planificar(aviso: dict, abiertos: dict[str, int]) -> list[tuple[str, dict]]:
    """Acciones para un aviso de Alertmanager. `abiertos`: alerta → número de issue abierto."""
    if str(aviso.get("version")) != "4":
        raise ValueError("versión de webhook de Alertmanager no soportada")
    acciones: list[tuple[str, dict]] = []
    vistas: set[str] = set()
    for alerta in aviso.get("alerts", []):
        nombre = alerta.get("labels", {}).get("alertname", "")
        if not NOMBRE.match(nombre) or nombre in vistas:
            continue
        vistas.add(nombre)
        resumen = limpio(alerta.get("annotations", {}).get("summary") or nombre, 120)
        numero = abiertos.get(nombre)
        if alerta.get("status") == "firing":
            if numero is None:
                acciones.append(("crear", {"title": f"[Alerta] {resumen}", "body": cuerpo_issue(alerta),
                                           "labels": ETIQUETAS}))
            else:
                acciones.append(("comentar", {"issue": numero, "body": f"🔔 La alerta `{nombre}` sigue activa. {resumen}"}))
        elif alerta.get("status") == "resolved" and numero is not None:
            acciones.append(("comentar", {"issue": numero, "body": f"✅ La alerta `{nombre}` se ha resuelto "
                                          f"({limpio(alerta.get('endsAt', ''), 40)}). El issue sigue abierto hasta que una persona o el PR del arreglo lo cierre."}))
    return acciones


class GitHub:
    def __init__(self, repo: str, token: str) -> None:
        self.repo, self.token = repo, token

    def llamar(self, metodo: str, ruta: str, datos: dict | None = None) -> object:
        peticion = urllib.request.Request(
            f"{API}{ruta}", method=metodo, data=json.dumps(datos).encode() if datos is not None else None,
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "almagentic-puente-alertas"})
        with urllib.request.urlopen(peticion, timeout=20) as r:  # noqa: S310 (URL fija de la API)
            texto = r.read()
        return json.loads(texto) if texto else None

    def abiertos(self) -> dict[str, int]:
        issues = self.llamar("GET", f"/repos/{self.repo}/issues?labels=alerta&state=open&per_page=100")
        resultado = {}
        for issue in issues if isinstance(issues, list) else []:
            m = re.search(r"<!-- alerta:([A-Za-z0-9_]+) -->", issue.get("body") or "")
            if m:
                resultado[m.group(1)] = issue["number"]
        return resultado

    def aplicar(self, acciones: list[tuple[str, dict]]) -> None:
        for tipo, datos in acciones:
            if tipo == "crear":
                issue = self.llamar("POST", f"/repos/{self.repo}/issues", datos)
                print(f"issue creado #{issue['number']}: {datos['title']}", flush=True)  # type: ignore[index]
            else:
                self.llamar("POST", f"/repos/{self.repo}/issues/{datos['issue']}/comments", {"body": datos["body"]})
                print(f"comentario en #{datos['issue']}", flush=True)


def servidor(github: GitHub, puerto: int) -> ThreadingHTTPServer:
    class Manejador(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200 if self.path == "/salud" else 404)
            self.end_headers()

        def do_POST(self) -> None:  # noqa: N802
            longitud = int(self.headers.get("Content-Length") or 0)
            if self.path != "/alertas" or not 0 < longitud <= MAX_BODY:
                self.send_response(400)
                self.end_headers()
                return
            try:
                aviso = json.loads(self.rfile.read(longitud))
                github.aplicar(planificar(aviso, github.abiertos()))
                self.send_response(200)
            except (ValueError, KeyError) as exc:
                print(f"aviso rechazado: {exc}", file=sys.stderr, flush=True)
                self.send_response(400)
            except (urllib.error.URLError, OSError) as exc:
                # 5xx: Alertmanager lo reintentará.
                print(f"error con GitHub: {exc}", file=sys.stderr, flush=True)
                self.send_response(502)
            self.end_headers()

        def log_message(self, *args: object) -> None:
            pass

    return ThreadingHTTPServer(("0.0.0.0", puerto), Manejador)  # noqa: S104 (dentro del pod)


def main() -> None:
    repo = os.environ["GITHUB_REPO"]
    with open(os.environ.get("GITHUB_TOKEN_FILE", "/run/secrets/github/token"), encoding="utf-8") as f:
        token = f.read().strip()
    puerto = int(os.environ.get("PUERTO", "8080"))
    print(f"puente de alertas → {repo}, puerto {puerto}", flush=True)
    servidor(GitHub(repo, token), puerto).serve_forever()


if __name__ == "__main__":
    main()
