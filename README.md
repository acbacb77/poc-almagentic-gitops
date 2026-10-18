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
6. Prueba la API desplegada (solo es accesible dentro del clúster; `port-forward` la acerca a tu equipo):
   ```bash
   kubectl -n staging rollout status deploy/poc-almagentic-app
   kubectl -n staging port-forward svc/poc-almagentic-app 8000:80
   curl -s "localhost:8000/api/v1/productos?q=taza"
   ```

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
