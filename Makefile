.DEFAULT_GOAL := help
DC := docker compose

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "};{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

up: ## Build and start the whole stack
	$(DC) up -d --build
	@echo ""
	@echo "  Grafana     http://localhost:3000   (anonymous viewer, or admin/admin)"
	@echo "  Prometheus  http://localhost:9090"
	@echo "  API         http://localhost:8000/health"
	@echo "  Detector    http://localhost:9110/metrics"

down: ## Stop the stack (keeps data)
	$(DC) down

clean: ## Stop and delete all volumes
	$(DC) down -v

logs: ## Tail the detector
	$(DC) logs -f detector

ps: ## Show service status
	$(DC) ps

experiment: ## Run the standard fault scenario (~38 min)
	python3 experiments/run_experiment.py --api http://localhost:8000 --scenario standard

smoke: ## Run a short fault scenario (~9 min)
	python3 experiments/run_experiment.py --api http://localhost:8000 --scenario smoke

hard: ## Run the subtle/low-magnitude scenario (~26 min)
	python3 experiments/run_experiment.py --api http://localhost:8000 --scenario hard

evaluate: ## Score the detector against injected ground truth
	$(DC) exec -T detector python evaluate.py --data /data

report: ## Emit the evaluation as markdown
	@$(DC) exec -T detector python evaluate.py --data /data --markdown

reset-data: ## Clear ground truth and alert history
	$(DC) exec -T detector sh -c 'rm -f /data/*.jsonl' && echo "cleared"

fault: ## Inject one fault: make fault KIND=latency_spike MAG=1.5 SECS=120
	@curl -sS -X POST localhost:8000/admin/faults -H 'Content-Type: application/json' \
	  -d '{"kind":"$(or $(KIND),latency_spike)","magnitude":$(or $(MAG),1.5),"duration_s":$(or $(SECS),120)}' | python3 -m json.tool

clear-faults: ## Clear all active faults
	@curl -sS -X DELETE localhost:8000/admin/faults | python3 -m json.tool

.PHONY: help up down clean logs ps experiment smoke hard evaluate report reset-data fault clear-faults
