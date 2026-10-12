## What & why



## How was this tested?

<!--
Record the commands run, their results, and any validation limits.
Where relevant, include:
- VM changes: fresh creation and rerunning make up with the same CLUSTER_NAME against an existing VM.
- Bootstrap changes: FluxInstance, GitRepository, and root Kustomization reach Ready.
- Routing changes: adding a route, rerunning setup, and updating after a VM IP change.
-->

## Checklist

- [ ] `README.md` updated if setup steps or behavior changed
- [ ] Changed shell scripts pass syntax checks
- [ ] Changed Make targets checked with `make -n`
- [ ] Host-specific changes checked on macOS and Ubuntu, or untested paths noted
- [ ] Bootstrap changes verified against a running cluster, or validation limits noted
- [ ] RKE2 version changes include updated checksums for both amd64 and arm64
