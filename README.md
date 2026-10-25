# poc-almagentic-gitops

Estado deseado del cluster Kubernetes local (Docker Desktop) de la POC de **ALM agéntico**.
Argo CD corre dentro del cluster y **lee** este repo (modelo pull): GitHub nunca se conecta al cluster.

## Estructura

```
bootstrap/root-app.yaml   # lo único que se aplica a mano
argocd-apps/              # una Application de Argo CD por componente
cluster/                  # componentes del cluster (namespaces, monitorización, políticas)
apps/                     # manifiestos de las aplicaciones (tarea 10)
```

## Puesta en marcha (una sola vez)

1. En Docker Desktop: **Settings → Kubernetes → Enable Kubernetes**. Asigna al menos 8 GB de memoria en **Resources**.
2. Comprueba el contexto:
   ```bash
   kubectl config use-context docker-desktop
   kubectl get nodes
   ```
3. Instala Argo CD (versión fijada; `stable` cambia sin aviso):
   ```bash
   kubectl create namespace argocd
   kubectl apply -n argocd --server-side --force-conflicts -f https://raw.githubusercontent.com/argoproj/argo-cd/v3.5.4/manifests/install.yaml
   kubectl -n argocd rollout status deploy/argocd-server
   ```
4. Conecta Argo CD con este repo:
   ```bash
   kubectl apply -f https://raw.githubusercontent.com/acbacb77/poc-almagentic-gitops/main/bootstrap/root-app.yaml
   ```
5. Abre la consola de Argo CD:
   ```bash
   kubectl -n argocd port-forward svc/argocd-server 8080:443
   # usuario: admin
   kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath="{.data.password}" | base64 -d
   ```
   Entra en https://localhost:8080. Deberías ver `root`, `cluster-namespaces` y `poc-almagentic-app-staging` en verde.
6. Prueba la API desplegada. El `Service` es de tipo `LoadBalancer` y Docker Desktop lo publica en el puerto 8000 del equipo:
   ```bash
   kubectl -n staging rollout status deploy/poc-almagentic-app
   kubectl -n staging get svc poc-almagentic-app     # EXTERNAL-IP: localhost
   curl -s "localhost:8000/api/v1/productos?q=taza"  # desde un contenedor: host.docker.internal:8000
   ```

## Observabilidad (tarea 11)

Prometheus, Alertmanager y Grafana van en el namespace `observabilidad`, cada uno con su aplicación de Argo CD. Hay dos piezas propias en `cluster/observabilidad/`, escritas solo con la biblioteca estándar y ejecutadas con la imagen oficial de Python fijada por digest:

- **Puente alerta → issue** (`puente.py`). Alertmanager lo llama y el puente abre en app un issue con el formato de la plantilla de peticiones. Ese issue sigue el camino normal: triage, `aprobado` y agente.
- **Exportador de telemetría de los agentes** (`exportador.py`). Lee de la API de GitHub las ejecuciones de triage, revisor, `@claude` y agente autónomo, y Grafana las muestra.

Antes de sincronizar hay que crear dos `Secret` a mano, una sola vez (nunca en git):

1. Crea un *fine-grained personal access token* en GitHub → *Settings → Developer settings → Fine-grained tokens*:
   - **Repository access**: solo `poc-almagentic-app`.
   - **Permissions**: *Issues* de lectura y escritura, y *Actions* de solo lectura.
   - **Expiration**: 90 días.
2. Crea los dos secretos desde WSL. La terminal es zsh, por eso se usa `read -s`:
   ```bash
   kubectl create namespace observabilidad --dry-run=client -o yaml | kubectl apply -f -
   printf 'Token de GitHub: '; read -s T; echo
   kubectl -n observabilidad create secret generic observabilidad-github --from-literal=token="$T"; unset T
   printf 'Contraseña de Grafana: '; read -s P; echo
   kubectl -n observabilidad create secret generic grafana-admin --from-literal=admin-user=admin --from-literal=admin-password="$P"; unset P
   ```

| Servicio | Dirección |
|---|---|
| Grafana (paneles «API del catálogo» y «Actividad de los agentes») | http://localhost:3000 |
| Prometheus (consultas y alertas) | http://localhost:9090 |
| Métricas de la API | solo dentro del clúster: puerto 9000 del pod, que el `Service` no publica |

Para provocar la alerta de búsquedas rechazadas, lanza durante un par de minutos búsquedas con un `q` de más de 200 caracteres (el máximo desde app#31; antes era 100):

```bash
for i in $(seq 1 60); do curl -s -o /dev/null "localhost:8000/api/v1/productos?q=$(printf 'x%.0s' {1..250})"; sleep 2; done
```

### Estado del proceso (tarea E)

`estado_alm.py` lee los tres repos de GitHub y las aplicaciones de Argo CD (con una cuenta de servicio que solo puede leerlas) y alimenta el panel **«Proceso ALM»** de Grafana:

- En qué fase está cada petición y si te toca hacer algo.
- Qué te espera: PRs por aprobar o fusionar, issues por decidir y promociones sustituidas que hay que cerrar.
- Workflows en rojo en `main`, incluidos los que no llegaron a arrancar.
- Staging frente a `main` de app, estado de Argo CD, caducidad del token y métricas DORA aproximadas.

**Flujo ALM** (`flujo_alm.py` y `flujo.html`, en `localhost:8088`) es la vista del mismo proceso pensada para seguirlo a diario. Cada petición ocupa una fila y sus ocho etapas van en columnas: petición, triage, decisión, spec, código, imagen, promoción y staging. Arriba aparece la cola de lo que te toca, y al pulsar una casilla se ve quién hizo esa etapa y cuándo, sus checks y qué falta. Lee GitHub en vivo cada 30 segundos con peticiones condicionales (ETag), así que en estado estable apenas gasta cuota. `/api/salud` dice cuándo fue la última lectura y si falló.

Las alertas del proceso (`ambito="proceso"`) solo se ven en Grafana y Prometheus: Alertmanager las envía a un receptor vacío y no abren issues. Usa el mismo Secret `observabilidad-github`; no hace falta crear nada nuevo.

## Cómo llega una versión a staging (tarea 10)

1. Un merge a `main` de app publica una imagen firmada (`supply-chain.yml`).
2. El promotor (`almagentic-promoter-gitops`) verifica su procedencia, su SBOM y su firma, y abre aquí un PR que cambia el digest en `apps/poc-almagentic-app/kustomization.yaml`. El PR lista los commits de app que entran.
3. El check `verify / images` vuelve a verificar todas las imágenes de `apps/`: por digest, construidas desde main de app y firmadas por `poc-almagentic-core/reusable-supply-chain.yml`.
4. **Fusionar el PR aprueba el despliegue.** Argo CD sincroniza `staging` en pocos minutos.

Para volver atrás, revierte el PR de promoción: Argo CD vuelve al digest anterior.

## Quién escribe aquí

- Un bot abre los PRs de promoción (cambio de digest de imagen).
- Un humano los aprueba: **el merge es el gate de despliegue**.
- Nunca se suben secretos a este repo.
