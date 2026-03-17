IMAGE_NAME ?= hisu/mitm-ai-observability
TAG        ?= latest

.PHONY: build run

build:
	docker build -f Containerfile -t $(IMAGE_NAME):$(TAG) .

run: build
	./run.sh
