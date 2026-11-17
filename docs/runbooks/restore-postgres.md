# Restore Postgres from R2

Backups: CNPG Barman Cloud plugin, `ObjectStore duit-backups` → `s3://homelab-backups/<env>/duit-db/`.
WAL is archived continuously; base backup nightly at 03:00 UTC (`ScheduledBackup duit-db-nightly`);
14-day retention. Anything in that window can be restored, to the latest point or to a timestamp.

All commands from PowerShell with `$env:KUBECONFIG = Join-Path $HOME ".kube\homelab.yaml"`.
None of them print secrets.

## 1. Is the backup healthy?

```powershell
kubectl -n duit-staging get cluster duit-db -o json | ConvertFrom-Json |
  % { $_.status.conditions } | ? type -in "ContinuousArchiving","LastBackupSucceeded" | select type,status,message
kubectl -n duit-staging get backup                          # phase completed
kubectl -n duit-staging get objectstore duit-backups -o yaml  # status.serverRecoveryWindow: first point
kubectl -n duit-staging exec duit-db-1 -c postgres -- psql -qAt -c "select * from pg_stat_archiver"
```

## 2. Restore into a new cluster (drill, or before a real switch)

A restore always creates a **new** cluster; CNPG never restores over a running one. Apply this
next to the original (same namespace, so it reuses `duit-backups` and its R2 key):

```yaml
apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata:
  name: duit-db-restore
  namespace: duit-staging
spec:
  instances: 1
  imageName: ghcr.io/cloudnative-pg/postgresql:18.6-minimal-trixie # same major as the backup
  bootstrap:
    recovery:
      source: origin
      # Point in time instead of "latest" (e.g. just before a bad migration):
      # recoveryTarget: { targetTime: "2026-11-17T03:55:00Z" }
  externalClusters:
    - name: origin
      plugin:
        name: barman-cloud.cloudnative-pg.io
        parameters: { barmanObjectName: duit-backups, serverName: duit-db }
  storage: { size: 2Gi }
  resources:
    requests: { cpu: 50m, memory: 256Mi }
    limits: { memory: 256Mi }
```

No `plugins:` entry on purpose: the restored cluster must not archive. If it ever archives, give
it its own `serverName`, or it would write into the original's backup path.

Wait for `Cluster in healthy state`, then compare (counts only, no row contents):

```powershell
$q = "select 'users',count(*) from users union all select 'accounts',count(*) from accounts " +
     "union all select 'categories',count(*) from categories union all select 'transactions',count(*) from transactions " +
     "union all select 'alembic '||version_num,1 from alembic_version order by 1"
foreach ($c in "duit-db-1","duit-db-restore-1") { "== $c"; kubectl -n duit-staging exec $c -c postgres -- psql -d duit -qAt -c $q }
```

Drill cleanup: `kubectl -n duit-staging delete cluster duit-db-restore` (its PVC goes with it).

## 3. Real recovery (the original is lost or corrupted)

1. Restore as above and verify.
2. Point the app at it: in `deploy/base/postgres.yaml` the Cluster is `duit-db`. Simplest is to
   restore *as* `duit-db`: delete the broken Cluster (and its PVC), then put the
   `bootstrap.recovery` + `externalClusters` block into `postgres.yaml` in place of
   `bootstrap.initdb`, with a **new** `serverName` for archiving (e.g. `duit-db-r1`) in the
   ObjectStore parameters, so the new timeline never overwrites the old backups. Sync.
3. Secret `duit-db-app` is recreated by CNPG with a new password; the API reads it at start,
   so roll the API (`kubectl -n duit-staging rollout restart deploy/duit-api`).
4. The app's encrypted columns need the same `DUIT_DATA_KEY` (SOPS, in Git): nothing to do
   unless the key itself was lost. **Back that key up separately; a backup without it cannot
   decrypt account numbers, statement passwords or TOTP secrets.**

## Drill log

| Date | What | Result |
|---|---|---|
| 2026-11-17 | Latest-point restore of staging into `duit-db-restore`, with a marker row written after the base backup (so only WAL replay could bring it back) | Healthy in 71 s; all table counts and the marker matched |
