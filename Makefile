SHELL := /bin/bash
.DEFAULT_GOAL := help

UV ?= uv
ARGS ?=

define run_invoke
	$(UV) run invoke $(1) $(ARGS)
endef

.PHONY: help
help:
	@echo "Available targets:"
	@echo "  make up    - Create the disposable k3d cluster and Flux playground"
	@echo "  make push  - Package the working tree and reconcile the OCI artifact"
	@echo "  make check - Verify the playground is healthy"
	@echo "  make down  - Tear down the cluster, registry, and kubeconfig"
	@echo "  make reset - Tear down and recreate the playground"
	@echo "  make test  - Run the unit test suite"
	@echo ""
	@echo "Forward arguments with ARGS=\"--registry-port=5050 --timeout=600\"."

.PHONY: up push check down reset test

up:
	$(call run_invoke,playground.up)

push:
	$(call run_invoke,playground.push)

check:
	$(call run_invoke,playground.check)

down:
	$(call run_invoke,playground.down)

reset:
	$(call run_invoke,playground.reset)

test:
	$(UV) run --with invoke==3.0.3 --no-project --python 3.12 python -m unittest discover -s tests -v
