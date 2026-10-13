# flux-playground

Run this workflow on macOS or Ubuntu with Multipass, Make, `kubectl`, `curl`,
and `shasum` installed. LoadBalancer routing requires `sudo` access and uses
macOS's `route` command or Ubuntu's `ip` command. The 1Password CLI (`op`) is
needed only when reading the GitHub App private key from 1Password.

On Ubuntu, `multipass list` must work as your normal user. If it reports
`multipass socket access denied`, check the socket's group with
`ls -l /var/snap/multipass/common/multipass_socket`. An administrator must grant
your user membership in that group (normally `sudo`); log out and back in so
the new membership takes effect. See the
[Multipass installation guide](https://canonical.com/multipass/docs/latest/how-to-guides/install-multipass/).

If it instead reports `The client is not authenticated`, an administrator
must set a service passphrase with `sudo multipass set local.passphrase`.
Then run `multipass authenticate` as your normal user and enter that passphrase.
Both commands prompt for hidden input. Verify access with `multipass list`
before running `make up CLUSTER_NAME=my-cluster`.

`make up CLUSTER_NAME=my-cluster` creates the Ubuntu VM, installs RKE2
`v1.36.4+rke2r1`, and waits for the node to become Ready. A stopped VM is started
without reinstalling RKE2.
It also sets up `kubectl` for the VM's `ubuntu` user and writes
`~/.kube/my-cluster.yaml` on the host with the VM's current IP address.
The exported context is named `my-cluster`, so `kubie ctx my-cluster`
can select it.
Use `KUBECONFIG="$HOME/.kube/my-cluster.yaml" kubectl get nodes` to access it.

`CLUSTER_NAME` is required for every cluster-related Make command. It names the
VM, exported kubeconfig, context, and fleet path `clusters/my-cluster`. Pass the
same name to subsequent commands or export it once with
`export CLUSTER_NAME=my-cluster`. `make cache-rke2` does not require a cluster name.

Existing VMs are not renamed. If the old VM name differs from the fleet cluster
name (for example, `flux-playground` versus `frasers-flux-playground` on the Mac),
recreate the VM under the fleet cluster name. Changing `CLUSTER_NAME` selects a
different VM; the old VM remains until explicitly deleted.

`make cache-rke2` downloads and verifies the RKE2 installer, binary, and image
archive into `.cache/rke2/`. `make up` uses this cache when creating a new VM.
The guest copy is kept under `/home/ubuntu/.cache/rke2/`.
The cache survives `make reset`, which deletes only the VM and its disk.

With `CLUSTER_NAME` exported, use `make down` to shut down the VM, `make reset`
to delete it, and `make ip` to print its current address.

After MetalLB has a pool for `172.30.255.241-172.30.255.246` and a
`LoadBalancer` Service, run `make setup-lb-routing CLUSTER_NAME=my-cluster` on the
host. It routes that range through the VM's current Multipass IP and prints the
route to verify it.
Run it again after a host reboot or whenever the VM's IP changes; an existing
route is updated. Routes are temporary on both operating systems. Once Traefik
has the `172.30.255.241` address, check it with
`curl -v http://172.30.255.241/` (a Traefik 404 still confirms connectivity).

## Flux bootstrap

Follow these steps from the repository root after completing the host setup
above. Choose a cluster name whose `clusters/<name>` directory already exists
in the fleet repository on the selected branch. The steps below export that name
once so all Make commands use the same cluster.

1. Create or start the VM and wait for Kubernetes to be ready, then select its
   kubeconfig for subsequent `kubectl` commands:

   ```sh
   export CLUSTER_NAME=my-cluster
   make up
   export KUBECONFIG="$HOME/.kube/$CLUSTER_NAME.yaml"
   kubectl get nodes
   ```

2. Install Flux Operator. This installs chart `0.58.1` through RKE2's Helm
   controller and waits for the operator deployment and `FluxInstance` CRD:

   ```sh
   make install-flux-operator
   ```

3. Install the GitHub App credentials. Sign in to the 1Password CLI for the
   `frontierhq` account and replace the example reference with the private-key
   field's actual `op://` reference:

   ```sh
   export FLUX_GITHUB_APP_PRIVATE_KEY_OP_REF='op://vault/item/private-key'
   make install-flux-github-app-secret
   ```

   Alternatively, export `FLUX_GITHUB_APP_PRIVATE_KEY` with the PEM key before
   running the Make command. The key is sent directly to Kubernetes and is not
   saved in this repository. The GitHub App ID defaults to `4831148` and its
   installation owner to `frontierhq`.

4. Apply the cluster properties and Flux configuration. `clusters/$CLUSTER_NAME`
   must already exist in `frontierhq/flux-fleet` on the selected branch, and the
   GitHub App must have access to that repository and any other private sources
   it uses:

   ```sh
   make apply-flux-bootstrap
   ```

   The branch defaults to `main`. Add `FLEET_BRANCH=my-branch` to the command
   when bootstrapping from another branch. Use `frasers-flux-playground` as the
   exported cluster name when rebuilding the Mac VM for its existing fleet path.

5. Wait for Flux to be ready and for the fleet source and root Kustomization
   to reconcile:

   ```sh
   kubectl -n flux-system wait fluxinstance/flux --for=condition=Ready --timeout=5m
   kubectl -n flux-system wait gitrepository/flux-system --for=condition=Ready --timeout=5m
   kubectl -n flux-system wait kustomization/flux-system --for=condition=Ready --timeout=10m
   kubectl -n flux-system get pods
   kubectl -n flux-system get gitrepositories,kustomizations
   ```

   The source and root Kustomization should show `READY=True`. Fleet workloads
   may continue reconciling afterward. If a wait times out, inspect the failing
   resource with `kubectl -n flux-system describe` followed by its resource/name
   from the commands above.

6. Once the fleet has configured MetalLB's pool and Traefik's `LoadBalancer`
   Service has the `172.30.255.241` address, set up host routing and check access:

   ```sh
   kubectl get services -A
   make setup-lb-routing
   curl -v http://172.30.255.241/
   ```

   A Traefik 404 confirms connectivity. Rerun the routing command after a host
   reboot or whenever the VM's IP changes.

The fleet cluster root selects the local-path provisioner, whose chart creates
the default `local-path` StorageClass after Flux starts. Vault and other PVCs
without an explicit class use it. It stores data on the VM's local disk with a
Delete reclaim policy; `make reset` deletes the disk.
RKE2 starts without its packaged ingress controller. Flux installs Gateway API
CRDs and Traefik after MetalLB is configured, then applies the cluster Gateway.
The `ingress-controller: none` cloud-init setting takes effect only when a new VM
is created. Rebuild with `make reset` followed by `make up` after the fleet and
components changes are available on the branches selected by the FluxInstance.
The running cluster currently tracks `frasdav/surabaya`. Use the fleet
repository's branch handoff workflow before merging so the live cluster does
not switch to a path that is not yet on `main`.
