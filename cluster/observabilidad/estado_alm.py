"""Estado del proceso ALM (tarea E): en qué fase está cada petición, qué espera a una persona,
qué ha fallado y si staging va por detrás.

Es la única pieza que ve a la vez GitHub (los tres repos) y el clúster (las aplicaciones de
Argo CD), así que vive aquí. Cada pocos minutos lee las dos fuentes y publica métricas en
/metrics; Grafana las muestra en el panel «Proceso ALM» y Prometheus evalúa las alertas del
proceso (que no abren issues: son fallos de la plataforma, no peticiones de producto).

Métricas principales (todas gauge):
  alm_peticion_fase_segundos{numero,titulo,fase,accion}   tiempo en la fase actual
  alm_pendiente_segundos{repo,numero,titulo,accion,autor}  algo espera a una persona
  alm_workflow_fallido_timestamp{repo,workflow,paso,url}   último run en main fallido
  alm_staging_commits_detras / alm_staging_detras_desde_timestamp
  alm_promociones_abiertas / alm_promociones_sustituidas
  alm_argocd_app{app,sync,health}
  alm_core_v1_al_dia, alm_token_caduca_timestamp
  alm_dora_*  (aproximaciones a 30 días: despliegues, tiempo de cambio, recuperación, fallos)
  alm_colector_ok

Solo biblioteca estándar. Variables: REPO_APP, REPO_CORE, REPO_GITOPS, RESPONSABLE (login de la
persona que aprueba), GITHUB_TOKEN_FILE (lectura), PUERTO (9200), INTERVALO (300).
"""

from __future__ import annotations

import json
import os
import re
import ssl
import statistics
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

API = "https://api.github.com"
K8S = "https://kubernetes.default.svc"
SA = "/var/run/secrets/kubernetes.io/serviceaccount"
DIA = 86400
FALLO = {"failure", "startup_failure", "timed_out"}
PROMOCION = re.compile(r"^deploy\(staging\): .*\(([0-9a-f]{7,40})\)\s*$")
CIERRA = re.compile(r"(?i)\b(?:closes|fixes|resolves)\s+#(\d+)")


def ts(fecha: str | None) -> float | None:
    if not fecha:
        return None
    return datetime.fromisoformat(fecha.replace("Z", "+00:00")).timestamp()


def etiquetas(issue: dict) -> set[str]:
    return {e["name"] for e in issue.get("labels", [])}


# ---------- fase de cada petición ----------


def prs_de(numero: int, prs: list[dict], prefijo: str) -> list[dict]:
    return [p for p in prs if p["head"]["ref"].startswith(f"{prefijo}/{numero}-")]


def fase_peticion(issue: dict, prs_app: list[dict], en_staging) -> tuple[str, str | None, str]:
    """Devuelve (fase, desde, acción humana pendiente o "")."""
    n = issue["number"]
    l = etiquetas(issue)
    specs, feats = prs_de(n, prs_app, "spec"), prs_de(n, prs_app, "feat")
    feat_merged = [p for p in feats if p.get("merged_at")]
    if feat_merged:
        p = max(feat_merged, key=lambda x: x["merged_at"])
        if en_staging(p["merge_commit_sha"]):
            return "en staging", p["merged_at"], ""
        return "pendiente de despliegue", p["merged_at"], "aprobar la promoción en gitops"
    abiertos = [p for p in feats if p["state"] == "open"]
    if abiertos:
        return "implementación en revisión", abiertos[0]["created_at"], "revisar y aprobar el PR"
    spec_merged = [p for p in specs if p.get("merged_at")]
    if issue["state"] == "closed":
        return "cerrada sin cambios", issue.get("closed_at"), ""
    if spec_merged:
        return "implementación (agente)", max(p["merged_at"] for p in spec_merged), ""
    abiertos = [p for p in specs if p["state"] == "open"]
    if abiertos:
        return "spec en revisión", abiertos[0]["created_at"], "revisar y aprobar la spec"
    if "aprobado" in l:
        return "spec (agente)", issue.get("updated_at"), ""
    if l & {"triage:propuesto", "triage:sospechoso", "triage:manual"}:
        return "esperando decisión", issue.get("updated_at"), "decidir y poner aprobado (o cerrar)"
    return "triage", issue.get("created_at"), ""


# ---------- qué espera a una persona ----------


