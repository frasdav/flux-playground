SHELL := /bin/sh

INSTANCE ?= flux-playground
UBUNTU_RELEASE ?= 24.04
CPUS ?= 4
MEMORY ?= 8G
DISK ?= 40G
RKE2_VERSION := v1.36.4+rke2r1
RKE2_ARCH := $(if $(filter arm64 aarch64,$(shell uname -m)),arm64,$(if $(filter x86_64 amd64,$(shell uname -m)),amd64,unsupported))
RKE2_CACHE_DIR := $(CURDIR)/.cache/rke2/$(RKE2_VERSION)/$(RKE2_ARCH)

.PHONY: up stop reset ip shell status cache-rke2 install-rke2

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
