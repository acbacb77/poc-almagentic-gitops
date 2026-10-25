import io
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

import flujo_alm as f

APP, CORE, GITOPS = "o/app", "o/core", "o/gitops"
T = "2026-10-25T10:00:00Z"


def pr(n, ref, autor="almagentic-agent[bot]", merged=None, state="closed", title=None):
    return {"number": n, "title": title or f"PR {n}", "head": {"ref": ref, "sha": f"h{n}"},
            "user": {"login": autor}, "state": state, "merged_at": merged, "merge_commit_sha": f"m{n}",
            "created_at": T, "html_url": f"https://github.com/{APP}/pull/{n}",
            "merged_by": {"login": "persona"}, "body": ""}


def datos():
    issue = {"number": 7, "title": "[Petición] Algo", "state": "open", "user": {"login": "persona"},
             "created_at": T, "html_url": f"https://github.com/{APP}/issues/7", "body": "",
             "labels": [{"name": "tipo:mejora"}, {"name": "triage:propuesto"}]}
    triage = {"user": {"login": "github-actions[bot]"}, "created_at": T, "html_url": "u",
              "body": "### 🤖 Propuesta de triage\n**Preguntas abiertas**\n\n- ¿a?\n- ¿b?\n\n**Riesgos**"}
    promo = pr(3, "promo", autor="almagentic-promoter-gitops[bot]", merged=T,
               title="deploy(staging): app sha256:1 (aaaaaaa)")
    d = {
        f"repos/{APP}/pulls?state=all&per_page=100&sort=created&direction=desc": [],
        f"repos/{GITOPS}/pulls?state=all&per_page=100&sort=created&direction=desc": [promo],
        f"repos/{CORE}/pulls?state=all&per_page=100&sort=created&direction=desc": [],
        f"repos/{APP}/commits/main": {"sha": "aaaaaaa000"},
        f"repos/{APP}/issues?state=all&per_page=50&sort=created&direction=desc": [issue],
        f"repos/{APP}/issues/7/comments?per_page=100": [triage],
        f"repos/{APP}/issues/7/events?per_page=100": [],
        f"repos/{APP}/compare/aaaaaaa000...aaaaaaa": {"status": "identical"},
    }
    for r in (APP, CORE, GITOPS):
        d[f"repos/{r}/actions/runs?branch=main&per_page=50"] = {"workflow_runs": []}
    return d


class Flujo(unittest.TestCase):
    def test_peticion_esperando_decision_con_sus_preguntas(self):
        r = f.construir(datos().__getitem__, APP, CORE, GITOPS, "persona", 12, 10)
        etapas = {e["id"]: e for e in r["peticiones"][0]["etapas"]}
        self.assertEqual(etapas["triage"]["estado"], "hecho")
        self.assertEqual(etapas["decision"]["estado"], "te_toca")
        self.assertIn("2 preguntas abiertas", etapas["decision"]["accion"])
        self.assertTrue(r["resumen"]["staging_al_dia"])
        self.assertEqual(r["resumen"]["te_toca"], 1)


class Respuesta(io.BytesIO):
    def __init__(self, cuerpo):
        super().__init__(json.dumps(cuerpo).encode())
        self.headers = {"ETag": '"e1"', "X-RateLimit-Remaining": "99"}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Cliente(unittest.TestCase):
    def test_etag_y_comparaciones_fijas_no_gastan_cuota(self):
        gh = f.GitHub("t")
        vistas = []

        def abrir(peticion, timeout):
            vistas.append(peticion.headers.get("If-none-match"))
            if peticion.full_url.endswith("/pulls") and len(vistas) > 1:
                raise urllib.error.HTTPError(peticion.full_url, 304, "Not Modified",
                                             {"X-RateLimit-Remaining": "99"}, None)
            return Respuesta({"n": len(vistas)})

        with mock.patch.object(urllib.request, "urlopen", abrir):
            self.assertEqual(gh("repos/o/app/pulls"), {"n": 1})
            self.assertEqual(gh("repos/o/app/pulls"), {"n": 1})
            self.assertEqual(vistas, [None, '"e1"'])
            gh("repos/o/app/compare/aaaaaaa...bbbbbbb")
            gh("repos/o/app/compare/aaaaaaa...bbbbbbb")
        self.assertEqual(len(vistas), 3)
        self.assertEqual(gh.restantes, 99)


class Servidor(unittest.TestCase):
    def test_sirve_panel_flujo_y_salud(self):
        estado = f.Estado()
        srv = ThreadingHTTPServer(("127.0.0.1", 0), f.manejador(estado, f.GitHub("t"), b"<html>panel</html>"))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        abrir = urllib.request.build_opener(urllib.request.ProxyHandler({})).open
        try:
            with self.assertRaises(urllib.error.HTTPError) as e:
                abrir(f"{base}/api/flujo")
            self.assertEqual(e.exception.code, 503)
            estado.datos = {"resumen": {}}
            self.assertIn("salud", json.loads(abrir(f"{base}/api/flujo").read()))
            r = abrir(f"{base}/")
            self.assertIn(b"panel", r.read())
            self.assertIn("frame-ancestors 'none'", r.headers["Content-Security-Policy"])
            self.assertTrue(json.loads(abrir(f"{base}/health").read())["ok"])
        finally:
            srv.shutdown()


if __name__ == "__main__":
    unittest.main()
