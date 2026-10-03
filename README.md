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
3. Instala Argo CD:
   ```bash
   kubectl create namespace argocd
   kubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml
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
   Entra en https://localhost:8080. Deberías ver `root` y `cluster-namespaces` en verde y el namespace `staging` creado.

## Quién escribe aquí

- Un bot abre los PRs de promoción (cambio de tag de imagen).
- Un humano los aprueba: **el merge es el gate de despliegue**.
- Nunca se suben secretos a este repo.
