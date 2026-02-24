.PHONY: install deploy-infra provision deprovision test status logs destroy help

help:
	@echo "DeepSeek R1 Auto-Scaling API"
	@echo ""
	@echo "Commands:"
	@echo "  make install        Install dependencies (AWS CDK, SF CLI)"
	@echo "  make deploy-infra   Deploy AWS API Gateway + Lambda stack"
	@echo "  make provision      Provision SF Compute GPU nodes + start vLLM"
	@echo "  make deprovision    Tear down SF Compute nodes"
	@echo "  make status         Check node health + API Gateway status"
	@echo "  make test           Run test request against the API"
	@echo "  make logs           Tail logs from vLLM nodes"
	@echo "  make destroy        Destroy all infrastructure (AWS + SF Compute)"

install:
	@echo "Installing dependencies..."
	pip install -r infra/aws-cdk/requirements.txt
	npm install -g aws-cdk
	@echo "Install SF CLI: curl -sSL https://sfcompute.com/install.sh | sh"
	@echo "Then run: sf auth login"

deploy-infra:
	@echo "Deploying AWS CDK stack..."
	cd infra/aws-cdk && cdk deploy --require-approval never

provision:
	@echo "Provisioning SF Compute nodes..."
	bash scripts/provision.sh

deprovision:
	@echo "Deprovisioning SF Compute nodes..."
	bash scripts/deprovision.sh

status:
	@echo "Checking system status..."
	bash scripts/health-check.sh

test:
	@echo "Running test request..."
	@API_KEY=$$(cat config/nodes.json 2>/dev/null | python3 -c "import json,sys; print(json.load(sys.stdin).get('api_key','YOUR_KEY'))") && \
	API_URL=$$(cat config/nodes.json 2>/dev/null | python3 -c "import json,sys; print(json.load(sys.stdin).get('api_gateway_url','http://localhost:8000'))") && \
	curl -s -X POST "$$API_URL/v1/chat/completions" \
	  -H "Content-Type: application/json" \
	  -H "x-api-key: $$API_KEY" \
	  -d '{"model":"deepseek-r1","messages":[{"role":"user","content":"What is 2+2? Be brief."}],"max_tokens":50}' | python3 -m json.tool

logs:
	@echo "Fetching logs from nodes..."
	@for ip in $$(cat config/nodes.json 2>/dev/null | python3 -c "import json,sys; [print(n['ip']) for n in json.load(sys.stdin).get('nodes',[])]"); do \
		echo "=== Node $$ip ==="; \
		ssh -o StrictHostKeyChecking=no ubuntu@$$ip "sudo docker logs deepseek-vllm --tail 50" 2>/dev/null; \
	done

destroy: deprovision
	@echo "Destroying AWS CDK stack..."
	cd infra/aws-cdk && cdk destroy --force
