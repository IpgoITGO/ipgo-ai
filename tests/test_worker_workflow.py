import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

WORKFLOW = Path('.github/workflows/deploy-dev.yml')


def workflow():
    return yaml.safe_load(WORKFLOW.read_text())


def gate(**overrides):
    step = workflow()['jobs']['deploy']['steps'][0]
    env = dict(os.environ, EXPECTED_SHA='a'*40, GITHUB_SHA='a'*40, DEPLOYMENT_SOURCE_SHA='a'*40,
               IPGO_LLM_EXPIRES_AT=(datetime.now(UTC)+timedelta(hours=2)).isoformat(),
               COMPUTE_MODE='onebox', MAINTENANCE_APPROVED='true', ENGINE_MODE='mock',
               REAL_CAPACITY_APPROVED='false', EMBEDDED_WORKER_ENABLED='false', LLM_ENABLED='false',
               LLM_APPROVED='false', API_DESIRED_COUNT='1', WORKER_DESIRED_COUNT='0')
    env.update(overrides)
    return subprocess.run(['bash', '-e', '-c', step['run']], env=env, capture_output=True).returncode


def test_onebox_gate():
    assert gate() == 0
    assert gate(EMBEDDED_WORKER_ENABLED='true') != 0
    real = dict(ENGINE_MODE='real', EMBEDDED_WORKER_ENABLED='true', REAL_CAPACITY_APPROVED='true')
    assert gate(**real) == 0
    for changed in ({'MAINTENANCE_APPROVED': 'false'}, {'REAL_CAPACITY_APPROVED': 'false'},
                    {'EMBEDDED_WORKER_ENABLED': 'false'}, {'API_DESIRED_COUNT': '0'},
                    {'WORKER_DESIRED_COUNT': '1'}):
        assert gate(**(real | changed)) != 0
    assert gate(LLM_ENABLED='true', LLM_APPROVED='true') != 0
    assert gate(**(real | {'LLM_ENABLED': 'true'})) != 0
    assert gate(**(real | {'LLM_ENABLED': 'true', 'LLM_APPROVED': 'true'})) == 0


def test_secret_configuration_without_real_calculation_key(tmp_path):
    steps = workflow()['jobs']['deploy']['steps']
    step = next(s for s in steps if s.get('name') == 'Configure API OpenAI secret')
    task = tmp_path / 'ai-api-task-definition.json'
    task.write_text('{"containerDefinitions":[{"name":"ai-api","secrets":[{"name":"OPENAI_API_KEY","valueFrom":"old"}]}]}')
    env = dict(os.environ, RUNNER_TEMP=str(tmp_path), ENGINE_MODE='real', LLM_ENABLED='false', OPENAI_SECRET_ARN='')
    result = subprocess.run(['bash', '-e', '-c', step['run']], env=env, capture_output=True)
    assert result.returncode == 0, result.stderr.decode()
    assert 'OPENAI_API_KEY' not in task.read_text()
    env['LLM_ENABLED'] = 'true'
    assert subprocess.run(['bash', '-e', '-c', step['run']], env=env, capture_output=True).returncode != 0


def test_llm_secret_is_opt_in_and_preserves_other_settings(tmp_path):
    steps = workflow()['jobs']['deploy']['steps']
    step = next(s for s in steps if s.get('name') == 'Configure API OpenAI secret')
    task = tmp_path / 'ai-api-task-definition.json'
    task.write_text('{"containerDefinitions":[{"name":"ai-api","memory":448,"secrets":'
                    '[{"name":"DATABASE_URL","valueFrom":"database"}]}],"taskRoleArn":"role"}')
    env = dict(os.environ, RUNNER_TEMP=str(tmp_path), ENGINE_MODE='real', LLM_ENABLED='true',
               OPENAI_SECRET_ARN='secret-arn')
    result = subprocess.run(['bash', '-e', '-c', step['run']], env=env, capture_output=True)
    assert result.returncode == 0
    import json

    definition = json.loads(task.read_text())
    assert definition['taskRoleArn'] == 'role'
    assert definition['containerDefinitions'][0]['memory'] == 448
    assert definition['containerDefinitions'][0]['secrets'] == [
        {'name': 'DATABASE_URL', 'valueFrom': 'database'},
        {'name': 'OPENAI_API_KEY', 'valueFrom': 'secret-arn'},
    ]


def test_deadline_and_exact_sha_gate():
    real = dict(ENGINE_MODE='real', EMBEDDED_WORKER_ENABLED='true', REAL_CAPACITY_APPROVED='true',
                LLM_ENABLED='true', LLM_APPROVED='true')
    assert gate(**real, IPGO_LLM_EXPIRES_AT='') != 0
    assert gate(**real, IPGO_LLM_EXPIRES_AT='2000-01-01T00:00:00Z') != 0
    assert gate(**real, IPGO_LLM_EXPIRES_AT='2026-11-06T08:30:00') != 0
    assert gate(**real, EXPECTED_SHA='b'*40) != 0


def test_continuous_requires_cost_approval_but_no_demo_deadline():
    real = dict(ENGINE_MODE='real', EMBEDDED_WORKER_ENABLED='true', REAL_CAPACITY_APPROVED='true',
                LLM_ENABLED='true', LLM_APPROVED='true', CONTINUOUS_LLM_APPROVED='true', IPGO_LLM_EXPIRES_AT='')
    assert gate(**real) == 0
    assert gate(**(real | {'LLM_APPROVED':'false'})) != 0
    assert gate(**(real | {'LLM_ENABLED':'false'})) != 0
    assert gate(**(real | {'CONTINUOUS_LLM_APPROVED':'false'})) != 0
