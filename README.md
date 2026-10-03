# flux-playground

`make up` creates the Ubuntu VM, installs RKE2 `v1.36.4+rke2r1`, and waits for
the node to become Ready. A stopped VM is started without reinstalling RKE2.
It also sets up `kubectl` for the VM's `ubuntu` user and writes
`~/.kube/flux-playground.yaml` on the Mac with the VM's current IP address.
The exported context is named `flux-playground`, so `kubie ctx flux-playground`
can select it.
Use `KUBECONFIG="$HOME/.kube/flux-playground.yaml" kubectl get nodes` to access it.

`make cache-rke2` downloads and verifies the RKE2 installer, binary, and image
archive into `.cache/rke2/`. `make up` uses this cache when creating a new VM.
The guest copy is kept under `/home/ubuntu/.cache/rke2/`.
The cache survives `make reset`, which deletes only the VM and its disk.

Use `make stop` to shut down the VM, `make reset` to delete it, and `make ip`
to print its current address.

## Flux bootstrap

`make install-flux-operator` installs Flux Operator chart `0.58.1` through
RKE2's Helm controller and waits for its deployment and `FluxInstance` CRD.

The GitHub App ID defaults to `4831148` and its installation owner to
`frontierhq`. Sign in to the 1Password CLI, then set
`FLUX_GITHUB_APP_PRIVATE_KEY_OP_REF` to the private-key field's `op://` reference
and run `make install-flux-github-app-secret`. The key is read into the process
and sent directly to Kubernetes; it is not saved in this repository. An exported
`FLUX_GITHUB_APP_PRIVATE_KEY` can be used instead.

`make apply-flux-bootstrap` applies the local cluster properties and
`FluxInstance`. By default, it syncs `clusters/frasers-flux-playground` from the
`main` branch of `frontierhq/flux-fleet`. That path must exist on the remote
branch before applying the bootstrap. Override `CLUSTER_NAME` or `FLEET_BRANCH`
on the Make command line when testing another path or branch. The
`FRO_LETSENCRYPT_EMAIL` cluster property defaults to `admin@frontierhq.net`;
override it with `LETSENCRYPT_EMAIL` on the Make command line.
The GitHub App must have access to `flux-fleet` and any other private sources
selected by that cluster.
The running cluster currently tracks `frasdav/surabaya`. Use the fleet
repository's branch handoff workflow before merging so the live cluster does
not switch to a path that is not yet on `main`.
