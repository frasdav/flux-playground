SHELL := /bin/sh

UBUNTU_RELEASE ?= 24.04
CPUS ?= 4
MEMORY ?= 8G
DISK ?= 40G
RKE2_VERSION := v1.36.4+rke2r1
RKE2_ARCH := $(if $(filter arm64 aarch64,$(shell uname -m)),arm64,$(if $(filter x86_64 amd64,$(shell uname -m)),amd64,unsupported))
RKE2_CACHE_DIR := $(CURDIR)/.cache/rke2/$(RKE2_VERSION)/$(RKE2_ARCH)
KUBECONFIG := $(HOME)/.kube/$(CLUSTER_NAME).yaml
FLEET_BRANCH ?= frasdav/Build-fraser-dev-env
FLUX_GITHUB_APP_ID ?= 4831148
FLUX_GITHUB_APP_INSTALLATION_OWNER ?= frontierhq
LB_ROUTE_CIDR ?= 172.30.255.240/29
LB_CHECK_IP ?= 172.30.255.241

.PHONY: check-cluster-name up down reset ip shell status cache-rke2 install-rke2 install-flux-operator install-flux-github-app-secret apply-flux-bootstrap setup-lb-routing

up: check-cluster-name
	@CLUSTER_NAME='$(CLUSTER_NAME)' UBUNTU_RELEASE='$(UBUNTU_RELEASE)' CPUS='$(CPUS)' \
		MEMORY='$(MEMORY)' DISK='$(DISK)' RKE2_VERSION='$(RKE2_VERSION)' \
		RKE2_ARCH='$(RKE2_ARCH)' RKE2_CACHE_DIR='$(RKE2_CACHE_DIR)' sh scripts/up.sh

down: check-cluster-name
	multipass stop '$(CLUSTER_NAME)'

reset: check-cluster-name
	multipass delete --purge '$(CLUSTER_NAME)'

ip: check-cluster-name
	@multipass info '$(CLUSTER_NAME)' | awk '$$1 == "IPv4:" { print $$2; exit }'

shell: check-cluster-name
	multipass shell '$(CLUSTER_NAME)'

status: check-cluster-name
	multipass info '$(CLUSTER_NAME)'

setup-lb-routing: check-cluster-name
	@CLUSTER_NAME='$(CLUSTER_NAME)' LB_ROUTE_CIDR='$(LB_ROUTE_CIDR)' \
		LB_CHECK_IP='$(LB_CHECK_IP)' sh scripts/setup-lb-routing.sh

cache-rke2:
	@RKE2_VERSION='$(RKE2_VERSION)' RKE2_ARCH='$(RKE2_ARCH)' \
		RKE2_CACHE_DIR='$(RKE2_CACHE_DIR)' sh scripts/cache-rke2.sh

install-rke2: check-cluster-name
	@CLUSTER_NAME='$(CLUSTER_NAME)' RKE2_VERSION='$(RKE2_VERSION)' RKE2_ARCH='$(RKE2_ARCH)' \
		RKE2_CACHE_DIR='$(RKE2_CACHE_DIR)' sh scripts/install-rke2.sh

install-flux-operator: check-cluster-name
	@KUBECONFIG='$(KUBECONFIG)' sh scripts/install-flux-operator.sh

install-flux-github-app-secret: check-cluster-name
	@KUBECONFIG='$(KUBECONFIG)' FLUX_GITHUB_APP_ID='$(FLUX_GITHUB_APP_ID)' \
		FLUX_GITHUB_APP_INSTALLATION_OWNER='$(FLUX_GITHUB_APP_INSTALLATION_OWNER)' \
		sh scripts/install-flux-github-app-secret.sh

apply-flux-bootstrap: check-cluster-name
	@KUBECONFIG='$(KUBECONFIG)' CLUSTER_NAME='$(CLUSTER_NAME)' \
		FLEET_BRANCH='$(FLEET_BRANCH)' sh scripts/apply-flux-bootstrap.sh

check-cluster-name:
	@if [ -z "$(strip $(CLUSTER_NAME))" ]; then \
		echo "Set CLUSTER_NAME, for example: make up CLUSTER_NAME=my-cluster" >&2; \
		exit 1; \
	fi
