### Except the environment-building agent, pipelined execution of agents has been integrated, and please refer to the curation README for convenient usage.
### Each agent's instances.yaml + output live under logs/curate/<run_id>/<stage>/ — before_start prints the exact absolute --instances.path; copy it and pass the matching --output_dir. Paths below are relative to the SWE-agent dir (susvibes' sibling); fill in <run_id> (and <N> for adaptive_gen iterations).
### per_instance_cost_limit=0.00 means NO cost cap (the run is bounded by per_instance_call_limit instead).


# Environment-building agent command — SWE-agent (sv-env-setup)
sweagent run-batch \
    --config=config/susvibes_env_setup.yaml \
    --agent.model.name=claude-sonnet-4-5-20250929 \
    --agent.model.per_instance_cost_limit=0.00 \
    --agent.model.per_instance_call_limit=200 \
    --instances.type=expert_file \
    --instances.path=../susvibes/logs/curate/<run_id>/env_setup/build_repo/instances.yaml \
    --output_dir=../susvibes/logs/curate/<run_id>/env_setup/build_repo \
    --num_workers=5

# Task-generation agent command — SWE-agent (sv) (illustrative — adaptive_gen runs these
# in-process; each iteration's instances.yaml + output land under the iter<N> dir below)
sweagent run-batch \
    --config=config/susvibes_curate.yaml \
    --agent.model.name=claude-sonnet-4-5-20250929 \
    --agent.model.per_instance_cost_limit=0.00 \
    --agent.model.per_instance_call_limit=100 \
    --instances.type=expert_file \
    --instances.path=../susvibes/logs/curate/<run_id>/adaptive_gen/verifier/iter<N>/instances.yaml \
    --output_dir=../susvibes/logs/curate/<run_id>/adaptive_gen/verifier/iter<N> \
    --num_workers=10
