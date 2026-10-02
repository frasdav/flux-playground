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
