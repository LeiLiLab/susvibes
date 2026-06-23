### Except the Env-agent, pipelined execution of agents has been integrated, and please refer to the curation README for convenient usage.


# Env-agent command
sweagent run-batch \
    --config=config/susvibes_env_setup.yaml \
    --agent.model.name=claude-sonnet-4-5-20250929 \
    --agent.model.per_instance_cost_limit=0.00 \
    --agent.model.per_instance_call_limit=200 \
    --instances.type=expert_file \
    --instances.path=/home/songwenzhao/susvibes/logs/agent_runs/susvibes.curate.env_setup.build_repo_instances.yaml \
    --num_workers=5

# SWE-agent command for task generation, e.g. for verification
sweagent run-batch \
    --config=config/susvibes_curate.yaml \
    --agent.model.name=claude-sonnet-4-20250514 \
    --agent.model.per_instance_cost_limit=5.00 \
    --agent.model.per_instance_call_limit=100 \
    --instances.type=expert_file \
    --instances.path=../susvibes/logs/agent_runs/susvibes.curate.adaptive_gen.verifier_instances.yaml \
    --num_workers=10

# SWE-agent command for test generation