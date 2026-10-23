import unittest

from exportador import agregar, exposicion, resumen_modelo


def ejecucion(i, conclusion="success", inicio="2026-10-24T10:00:00Z", fin="2026-10-24T10:01:30Z", status="completed"):
    return {"id": i, "status": status, "conclusion": conclusion, "run_started_at": inicio, "updated_at": fin}


class Exportador(unittest.TestCase):
    def test_agrega_resultados_duracion_y_modelo(self):
        ejecuciones = [ejecucion(1), ejecucion(2, "failure", fin="2026-10-24T10:00:30Z"), ejecucion(3, "skipped"),
                       ejecucion(4, None, status="in_progress", inicio="2026-10-24T11:00:00Z")]
        modelo = {1: resumen_modelo({"num_turns": 12, "total_cost_usd": 0.31, "is_error": False}),
                  2: resumen_modelo({"num_turns": 3, "total_cost_usd": 0.05, "is_error": True})}
        m = {(n, tuple(sorted(e.items()))): v for n, e, v in agregar("respuesta", ejecuciones, modelo)}
        self.assertEqual(m[("agentes_ejecuciones", (("agente", "respuesta"), ("resultado", "success")))], 1)
        self.assertNotIn(("agentes_ejecuciones", (("agente", "respuesta"), ("resultado", "skipped"))), m)
        self.assertEqual(m[("agentes_duracion_media_segundos", (("agente", "respuesta"),))], 60.0)
        self.assertEqual(m[("agentes_turnos_total", (("agente", "respuesta"),))], 15)
        self.assertEqual(m[("agentes_coste_equivalente_usd_total", (("agente", "respuesta"),))], 0.36)
        self.assertEqual(m[("agentes_errores_modelo_total", (("agente", "respuesta"),))], 1)
        self.assertEqual(m[("agentes_ultima_ejecucion_timestamp", (("agente", "respuesta"),))], 1792839600.0)

    def test_sin_ejecuciones(self):
        m = {n: v for n, _, v in agregar("triage", [], {})}
        self.assertEqual(m["agentes_turnos_total"], 0)
        self.assertNotIn("agentes_ultima_ejecucion_timestamp", m)

    def test_formato_de_exposicion_agrupa_cada_metrica(self):
        texto = exposicion([("a", {"agente": "x", "r": "y"}, 1), ("b", {}, 1), ("a", {"agente": "z"}, 2)])
        self.assertEqual(texto, '# TYPE a gauge\na{agente="x",r="y"} 1\na{agente="z"} 2\n# TYPE b gauge\nb 1\n')

    def test_resumen_tolera_campos_vacios(self):
        self.assertEqual(resumen_modelo({}), {"turnos": 0, "coste": 0.0, "error": False})


if __name__ == "__main__":
    unittest.main()
