SHELL := /bin/sh

INSTANCE ?= flux-playground
UBUNTU_RELEASE ?= 24.04
CPUS ?= 4
MEMORY ?= 8G
DISK ?= 40G
RKE2_VERSION := v1.36.4+rke2r1
RKE2_ARCH := $(if $(filter arm64 aarch64,$(shell uname -m)),arm64,$(if $(filter x86_64 amd64,$(shell uname -m)),amd64,unsupported))
RKE2_CACHE_DIR := $(CURDIR)/.cache/rke2/$(RKE2_VERSION)/$(RKE2_ARCH)
KUBECONFIG := $(HOME)/.kube/$(INSTANCE).yaml
CLUSTER_NAME ?= frasers-flux-playground
LETSENCRYPT_EMAIL ?= admin@frontierhq.net
FLEET_BRANCH ?= main
FLUX_GITHUB_APP_ID ?= 4831148
FLUX_GITHUB_APP_INSTALLATION_OWNER ?= frontierhq

.PHONY: up stop reset ip shell status cache-rke2 install-rke2 install-flux-operator install-flux-github-app-secret apply-flux-bootstrap

up:
	@INSTANCE='$(INSTANCE)' UBUNTU_RELEASE='$(UBUNTU_RELEASE)' CPUS='$(CPUS)' \
		MEMORY='$(MEMORY)' DISK='$(DISK)' RKE2_VERSION='$(RKE2_VERSION)' \
		RKE2_ARCH='$(RKE2_ARCH)' RKE2_CACHE_DIR='$(RKE2_CACHE_DIR)' sh scripts/up.sh

stop:
	multipass stop '$(INSTANCE)'

reset:
	multipass delete --purge '$(INSTANCE)'

ip:
	@multipass info '$(INSTANCE)' | awk '$$1 == "IPv4:" { print $$2; exit }'

shell:
	multipass shell '$(INSTANCE)'

status:
	multipass info '$(INSTANCE)'

cache-rke2:
	@RKE2_VERSION='$(RKE2_VERSION)' RKE2_ARCH='$(RKE2_ARCH)' \
		RKE2_CACHE_DIR='$(RKE2_CACHE_DIR)' sh scripts/cache-rke2.sh

install-rke2:
	@INSTANCE='$(INSTANCE)' RKE2_VERSION='$(RKE2_VERSION)' RKE2_ARCH='$(RKE2_ARCH)' \
		RKE2_CACHE_DIR='$(RKE2_CACHE_DIR)' sh scripts/install-rke2.sh

install-flux-operator:
	@KUBECONFIG='$(KUBECONFIG)' sh scripts/install-flux-operator.sh

install-flux-github-app-secret:
	@KUBECONFIG='$(KUBECONFIG)' FLUX_GITHUB_APP_ID='$(FLUX_GITHUB_APP_ID)' \
		FLUX_GITHUB_APP_INSTALLATION_OWNER='$(FLUX_GITHUB_APP_INSTALLATION_OWNER)' \
		sh scripts/install-flux-github-app-secret.sh

apply-flux-bootstrap:
	@KUBECONFIG='$(KUBECONFIG)' CLUSTER_NAME='$(CLUSTER_NAME)' \
		LETSENCRYPT_EMAIL='$(LETSENCRYPT_EMAIL)' FLEET_BRANCH='$(FLEET_BRANCH)' \
		sh scripts/apply-flux-bootstrap.sh
