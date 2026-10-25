import unittest

import estado_alm as e

T0 = "2026-10-25T10:00:00Z"


def issue(n, labels=(), state="open", **kw):
    return {"number": n, "title": f"Petición {n}", "state": state, "labels": [{"name": x} for x in labels],
            "created_at": T0, "updated_at": T0, "closed_at": kw.get("closed_at"), "user": {"login": "persona"}}


def pr(n, ref, state="open", merged_at=None, autor="almagentic-agent[bot]", title=None, sha="abc"):
    return {"number": n, "title": title or f"PR {n}", "state": state, "merged_at": merged_at, "draft": False,
            "head": {"ref": ref, "sha": sha}, "merge_commit_sha": f"m{n}", "created_at": T0,
            "user": {"login": autor}}


class Fases(unittest.TestCase):
    def fase(self, i, prs, en_staging=lambda sha: False):
        return e.fase_peticion(i, prs, en_staging)[0::2]

    def test_recorrido_completo(self):
        self.assertEqual(self.fase(issue(1, ["triage:pendiente"]), []), ("triage", ""))
        self.assertEqual(self.fase(issue(1, ["triage:propuesto"]), [])[0], "esperando decisión")
        self.assertEqual(self.fase(issue(1, ["triage:propuesto", "aprobado"]), []), ("spec (agente)", ""))
        spec = pr(2, "spec/1-algo")
        self.assertEqual(self.fase(issue(1, ["aprobado"]), [spec])[0], "spec en revisión")
        spec_ok = pr(2, "spec/1-algo", "closed", T0)
        self.assertEqual(self.fase(issue(1, ["aprobado"]), [spec_ok]), ("implementación (agente)", ""))
        feat = pr(3, "feat/1-algo")
        self.assertEqual(self.fase(issue(1, ["aprobado"]), [spec_ok, feat])[0], "implementación en revisión")
        feat_ok = pr(3, "feat/1-algo", "closed", "2026-10-25T11:00:00Z")
        cerrado = issue(1, ["aprobado"], "closed")
        self.assertEqual(self.fase(cerrado, [spec_ok, feat_ok]),
                         ("pendiente de despliegue", "aprobar la promoción en gitops"))
        self.assertEqual(self.fase(cerrado, [spec_ok, feat_ok], lambda sha: sha == "m3"), ("en staging", ""))

    def test_no_confunde_numeros_de_issue(self):
        self.assertEqual(self.fase(issue(1, ["aprobado"]), [pr(9, "spec/10-otro")])[0], "spec (agente)")

    def test_cerrada_sin_cambios(self):
        self.assertEqual(self.fase(issue(28, ["alerta"], "closed"), [])[0], "cerrada sin cambios")


class Pendientes(unittest.TestCase):
    def test_acciones_por_autor_y_estado(self):
        prs = [pr(1, "feat/1-a"), pr(2, "x", autor="acbacb77"), pr(3, "deps", autor="dependabot[bot]"),
               pr(4, "y"), pr(5, "z", state="closed")]
        prs[3]["checks"] = "rojo"
        prs[0]["aprobado"] = True
        acciones = {p["numero"]: p["accion"] for p in e.pendientes_prs("app", prs, "acbacb77")}
        self.assertEqual(acciones, {1: "fusionar", 2: "fusionar (tuyo)", 3: "revisar y aprobar",
                                    4: "checks en rojo"})

    def test_issue_sin_decidir(self):
        out = e.pendientes_issues([issue(1, ["triage:propuesto"]), issue(2, ["triage:propuesto", "aprobado"]),
                                   issue(3, ["triage:propuesto"], "closed")])
        self.assertEqual([p["numero"] for p in out], [1])


class Promociones(unittest.TestCase):
    def test_sustituidas_y_staging(self):
        prs = [pr(10, "a", "closed", "2026-10-24T10:00:00Z", title="deploy(staging): app sha256:1 (aaaaaaa)"),
               pr(11, "b", "closed", "2026-10-25T10:00:00Z", title="deploy(staging): app sha256:2 (bbbbbbb)"),
               pr(12, "c", title="deploy(staging): app sha256:3 (ccccccc)"),
               pr(13, "d", title="deploy(staging): app sha256:4 (ddddddd)"),
               pr(14, "e", title="fix: otra cosa")]
        prs[3]["created_at"] = "2026-10-25T12:00:00Z"
        p = e.promociones(prs)
        self.assertEqual((p["abiertas"], p["sustituidas"], p["staging_commit"]), (2, 1, "bbbbbbb"))
        self.assertTrue(prs[2].get("sustituida") and not prs[3].get("sustituida"))
        acciones = {x["numero"]: x["accion"] for x in e.pendientes_prs("gitops", prs, "acbacb77")}
        self.assertEqual(acciones[12], "cerrar (promoción sustituida)")

    def test_dora(self):
        ahora = e.ts("2026-10-26T10:00:00Z")
        fus = [pr(1, "a", "closed", "2026-10-25T10:00:00Z", title="deploy(staging): x (aaaaaaa)"),
               pr(2, "b", "closed", "2026-10-20T10:00:00Z", title="deploy(staging): x (bbbbbbb)"),
               pr(3, "c", "closed", "2026-08-01T10:00:00Z", title="deploy(staging): x (ccccccc)")]
        commits = {"aaaaaaa": e.ts("2026-10-25T09:00:00Z"), "bbbbbbb": e.ts("2026-10-20T07:00:00Z")}
        alertas = [{"created_at": "2026-10-25T12:00:00Z", "closed_at": "2026-10-25T14:00:00Z"}]
        d = e.dora(fus, commits.get, alertas, ahora)
        self.assertEqual(d["despliegues_30d"], 2)
        self.assertEqual(d["tiempo_cambio_mediana_segundos"], 7200)
        self.assertEqual(d["recuperacion_mediana_segundos"], 7200)
        self.assertEqual(d["tasa_fallo"], 0.5)


class Workflows(unittest.TestCase):
    def test_solo_el_ultimo_run_de_cada_workflow(self):
        runs = [{"path": "ci.yml", "status": "completed", "conclusion": "failure", "created_at": "2026-10-25T09:00:00Z"},
                {"path": "ci.yml", "status": "completed", "conclusion": "success", "created_at": "2026-10-25T10:00:00Z"},
                {"path": "evals.yml", "status": "completed", "conclusion": "startup_failure", "created_at": T0},
                {"path": "rel.yml", "status": "completed", "conclusion": "skipped", "created_at": T0},
                {"path": "x.yml", "status": "in_progress", "conclusion": None, "created_at": T0}]
        self.assertEqual([r["path"] for r in e.workflows_fallidos(runs)], ["evals.yml"])


class Exposicion(unittest.TestCase):
    def test_escapa_etiquetas(self):
        txt = e.exposicion([("m", {"titulo": 'a "b" \\ c\nd'}, 1)])
        self.assertIn('m{titulo="a \\"b\\" \\\\ c d"} 1', txt)

    def test_un_fallo_de_github_conserva_los_datos_y_marca_el_colector(self):
        estado = e.Estado()
        estado.github = [("alm_pendientes_total", {}, 3)]

        class Roto:
            def get(self, ruta):
                raise OSError("sin red")

        ok = e.ciclo(Roto(), {"app": "a", "core": "c", "gitops": "g"}, "yo", estado, argocd=lambda: [])
        self.assertEqual(ok, 0)
        self.assertIn("alm_pendientes_total 3", estado.texto)
        self.assertIn("alm_colector_ok 0", estado.texto)


if __name__ == "__main__":
    unittest.main()
