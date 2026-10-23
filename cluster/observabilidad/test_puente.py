import unittest

from puente import cuerpo_issue, marcador, planificar


def alerta(estado="firing", nombre="BusquedasRechazadas", **notas):
    return {"status": estado, "labels": {"alertname": nombre, "severity": "aviso"},
            "annotations": {"summary": "Muchas búsquedas rechazadas", "description": "El 40 % recibe 422", **notas},
            "startsAt": "2026-10-24T10:00:00Z", "endsAt": "2026-10-24T10:30:00Z"}


def aviso(*alertas):
    return {"version": "4", "alerts": list(alertas)}


class Puente(unittest.TestCase):
    def test_crea_issue_con_la_plantilla_y_el_marcador(self):
        (accion,) = planificar(aviso(alerta()), {})
        tipo, datos = accion
        self.assertEqual(tipo, "crear")
        self.assertEqual(datos["title"], "[Alerta] Muchas búsquedas rechazadas")
        self.assertEqual(datos["labels"], ["alerta", "triage:pendiente"])
        for campo in ("### ¿Qué tipo de petición es?", "### ¿Qué necesitas?", "### Urgencia", "El 40 % recibe 422"):
            self.assertIn(campo, datos["body"])
        self.assertTrue(datos["body"].endswith(marcador("BusquedasRechazadas")))

    def test_no_duplica_si_ya_hay_issue_abierto(self):
        self.assertEqual(planificar(aviso(alerta()), {"BusquedasRechazadas": 7})[0][0], "comentar")

    def test_resuelta_comenta_y_no_cierra(self):
        ((tipo, datos),) = planificar(aviso(alerta("resolved")), {"BusquedasRechazadas": 7})
        self.assertEqual((tipo, datos["issue"]), ("comentar", 7))
        self.assertIn("se ha resuelto", datos["body"])

    def test_resuelta_sin_issue_no_hace_nada(self):
        self.assertEqual(planificar(aviso(alerta("resolved")), {}), [])

    def test_una_accion_por_alerta_aunque_venga_repetida(self):
        self.assertEqual(len(planificar(aviso(alerta(), alerta()), {})), 1)

    def test_nombres_raros_se_ignoran(self):
        self.assertEqual(planificar(aviso(alerta(nombre="x -->")), {}), [])

    def test_version_desconocida(self):
        with self.assertRaises(ValueError):
            planificar({"version": "3", "alerts": []}, {})

    def test_texto_neutralizado(self):
        body = cuerpo_issue(alerta(description="<script> @admin"))
        self.assertIn("&lt;script&gt;", body)
        self.assertIn("@​admin", body)


if __name__ == "__main__":
    unittest.main()
