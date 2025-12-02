
# Env-agent command
sweagent run-batch \
    --config=config/agentsec_challenge.yaml \
    --agent.model.name=claude-sonnet-4-20250514 \
    --agent.model.per_instance_cost_limit=5.00 \
    --agent.model.per_instance_call_limit=150 \
    --instances.type=expert_file \
    --instances.path=/home/songwenzhao/OSS-security/logs/curate/agent_runs/install_test_instances.yaml \
    --num_workers=4

# SWE-agent command for task generation
sweagent run-batch \
    --config=config/agentsec_challenge.yaml \
    --agent.model.name=gpt-4.1 \
    --agent.model.per_instance_cost_limit=2.00 \
    --agent.model.per_instance_call_limit=100 \
    --instances.type=expert_file \
    --instances.path=/Users/songwenzhao/Desktop/Study/Projects/cmu_llm_security/OSS-security/logs/curate/agent_runs/verifier_instances.yaml \
    --num_workers=4

# SWE-agent command for evaluation
sweagent run-batch \
    --config=config/default.yaml \
    --agent.model.name=claude-sonnet-4-20250514 \
    --agent.model.per_instance_cost_limit=10.00 \
    --agent.model.per_instance_call_limit=200 \
    --instances.type=expert_file \
    --instances.path=/home/songwenzhao/OSS-security/logs/curate/agent_runs/run_evaluation_instances.yaml \
    --num_workers=5


# Single instance command
sweagent run \
    --config=config/baxbench_challenge.yaml \
    --agent.model.name=claude-3-7-sonnet-20250219 \
    --agent.model.per_instance_cost_limit=5.00 \
    --agent.model.per_instance_call_limit=50 \
    --problem_statement.text="try to run test suite" \
    --env.repo.type=preexisting \
    --env.repo.repo_name=project \
    --env.deployment.image=try_bug \
    --env.deployment.python_standalone_dir=/root