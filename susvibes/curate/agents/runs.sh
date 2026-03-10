### Except the Env-agent, pipelined execution of agents has been integrated, and please refer to the curation README for convenient usage.


# Env-agent command
sweagent run-batch \
    --config=config/susvibes_env_setup.yaml \
    --agent.model.name=claude-sonnet-4-20250514 \
    --agent.model.per_instance_cost_limit=5.00 \
    --agent.model.per_instance_call_limit=150 \
    --instances.type=expert_file \
    --instances.path=../susvibes/logs/agent_runs/susvibes.curate.env_setup.build_dataset_instances.yaml \
    --num_workers=4

# SWE-agent command for task generation, e.g. for verification
sweagent run-batch \
    --config=config/susvibes_curate.yaml \
    --agent.model.name=claude-sonnet-4-20250514 \
    --agent.model.per_instance_cost_limit=2.00 \
    --agent.model.per_instance_call_limit=100 \
    --instances.type=expert_file \
    --instances.path=../susvibes/logs/agent_runs/susvibes.curate.adaptive_gen.verifier_instances.yaml \
    --num_workers=4

# SWE-agent command for evaluation
sweagent run-batch \
    --config=config/default_susvibes.yaml \
    --agent.model.name=claude-sonnet-4-20250514 \
    --agent.model.per_instance_cost_limit=10.00 \
    --agent.model.per_instance_call_limit=200 \
    --instances.type=expert_file \
    --instances.path=../susvibes/logs/agent_runs/susvibes.curate.evaluation.run_evaluation_generic_instances.yaml \
    --num_workers=5

# SWE-agent command for debuging single instance evaluation
sweagent run \
    --config=config/default_susvibes.yaml \
    --agent.model.name=claude-3-7-sonnet-20250219 \
    --agent.model.per_instance_cost_limit=5.00 \
    --agent.model.per_instance_call_limit=50 \
    --problem_statement.text="try to run test suite" \
    --env.repo.type=preexisting \
    --env.repo.repo_name=project \
    --env.deployment.image=debug \
    --env.deployment.python_standalone_dir=/root