def pendientes_prs(repo: str, prs: list[dict], responsable: str) -> list[dict]:
    """PRs abiertos: los de otros (agente, Dependabot, promotor) esperan revisión; los propios, el merge."""
    out = []
    for p in prs:
        if p["state"] != "open" or p.get("draft"):
            continue
        autor = p["user"]["login"]
        estado = p.get("mergeable_state", "")
        if p.get("checks") == "rojo":
            accion = "checks en rojo"
        elif autor == responsable:
            accion = "fusionar (tuyo)"
        elif p.get("aprobado"):
            accion = "fusionar"
        else:
            accion = "revisar y aprobar"
        if p.get("sustituida"):
            accion = "cerrar (promoción sustituida)"
        out.append({"repo": repo, "numero": p["number"], "titulo": p["title"], "accion": accion,
                    "autor": autor, "desde": p["created_at"], "estado": estado})
    return out


def pendientes_issues(issues: list[dict]) -> list[dict]:
    out = []
    for i in issues:
        if i["state"] != "open" or "pull_request" in i:
            continue
        l = etiquetas(i)
        if l & {"triage:propuesto", "triage:sospechoso", "triage:manual"} and "aprobado" not in l:
            out.append({"repo": "app", "numero": i["number"], "titulo": i["title"],
                        "accion": "decidir la petición", "autor": i["user"]["login"], "desde": i["updated_at"]})
    return out


# ---------- promociones y DORA ----------


def promociones(prs_gitops: list[dict]) -> dict:
    promos = [p for p in prs_gitops if PROMOCION.match(p["title"])]
    abiertas = sorted((p for p in promos if p["state"] == "open"), key=lambda p: p["created_at"])
    for p in abiertas[:-1]:
        p["sustituida"] = True
    fusionadas = sorted((p for p in promos if p.get("merged_at")), key=lambda p: p["merged_at"])
    ultima = fusionadas[-1] if fusionadas else None
    return {
        "abiertas": len(abiertas),
        "sustituidas": max(len(abiertas) - 1, 0),
        "staging_commit": PROMOCION.match(ultima["title"]).group(1) if ultima else None,
        "fusionadas": fusionadas,
    }


def dora(fusionadas: list[dict], fecha_commit, alertas: list[dict], ahora: float) -> dict:
    """Aproximaciones a 30 días a partir de las promociones fusionadas y de los issues de alerta."""
    desde = ahora - 30 * DIA
    despl = [p for p in fusionadas if ts(p["merged_at"]) >= desde]
    lead = []
    for p in despl:
        c = fecha_commit(PROMOCION.match(p["title"]).group(1))
        if c:
            lead.append(ts(p["merged_at"]) - c)
    recientes = [a for a in alertas if ts(a["created_at"]) >= desde]
    cerradas = [ts(a["closed_at"]) - ts(a["created_at"]) for a in recientes if a.get("closed_at")]
    tras_despliegue = [a for a in recientes
                       if any(0 <= ts(a["created_at"]) - ts(p["merged_at"]) <= DIA for p in despl)]
    return {
        "despliegues_30d": len(despl),
        "tiempo_cambio_mediana_segundos": statistics.median(lead) if lead else 0,
        "recuperacion_mediana_segundos": statistics.median(cerradas) if cerradas else 0,
        "tasa_fallo": round(len(tras_despliegue) / len(despl), 3) if despl else 0,
    }


# ---------- workflows ----------


def workflows_fallidos(runs: list[dict]) -> list[dict]:
    """Último run de cada workflow en main; se queda con los que fallaron."""
    ultimo: dict[str, dict] = {}
    for r in runs:
        if r.get("status") != "completed" or r.get("conclusion") in (None, "skipped", "cancelled"):
            continue
        clave = r.get("path") or r.get("name")
        if clave not in ultimo or r["created_at"] > ultimo[clave]["created_at"]:
            ultimo[clave] = r
    return [r for r in ultimo.values() if r["conclusion"] in FALLO]


# ---------- exposición ----------


def escapar(v: object) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")[:120]


def exposicion(muestras: list[tuple[str, dict, float]]) -> str:
    orden = list(dict.fromkeys(n for n, _, _ in muestras))
    lineas, vistos = [], set()
    for nombre, et, valor in sorted(muestras, key=lambda m: orden.index(m[0])):
        if nombre not in vistos:
            vistos.add(nombre)
            lineas.append(f"# TYPE {nombre} gauge")
        txt = ",".join(f'{k}="{escapar(v)}"' for k, v in sorted(et.items()))
        lineas.append(f"{nombre}{{{txt}}} {valor}" if txt else f"{nombre} {valor}")
    return "\n".join(lineas) + "\n"


# ---------- fuentes ----------


