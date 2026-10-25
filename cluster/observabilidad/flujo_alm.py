"""Flujo ALM (tarea E): aplicación web que muestra, en vivo, por dónde va cada petición.

Lee la API de GitHub de los tres repos y sirve:
  /             el panel (flujo.html)
  /api/flujo    el estado de cada petición por sus 8 etapas (petición, triage, decisión, spec,
                código, imagen, promoción y staging): estado, quién, cuándo, checks y qué falta
  /api/salud    si la última lectura funcionó, cuándo y cuántas consultas le quedan al token

Un hilo recalcula el estado cada INTERVALO segundos. Para no agotar el límite de la API usa
peticiones condicionales (ETag: una respuesta 304 no cuenta) y guarda para siempre lo que no
cambia (comparaciones entre dos SHA). Solo lee: no escribe nada en GitHub.

Solo biblioteca estándar. Variables: REPO_APP, REPO_CORE, REPO_GITOPS, RESPONSABLE,
GITHUB_TOKEN_FILE (lectura), PUERTO (8088), INTERVALO (60), HTML (ruta de flujo.html).

Estados: hecho, en_curso (lo hace una máquina), te_toca (espera a una persona), fallo,
aviso (hecho, con algo que mirar), pendiente (aún no ha llegado) y omitido.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

API = "https://api.github.com"
AGENTE = "almagentic-agent[bot]"
REVISOR = "github-actions[bot]"
DEPENDABOT = "dependabot[bot]"
PROMOCION = re.compile(r"^deploy\(staging\): .*\(([0-9a-f]{7,40})\)\s*$")
VEREDICTO = re.compile(r"Revisión del agente:\s*(.+)")
CABECERA = re.compile(r"^###\s*🤖\s*(.+)$", re.M)
FALLO = {"failure", "startup_failure", "timed_out", "action_required"}
INMUTABLE = re.compile(r"/compare/[0-9a-f]{7,40}\.\.\.[0-9a-f]{7,40}$")

Api = Callable[[str], object]


class GitHub:
    """Cliente de solo lectura con caché por ETag: lo que no ha cambiado no gasta cuota."""

    def __init__(self, token: str) -> None:
        self.token = token
        self.etags: dict[str, tuple[str, object]] = {}
        self.fijos: dict[str, object] = {}
        self.restantes: int | None = None
        self.llamadas = 0
        self.cerrojo = threading.Lock()

    def __call__(self, ruta: str) -> object:
        if ruta in self.fijos:
            return self.fijos[ruta]
        cab = {"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "almagentic-flujo-alm"}
        previo = self.etags.get(ruta)
        if previo:
            cab["If-None-Match"] = previo[0]
        peticion = urllib.request.Request(f"{API}/{ruta}", headers=cab)
        try:
            with urllib.request.urlopen(peticion, timeout=30) as r:  # noqa: S310 (API de GitHub)
                datos = json.loads(r.read())
                self._cuota(r.headers)
                etag = r.headers.get("ETag")
        except urllib.error.HTTPError as e:
            self._cuota(e.headers)
            if e.code == 304 and previo:
                return previo[1]
            raise
        with self.cerrojo:
            self.llamadas += 1
            if INMUTABLE.search(ruta.split("?")[0]):
                self.fijos[ruta] = datos
            elif etag:
                self.etags[ruta] = (etag, datos)
        return datos

    def _cuota(self, cabeceras) -> None:
        valor = cabeceras.get("X-RateLimit-Remaining") if cabeceras else None
        if valor is not None:
            self.restantes = int(valor)


def etapa(id_: str, nombre: str, estado: str, titulo: str = "", **kw) -> dict:
    return {"id": id_, "nombre": nombre, "estado": estado, "titulo": titulo,
            "detalle": kw.get("detalle", []), "accion": kw.get("accion", ""), "quien": kw.get("quien", ""),
            "cuando": kw.get("cuando", ""), "enlaces": kw.get("enlaces", []), "checks": kw.get("checks", [])}


class Fuente:
    """Lecturas con caché: cada ruta se pide una vez por ejecución."""

    def __init__(self, api: Api, app: str, core: str, gitops: str) -> None:
        self.api, self.app, self.core, self.gitops = api, app, core, gitops
        self._c: dict[str, object] = {}

    def get(self, ruta: str) -> object:
        if ruta not in self._c:
            self._c[ruta] = self.api(ruta)
        return self._c[ruta]

    def url(self, repo: str, tipo: str, n: int) -> str:
        return f"https://github.com/{repo}/{tipo}/{n}"

    def checks(self, repo: str, sha: str) -> list[dict]:
        runs = self.get(f"repos/{repo}/commits/{sha}/check-runs?per_page=100")["check_runs"]
        vistos: dict[str, dict] = {}
        for r in runs:
            if r.get("conclusion") == "skipped":
                continue
            vistos[r["name"]] = {"nombre": r["name"], "estado": r.get("conclusion") or r.get("status"),
                                 "url": r.get("html_url", "")}
        return sorted(vistos.values(), key=lambda c: c["nombre"])

    def comentarios(self, repo: str, n: int) -> list[dict]:
        return self.get(f"repos/{repo}/issues/{n}/comments?per_page=100")

    def revisiones(self, repo: str, n: int) -> list[dict]:
        return self.get(f"repos/{repo}/pulls/{n}/reviews?per_page=100")

    def incluido(self, sha: str, en: str) -> bool:
        """¿El commit `sha` de app está dentro de `en`?"""
        if not sha or not en:
            return False
        return self.get(f"repos/{self.app}/compare/{sha}...{en}").get("status") in ("ahead", "identical")


def ultimo_aviso_bot(comentarios: list[dict], autor: str) -> tuple[str, str, str] | None:
    """Último comentario de un bot: (cabecera, fecha, url)."""
    for c in reversed(comentarios):
        if c["user"]["login"] == autor:
            m = CABECERA.search(c["body"] or "")
            return (m.group(1).strip() if m else c["body"][:80], c["created_at"], c["html_url"])
    return None


def resumen_revision(revisiones: list[dict]) -> tuple[str | None, list[str]]:
    agente = [r for r in revisiones if r["user"]["login"] == REVISOR and VEREDICTO.search(r.get("body") or "")]
    if not agente:
        return None, []
    cuerpo = agente[-1]["body"]
    hallazgos = re.findall(r"^\|\s*(alta|media|baja)\s*\|", cuerpo, re.M)
    cuenta = [f"{hallazgos.count(s)} {s}" for s in ("alta", "media", "baja") if hallazgos.count(s)]
    return VEREDICTO.search(cuerpo).group(1).strip(), cuenta


def aprobaciones(revisiones: list[dict]) -> list[str]:
    ultimo: dict[str, str] = {}
    for r in revisiones:
        if r["user"]["login"] != REVISOR and r["state"] in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            ultimo[r["user"]["login"]] = r["state"]
    return [u for u, s in ultimo.items() if s == "APPROVED"]


def etapa_pr(f: Fuente, id_: str, nombre: str, pr: dict, responsable: str) -> dict:
    """Spec o código: estado del PR, checks, revisión automática, @claude y qué falta."""
    n = pr["number"]
    checks = f.checks(f.app, pr["head"]["sha"])
    revs = f.revisiones(f.app, n)
    veredicto, hallazgos = resumen_revision(revs)
    aprob = aprobaciones(revs)
    coms = f.comentarios(f.app, n)
    rondas = [c for c in coms if c["user"]["login"] != AGENTE and "@claude" in (c["body"] or "")]
    respuesta = ultimo_aviso_bot(coms, AGENTE)
    detalle = []
    if veredicto:
        detalle.append(f"Revisor automático: {veredicto}" + (f" ({', '.join(hallazgos)})" if hallazgos else ""))
    if rondas:
        detalle.append(f"{len(rondas)} petición(es) @claude; última respuesta: {respuesta[0] if respuesta else '—'}")
    if pr.get("merged_at"):
        quien = (pr.get("merged_by") or {}).get("login", "")
        propio = pr["user"]["login"] == responsable
        if aprob:
            detalle.append("Aprobado por " + ", ".join(f"@{u}" for u in aprob))
        elif propio:
            detalle.append("PR propio: GitHub no deja aprobarlo, se fusionó con bypass (queda auditado)")
        else:
            detalle.append(f"Fusionado sin aprobación vigente (bypass de @{quien}): un commit posterior la descartó o no se aprobó")
        estado = "hecho" if aprob or propio else "aviso"
        return etapa(id_, nombre, estado, f"#{n} fusionado", detalle=detalle, quien=f"@{pr['user']['login']}",
                     cuando=pr["merged_at"], enlaces=[{"texto": f"PR #{n}", "url": pr["html_url"]}], checks=checks)
    if pr["state"] == "closed":
        return etapa(id_, nombre, "omitido", f"#{n} cerrado sin fusionar", detalle=detalle,
                     enlaces=[{"texto": f"PR #{n}", "url": pr["html_url"]}], checks=checks)
    rojos = [c["nombre"] for c in checks if c["estado"] in FALLO]
    corriendo = [c["nombre"] for c in checks if c["estado"] in ("queued", "in_progress")]
    if respuesta and respuesta[0].startswith(("⚠️", "⛔")) and (not rondas or respuesta[1] > rondas[-1]["created_at"]):
        estado, accion = "fallo", f"El agente no completó el último @claude: {respuesta[0]}. Mira la ejecución y vuelve a pedirlo."
    elif rojos:
        estado, accion = "fallo", f"Checks en rojo ({', '.join(rojos)}). Pide el arreglo con @claude en el PR."
    elif corriendo:
        estado, accion = "en_curso", f"Esperando a los checks ({', '.join(corriendo)})."
    elif aprob:
        estado, accion = "te_toca", "Aprobado: falta fusionar."
    else:
        estado, accion = "te_toca", "Revisa el PR (y los hallazgos del revisor) y apruébalo, o pide cambios con @claude."
    return etapa(id_, nombre, estado, f"#{n} abierto", detalle=detalle, accion=accion, quien=f"@{pr['user']['login']}",
                 cuando=pr["created_at"], enlaces=[{"texto": f"PR #{n}", "url": pr["html_url"]}], checks=checks)


def etapas_entrega(f: Fuente, merge_sha: str | None, promos: list[dict], staging: str | None) -> list[dict]:
    """Imagen, promoción y staging de un commit fusionado en main de app."""
    if not merge_sha:
        return [etapa("imagen", "Imagen", "pendiente"), etapa("promocion", "Promoción", "pendiente"),
                etapa("staging", "Staging", "pendiente")]
    runs = f.get(f"repos/{f.app}/actions/runs?head_sha={merge_sha}&per_page=20")["workflow_runs"]
    sc = [r for r in runs if r["name"] == "supply-chain"]
    if not sc:
        img = etapa("imagen", "Imagen", "omitido", "sin cadena de suministro (anterior a la tarea 9)")
    else:
        r = max(sc, key=lambda x: x["created_at"])
        if r["status"] != "completed":
            img = etapa("imagen", "Imagen", "en_curso", "construyendo y firmando", enlaces=[{"texto": "supply-chain", "url": r["html_url"]}])
        elif r["conclusion"] == "success":
            img = etapa("imagen", "Imagen", "hecho", "firmada y atestiguada", cuando=r["updated_at"],
                        enlaces=[{"texto": "supply-chain", "url": r["html_url"]}])
        else:
            img = etapa("imagen", "Imagen", "fallo", f"supply-chain: {r['conclusion']}",
                        accion="Revisa la ejecución; si el fallo es del pipeline, el arreglo va en core.",
                        enlaces=[{"texto": "supply-chain", "url": r["html_url"]}])
    # Promoción: la primera que incluye el commit (las abiertas cuentan si llevan exactamente este o uno posterior).
    prom = None
    for p in sorted(promos, key=lambda x: x["created_at"]):
        c = PROMOCION.match(p["title"]).group(1)
        if p["state"] == "closed" and not p.get("merged_at"):
            continue
        if f.incluido(merge_sha, c):
            prom = p
            break
    if not prom:
        pr_e = etapa("promocion", "Promoción", "en_curso" if img["estado"] in ("en_curso", "hecho") else "pendiente",
                     "el promotor aún no ha abierto el PR en gitops")
    elif prom.get("merged_at"):
        pr_e = etapa("promocion", "Promoción", "hecho", f"gitops#{prom['number']} fusionado", cuando=prom["merged_at"],
                     enlaces=[{"texto": f"gitops#{prom['number']}", "url": prom["html_url"]}],
                     checks=f.checks(f.gitops, prom["head"]["sha"]))
    else:
        chk = f.checks(f.gitops, prom["head"]["sha"])
        rojos = [c["nombre"] for c in chk if c["estado"] in FALLO]
        pr_e = etapa("promocion", "Promoción", "fallo" if rojos else "te_toca", f"gitops#{prom['number']} abierto",
                     accion=(f"verify en rojo ({', '.join(rojos)}): la imagen no pasa la verificación." if rojos
                             else f"Aprueba y fusiona gitops#{prom['number']} para desplegar."),
                     cuando=prom["created_at"], checks=chk,
                     enlaces=[{"texto": f"gitops#{prom['number']}", "url": prom["html_url"]}])
    if img["estado"] == "fallo" and pr_e["estado"] == "hecho":
        img["estado"], img["accion"] = "aviso", ""
        img["detalle"] = ["Esta ejecución falló; la imagen que se desplegó salió de una ejecución posterior."]
    if staging and f.incluido(merge_sha, staging):
        stg = etapa("staging", "Staging", "hecho", f"desplegado ({staging[:7]})")
    else:
        stg = etapa("staging", "Staging", "pendiente", "todavía no")
    return [img, pr_e, stg]


def flujo_peticion(f: Fuente, issue: dict, prs: list[dict], promos: list[dict], staging: str | None,
                   core_prs: list[dict], responsable: str) -> dict:
    n = issue["number"]
    etiquetas = {e["name"] for e in issue.get("labels", [])}
    alerta = "<!-- alerta:" in (issue.get("body") or "")
    coms = f.comentarios(f.app, n)
    et = [etapa("peticion", "Petición", "hecho", "alerta de Prometheus" if alerta else f"@{issue['user']['login']}",
                cuando=issue["created_at"], enlaces=[{"texto": f"Issue #{n}", "url": issue["html_url"]}],
                detalle=[issue["title"]])]
    # Triage.
    triage = [c for c in coms if c["user"]["login"] == REVISOR and "Propuesta de triage" in (c["body"] or "")]
    clas = sorted(e.split(":", 1)[1] for e in etiquetas if e.split(":")[0] in ("tipo", "prioridad", "tamano"))
    preguntas = 0
    if triage:
        bloque = triage[-1]["body"].split("**Preguntas abiertas**")
        preguntas = len(re.findall(r"^- ", bloque[1].split("**", 1)[0], re.M)) if len(bloque) > 1 else 0
    if "triage:sospechoso" in etiquetas:
        tri = etapa("triage", "Triage", "aviso", "posible inyección", accion="Revisa el texto con cuidado antes de aprobar.")
    elif "triage:manual" in etiquetas:
        tri = etapa("triage", "Triage", "te_toca", "triage manual", accion="El agente no pudo clasificarla: hazlo tú.")
    elif triage:
        tri = etapa("triage", "Triage", "hecho", " · ".join(clas), cuando=triage[-1]["created_at"],
                    detalle=[f"{preguntas} preguntas abiertas"] if preguntas else [],
                    enlaces=[{"texto": "propuesta", "url": triage[-1]["html_url"]}])
    else:
        tri = etapa("triage", "Triage", "en_curso", "el agente de triage está trabajando")
    et.append(tri)
    # Decisión humana.
    eventos = f.get(f"repos/{f.app}/issues/{n}/events?per_page=100")
    aprob = [e for e in eventos if e.get("event") == "labeled" and e["label"]["name"] == "aprobado"]
    respuestas = [c for c in coms if c["user"]["login"] not in (REVISOR, AGENTE) and "@claude" not in (c["body"] or "")]
    if aprob:
        et.append(etapa("decision", "Decisión", "hecho", f"aprobado por @{aprob[0]['actor']['login']}",
                        cuando=aprob[0]["created_at"],
                        detalle=[f"{len(respuestas)} comentario(s) con respuestas o decisiones"] if respuestas else []))
    elif issue["state"] == "closed":
        et.append(etapa("decision", "Decisión", "omitido", "cerrada sin aprobar", cuando=issue.get("closed_at", "")))
    elif tri["estado"] in ("hecho", "aviso", "te_toca"):
        falta = (f"Responde a las {preguntas} preguntas abiertas en un comentario y pon la etiqueta aprobado."
                 if preguntas and not respuestas else "Pon la etiqueta aprobado (o cierra el issue con el motivo).")
        et.append(etapa("decision", "Decisión", "te_toca", "esperando tu decisión", accion=falta))
    else:
        et.append(etapa("decision", "Decisión", "pendiente"))
    # Spec y código.
    specs = [p for p in prs if p["head"]["ref"].startswith(f"spec/{n}-")]
    feats = [p for p in prs if p["head"]["ref"].startswith(f"feat/{n}-")]
    agente = ultimo_aviso_bot(coms, AGENTE)
    spec = max(specs, key=lambda p: p["created_at"]) if specs else None
    feat = max(feats, key=lambda p: p["created_at"]) if feats else None
    if spec:
        et.append(etapa_pr(f, "spec", "Spec", spec, responsable))
    elif aprob and agente and agente[0].startswith(("⚠️", "⛔")):
        et.append(etapa("spec", "Spec", "fallo", "el agente no publicó la spec", detalle=[agente[0]],
                        accion="Lee el motivo; si es un fallo de la plataforma, el arreglo va en core. Luego quita y pon aprobado.",
                        enlaces=[{"texto": "aviso del agente", "url": agente[2]}]))
    elif aprob:
        et.append(etapa("spec", "Spec", "en_curso", "el agente está escribiendo la spec"))
    else:
        et.append(etapa("spec", "Spec", "pendiente"))
    if feat:
        et.append(etapa_pr(f, "codigo", "Código", feat, responsable))
    elif spec and spec.get("merged_at"):
        et.append(etapa("codigo", "Código", "en_curso", "el agente está implementando"))
    else:
        et.append(etapa("codigo", "Código", "pendiente"))
    merge = feat["merge_commit_sha"] if feat and feat.get("merged_at") else None
    et += etapas_entrega(f, merge, promos, staging)
    # Arreglos en core que cita esta petición o sus PRs.
    refs = {f"app#{n}"} | {f"app#{p['number']}" for p in specs + feats}
    plataforma = [{"numero": p["number"], "titulo": p["title"], "url": p["html_url"],
                   "estado": "fusionado" if p.get("merged_at") else ("abierto" if p["state"] == "open" else "cerrado")}
                  for p in core_prs if any(re.search(rf"\b{re.escape(r)}\b", p.get("body") or "") for r in refs)]
    return {"tipo": "peticion", "numero": n, "titulo": issue["title"], "url": issue["html_url"],
            "origen": "alerta" if alerta else "persona", "abierta": issue["state"] == "open",
            "etapas": et, "plataforma": plataforma}


def flujo_cambio(f: Fuente, pr: dict, promos: list[dict], staging: str | None, responsable: str) -> dict:
    autor = pr["user"]["login"]
    et = [etapa_pr(f, "codigo", "Código", pr, responsable)]
    et += etapas_entrega(f, pr["merge_commit_sha"] if pr.get("merged_at") else None, promos, staging)
    return {"tipo": "cambio", "numero": pr["number"], "titulo": pr["title"], "url": pr["html_url"],
            "origen": "dependabot" if autor == DEPENDABOT else ("agente" if autor == AGENTE else "persona"),
            "abierta": pr["state"] == "open", "etapas": et, "plataforma": []}


def construir(api: Api, app: str, core: str, gitops: str, responsable: str, n_pet: int, n_cambios: int) -> dict:
    f = Fuente(api, app, core, gitops)
    prs = f.get(f"repos/{app}/pulls?state=all&per_page=100&sort=created&direction=desc")
    gprs = f.get(f"repos/{gitops}/pulls?state=all&per_page=100&sort=created&direction=desc")
    core_prs = f.get(f"repos/{core}/pulls?state=all&per_page=100&sort=created&direction=desc")
    promos = [p for p in gprs if PROMOCION.match(p["title"])]
    fusionadas = sorted((p for p in promos if p.get("merged_at")), key=lambda p: p["merged_at"])
    staging = PROMOCION.match(fusionadas[-1]["title"]).group(1) if fusionadas else None
    main = f.get(f"repos/{app}/commits/main")["sha"]
    issues = [i for i in f.get(f"repos/{app}/issues?state=all&per_page=50&sort=created&direction=desc")
              if "pull_request" not in i][:n_pet]
    flujos = [flujo_peticion(f, i, prs, promos, staging, core_prs, responsable) for i in issues]
    sin_peticion = [p for p in prs if not re.match(r"^(spec|feat)/\d+-", p["head"]["ref"])
                    and (p["state"] == "open" or p.get("merged_at"))][:n_cambios]
    cambios = [flujo_cambio(f, p, promos, staging, responsable) for p in sin_peticion]
    # Lo que no pertenece a ninguna petición: workflows en rojo en main y PRs abiertos de core y gitops.
    rojos = []
    for nombre, repo in (("app", app), ("core", core), ("gitops", gitops)):
        ultimo: dict[str, dict] = {}
        for r in f.get(f"repos/{repo}/actions/runs?branch=main&per_page=50")["workflow_runs"]:
            if r["status"] == "completed" and r.get("conclusion") not in (None, "skipped", "cancelled"):
                if r["path"] not in ultimo or r["created_at"] > ultimo[r["path"]]["created_at"]:
                    ultimo[r["path"]] = r
        rojos += [{"repo": nombre, "workflow": r["name"], "cuando": r["created_at"], "url": r["html_url"],
                   "conclusion": r["conclusion"]} for r in ultimo.values() if r["conclusion"] in FALLO]
    abiertos_plataforma = [{"repo": nombre, "numero": p["number"], "titulo": p["title"], "url": p["html_url"],
                            "autor": p["user"]["login"], "desde": p["created_at"]}
                           for nombre, lista in (("core", core_prs), ("gitops", gprs))
                           for p in lista if p["state"] == "open" and not PROMOCION.match(p["title"])]
    todos = flujos + cambios
    te_toca = [(x["tipo"], x["numero"], e["nombre"], e["accion"]) for x in todos for e in x["etapas"]
               if e["estado"] == "te_toca"]
    return {
        "generado": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "repos": {"app": app, "core": core, "gitops": gitops},
        "resumen": {
            "te_toca": len(te_toca) + len(abiertos_plataforma),
            "fallos": sum(1 for x in todos for e in x["etapas"] if e["estado"] == "fallo") + len(rojos),
            "en_curso": sum(1 for x in todos for e in x["etapas"] if e["estado"] == "en_curso"),
            "staging": (staging or "")[:7], "main": main[:7],
            "staging_al_dia": bool(staging) and f.incluido(main, staging),
        },
        "peticiones": flujos,
        "cambios": cambios,
        "plataforma": {"workflows_rojos": rojos, "prs_abiertos": abiertos_plataforma},
    }



class Estado:
    def __init__(self) -> None:
        self.datos: dict | None = None
        self.error = ""
        self.ultima_ok = ""
        self.ultimo_intento = ""
        self.segundos = 0.0
        self.cerrojo = threading.Lock()


def ahora() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def bucle(gh: GitHub, repos: dict[str, str], responsable: str, estado: Estado, intervalo: int) -> None:
    while True:
        inicio = time.monotonic()
        try:
            datos = construir(gh, repos["app"], repos["core"], repos["gitops"], responsable, 12, 10)
            with estado.cerrojo:
                estado.datos, estado.error, estado.ultima_ok = datos, "", ahora()
            print(f"flujo actualizado en {time.monotonic() - inicio:.0f} s; quedan {gh.restantes} consultas",
                  flush=True)
        except Exception as exc:  # noqa: BLE001 (un fallo de red no debe tirar la aplicación)
            with estado.cerrojo:
                estado.error = f"{type(exc).__name__}: {exc}"[:300]
            print(f"error leyendo GitHub: {exc}", file=sys.stderr, flush=True)
        with estado.cerrojo:
            estado.ultimo_intento, estado.segundos = ahora(), round(time.monotonic() - inicio, 1)
        time.sleep(intervalo)


def manejador(estado: Estado, gh: GitHub, html: bytes):
    class Manejador(BaseHTTPRequestHandler):
        def _enviar(self, codigo: int, cuerpo: bytes, tipo: str) -> None:
            self.send_response(codigo)
            self.send_header("Content-Type", tipo)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                             "font-src https://fonts.gstatic.com; script-src 'self' 'unsafe-inline'; "
                             "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(cuerpo)

        def do_GET(self) -> None:  # noqa: N802
            ruta = self.path.split("?")[0]
            with estado.cerrojo:
                datos, salud = estado.datos, {
                    "ok": not estado.error and estado.datos is not None, "error": estado.error,
                    "ultima_lectura": estado.ultima_ok, "ultimo_intento": estado.ultimo_intento,
                    "segundos": estado.segundos, "consultas_restantes": gh.restantes, "consultas": gh.llamadas}
            if ruta in ("/", "/index.html"):
                self._enviar(200, html, "text/html; charset=utf-8")
            elif ruta == "/api/flujo":
                if datos is None:
                    self._enviar(503, json.dumps({"error": salud["error"] or "Primera lectura en curso"},
                                                 ensure_ascii=False).encode(), "application/json")
                else:
                    self._enviar(200, json.dumps({**datos, "salud": salud}, ensure_ascii=False).encode(),
                                 "application/json")
            elif ruta in ("/api/salud", "/health"):
                self._enviar(200, json.dumps(salud, ensure_ascii=False).encode(), "application/json")
            else:
                self._enviar(404, b"{}", "application/json")

        def do_HEAD(self) -> None:  # noqa: N802
            self.do_GET()

        def log_message(self, *args: object) -> None:
            pass

    return Manejador


def main() -> None:
    repos = {"app": os.environ["REPO_APP"], "core": os.environ["REPO_CORE"], "gitops": os.environ["REPO_GITOPS"]}
    with open(os.environ.get("GITHUB_TOKEN_FILE", "/run/secrets/github/token"), encoding="utf-8") as f:
        gh = GitHub(f.read().strip())
    with open(os.environ.get("HTML", os.path.join(os.path.dirname(__file__), "flujo.html")), "rb") as f:
        html = f.read()
    estado = Estado()
    threading.Thread(target=bucle, args=(gh, repos, os.environ["RESPONSABLE"], estado,
                                         int(os.environ.get("INTERVALO", "60"))), daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PUERTO", "8088"))),  # noqa: S104
                        manejador(estado, gh, html)).serve_forever()


if __name__ == "__main__":
    main()