class GitHub:
    def __init__(self, token: str) -> None:
        self.token = token
        self.caducidad: float | None = None
        self.cache: dict[str, object] = {}

    def get(self, ruta: str) -> object:
        url = ruta if ruta.startswith("http") else f"{API}{ruta}"
        peticion = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "almagentic-estado-alm"})
        with urllib.request.urlopen(peticion, timeout=30) as r:  # noqa: S310 (API de GitHub)
            caduca = r.headers.get("github-authentication-token-expiration")
            if caduca:
                try:
                    self.caducidad = datetime.strptime(caduca, "%Y-%m-%d %H:%M:%S %Z").replace(
                        tzinfo=timezone.utc).timestamp()
                except ValueError:
                    pass
            return json.loads(r.read())

    def fijo(self, ruta: str) -> object:
        """Para datos que no cambian (un commit, una comparación entre dos SHA)."""
        if ruta not in self.cache:
            self.cache[ruta] = self.get(ruta)
        return self.cache[ruta]


def argocd_apps() -> list[dict]:
    """Aplicaciones de Argo CD por la API de Kubernetes (ClusterRole de solo lectura)."""
    with open(f"{SA}/token", encoding="utf-8") as f:
        token = f.read().strip()
    ctx = ssl.create_default_context(cafile=f"{SA}/ca.crt")
    peticion = urllib.request.Request(f"{K8S}/apis/argoproj.io/v1alpha1/applications",
                                      headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(peticion, timeout=15, context=ctx) as r:  # noqa: S310 (API del clúster)
        datos = json.loads(r.read())
    return [{"app": a["metadata"]["name"],
             "sync": a.get("status", {}).get("sync", {}).get("status", "Unknown"),
             "health": a.get("status", {}).get("health", {}).get("status", "Unknown")}
            for a in datos.get("items", [])]


# ---------- ciclo ----------


def recoger(gh: GitHub, repos: dict[str, str], responsable: str, ahora: float) -> list[tuple[str, dict, float]]:
    app, core, gitops = repos["app"], repos["core"], repos["gitops"]
    m: list[tuple[str, dict, float]] = []

    prs = {k: gh.get(f"/repos/{v}/pulls?state=all&per_page=60&sort=updated&direction=desc")
           for k, v in repos.items()}
    promo = promociones(prs["gitops"])

    # PRs abiertos: estado de los checks y si ya tienen aprobación.
    for k, v in repos.items():
        for p in prs[k]:
            if p["state"] != "open":
                continue
            checks = gh.get(f"/repos/{v}/commits/{p['head']['sha']}/check-runs?per_page=100")["check_runs"]
            p["checks"] = "rojo" if any(c.get("conclusion") in FALLO for c in checks) else "ok"
            reviews = gh.get(f"/repos/{v}/pulls/{p['number']}/reviews")
            p["aprobado"] = any(r.get("state") == "APPROVED" for r in reviews)

    # Staging frente a main de app.
    staging = promo["staging_commit"]
    main_sha = gh.get(f"/repos/{app}/commits/main")["sha"]
    detras, detras_desde = 0, None
    if staging:
        comp = gh.get(f"/repos/{app}/compare/{staging}...{main_sha}")
        detras = comp.get("ahead_by", 0)
        if detras:
            detras_desde = min(ts(c["commit"]["committer"]["date"]) for c in comp.get("commits", []))
    m.append(("alm_staging_commits_detras", {}, detras))
    if detras_desde:
        m.append(("alm_staging_detras_desde_timestamp", {}, detras_desde))
    m.append(("alm_staging_info", {"commit": (staging or "")[:7], "main": main_sha[:7]}, 1))

    def en_staging(sha: str) -> bool:
        if not staging or not sha:
            return False
        estado = gh.fijo(f"/repos/{app}/compare/{sha}...{staging}").get("status")
        return estado in ("ahead", "identical")

    # Peticiones: las 30 más recientes.
    issues = [i for i in gh.get(f"/repos/{app}/issues?state=all&per_page=30&sort=created&direction=desc")
              if "pull_request" not in i]
    for i in issues:
        fase, desde, accion = fase_peticion(i, prs["app"], en_staging)
        if fase == "cerrada sin cambios" and ahora - (ts(desde) or ahora) > 7 * DIA:
            continue
        m.append(("alm_peticion_fase_segundos",
                  {"numero": i["number"], "titulo": i["title"], "fase": fase, "accion": accion},
                  round(ahora - (ts(desde) or ahora))))

    # Qué espera a una persona.
    pend = pendientes_issues(issues)
    for k in repos:
        pend += pendientes_prs(k, prs[k], responsable)
    for p in pend:
        m.append(("alm_pendiente_segundos", {k: p[k] for k in ("repo", "numero", "titulo", "accion", "autor")},
                  round(ahora - ts(p["desde"]))))
    m.append(("alm_pendientes_total", {}, len(pend)))

    # Workflows en rojo en main.
    for k, v in repos.items():
        runs = gh.get(f"/repos/{v}/actions/runs?branch=main&per_page=50")["workflow_runs"]
        for r in workflows_fallidos(runs):
            paso = ""
            try:
                trabajos = gh.get(f"/repos/{v}/actions/runs/{r['id']}/jobs")["jobs"]
                for t in trabajos:
                    fallido = [s["name"] for s in t.get("steps", []) if s.get("conclusion") == "failure"]
                    if fallido:
                        paso = f"{t['name']} › {fallido[0]}"
                        break
                if not trabajos:
                    paso = "no llegó a arrancar"
            except Exception:  # noqa: BLE001
                pass
            m.append(("alm_workflow_fallido_timestamp",
                      {"repo": k, "workflow": r.get("name", ""), "paso": paso, "url": r.get("html_url", "")},
                      ts(r["created_at"])))

    # Promociones, Dependabot, core v1.
    m.append(("alm_promociones_abiertas", {}, promo["abiertas"]))
    m.append(("alm_promociones_sustituidas", {}, promo["sustituidas"]))
    for k in repos:
        n = sum(1 for p in prs[k] if p["state"] == "open" and p["user"]["login"] == "dependabot[bot]")
        m.append(("alm_dependabot_abiertos", {"repo": k}, n))
    v1 = gh.get(f"/repos/{core}/git/ref/tags/v1")["object"]["sha"]
    ultima = gh.get(f"/repos/{core}/releases/latest")["tag_name"]
    ultima_sha = gh.get(f"/repos/{core}/git/ref/tags/{urllib.parse.quote(ultima)}")["object"]["sha"]
    m.append(("alm_core_v1_al_dia", {"ultima": ultima}, int(v1 == ultima_sha)))

    # DORA (aproximado).
    alertas = gh.get(f"/repos/{app}/issues?state=all&labels=alerta&per_page=50")

    def fecha_commit(sha: str) -> float | None:
        try:
            return ts(gh.fijo(f"/repos/{app}/commits/{sha}")["commit"]["committer"]["date"])
        except Exception:  # noqa: BLE001
            return None

    for k, v in dora(promo["fusionadas"], fecha_commit, alertas, ahora).items():
        m.append((f"alm_dora_{k}", {}, v))

    if gh.caducidad:
        m.append(("alm_token_caduca_timestamp", {}, gh.caducidad))
    return m


class Estado:
    def __init__(self) -> None:
        self.texto = "alm_colector_ok 0\n"
        self.github: list[tuple[str, dict, float]] = []
        self.cerrojo = threading.Lock()


def ciclo(gh: GitHub, repos: dict[str, str], responsable: str, estado: Estado, argocd=argocd_apps) -> int:
    """Una lectura completa. Si una fuente falla, se publican sus últimos datos buenos y colector_ok 0."""
    ok = 1
    try:
        estado.github = recoger(gh, repos, responsable, time.time())
    except Exception as exc:  # noqa: BLE001 (el colector no debe caerse por un fallo de red)
        print(f"error leyendo GitHub: {exc}", file=sys.stderr, flush=True)
        ok = 0
    muestras = list(estado.github)
    try:
        muestras += [("alm_argocd_app", a, 1) for a in argocd()]
    except Exception as exc:  # noqa: BLE001
        print(f"error leyendo Argo CD: {exc}", file=sys.stderr, flush=True)
        ok = 0
    muestras.append(("alm_colector_ok", {}, ok))
    with estado.cerrojo:
        estado.texto = exposicion(muestras)
    return ok


def bucle(gh: GitHub, repos: dict[str, str], responsable: str, estado: Estado, intervalo: int) -> None:
    while True:
        ok = ciclo(gh, repos, responsable, estado)
        print("estado actualizado" if ok else "estado con errores", flush=True)
        time.sleep(intervalo)


def main() -> None:
    repos = {"app": os.environ["REPO_APP"], "core": os.environ["REPO_CORE"], "gitops": os.environ["REPO_GITOPS"]}
    with open(os.environ.get("GITHUB_TOKEN_FILE", "/run/secrets/github/token"), encoding="utf-8") as f:
        gh = GitHub(f.read().strip())
    estado = Estado()
    threading.Thread(target=bucle, args=(gh, repos, os.environ["RESPONSABLE"], estado,
                                         int(os.environ.get("INTERVALO", "300"))), daemon=True).start()

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

    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PUERTO", "9200"))), Manejador).serve_forever()  # noqa: S104


if __name__ == "__main__":
    main()
