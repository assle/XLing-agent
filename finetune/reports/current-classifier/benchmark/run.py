"""One fixed 80+40 observation per frozen classifier; no training or retries."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import inspect
import json
import math
import os
from pathlib import Path
import random
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

sys.dont_write_bytecode = True
ROOT = Path('/Users/assle/dev/mindbridge-py')
PARENT = ROOT / '.scratch/user-simulation/20261002-022717-followup/goal-validation'
HERE = Path(__file__).resolve().parent
LABELS = ('正常', '焦虑', '低落', '高风险')
GT_MAP = {'NORMAL': '正常', 'ANX': '焦虑', 'LOW': '低落', 'HIGH': '高风险'}
BASE = PARENT / 'training-candidate/recovered-merged-base'
ADAPTER = PARENT / 'training-candidate-epochs4/training-output/adapter'
WRAPPER = PARENT / 'training-candidate-epochs4/wrapper.py'
LOCAL_SYSTEM = PARENT / 'training-candidate-v10/system-prompt.txt'
REMOTE = PARENT / 'deepseek-json-thinking-locked-qualification'
PARSER = PARENT / 'deepseek-json-thinking-validation-shadow/run-shadow.py'
METRIC = PARENT / 'generic-3b-validation-shadow/run-shadow.py'
PROTECTION = PARENT / 'epochs4-final-root-protection.json'
SETS = [
    {'name': 'mechanical-v2-80', 'path': str(ROOT / 'finetune/data/general-routing-v2/general-test.jsonl'),
     'sha256': '9e4ad6492e2cb6784b66e90a79acdbe08b37b2e8a9a58917bd58c788b0f848bd', 'cases': 80,
     'exposure': 'mechanical current routing-v2 test; not a new blind test'},
    {'name': 'natural-v12-r2-40', 'path': str(PARENT / 'prospective-acceptance-v12-r2/acceptance-v12.jsonl'),
     'sha256': 'e0b78f203c5c9e9592422ff22d9bc9660f3848c9302b1bc8b083926c84c04f04', 'cases': 40,
     'exposure': 'first fixed independent model test; engineering synthetic targets'},
]


def now():
    return datetime.now(timezone.utc).isoformat()


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def write(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


def module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def rows(dataset):
    assert sha(dataset['path']) == dataset['sha256'], 'Dataset fingerprint changed'
    result = [json.loads(line) for line in Path(dataset['path']).read_text().splitlines() if line.strip()]
    assert len(result) == dataset['cases'] and len({row['id'] for row in result}) == len(result)
    for row in result:
        row['expectedSource'] = row['output']
        row['output'] = GT_MAP.get(row['output'], row['output'])
        assert row['output'] in LABELS
    return result


def old_protection():
    assert sha(PROTECTION) == 'd7e78e6916ccfee918bb1003267d7587f220d625bfe80c938eeebc59d9c23274'
    entries = json.loads(PROTECTION.read_text())['files']
    assert len(entries) == 197
    for item in entries:
        assert sha(item['path']) == item['sha256'], 'Old protected source changed: ' + item['path']
    return entries


def load_trainer():
    wrapper = module(WRAPPER, 'bounded_frozen_wrapper')
    trainer = wrapper.load_trainer()
    trainer.SYSTEM_PROMPT = LOCAL_SYSTEM.read_text().strip()
    return trainer, wrapper


def prepare():
    assert not (HERE / 'freeze-manifest.json').exists(), 'Freeze already exists'
    old = old_protection()
    protocol = json.loads((REMOTE / 'protocol-skeleton.json').read_text())
    assert sha(REMOTE / 'protocol-skeleton.json') == '1103eb2c1309223a3e40854ef7c29c09bc19b401dc5f52f6014f179d6b09b555'
    assert sha(PARSER) == protocol['parserSource']['sha256']
    assert sha(REMOTE / 'system-prompt.txt') == protocol['systemPromptSHA256']
    assert sha(LOCAL_SYSTEM) == 'a6d981d29f3cfe7f4edef11bb293a5c5ddf06a87d6205177746b5c4454d346ab'
    assert sha(ADAPTER / 'adapter_model.safetensors') == '39312e62bae612295d46153242b6159e15e0b5196ba4e3906df779e87f770efd'
    trainer, wrapper = load_trainer()
    assert trainer.device_name() == 'mps', 'MPS unavailable'
    tokenizer = trainer.AutoTokenizer.from_pretrained(str(BASE), trust_remote_code=True, local_files_only=True,
                                                      cache_dir=ROOT / 'data/huggingface')
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
    assert tokenizer.eos_token_id == 151645 and tokenizer.pad_token_id == 151643
    datasets = []
    for dataset in SETS:
        collection = rows(dataset)
        lengths = [len(tokenizer(trainer.prompt_text(tokenizer, row['input']), add_special_tokens=False)['input_ids'])
                   for row in collection]
        datasets.append({**dataset, 'labelCounts': dict(Counter(row['output'] for row in collection)),
                         'sourceLabelCounts': dict(Counter(row['expectedSource'] for row in collection)),
                         'orderedIDs': [row['id'] for row in collection],
                         'maxUntruncatedInputTokens': max(lengths), 'inputsExceeding256': sum(n > 256 for n in lengths)})
    sys.path.insert(0, str(ROOT))
    from app.core.config import Settings
    import httpx
    settings = Settings()
    config_matches = (settings.ai_provider == 'openai' and settings.openai_model == protocol['configuredModel']
                      and settings.openai_base_url == protocol['configuredBaseURL'] and settings.ai_max_tokens == 2048
                      and bool(settings.openai_api_key))
    sources = [Path(__file__).resolve(), WRAPPER, ROOT / 'finetune/scripts/train_general_classifier.py', LOCAL_SYSTEM,
               REMOTE / 'run.py', REMOTE / 'protocol-skeleton.json', REMOTE / 'system-prompt.txt', PARSER, METRIC,
               PROTECTION, ROOT / '.env', ROOT / 'app/services/ai.py', ROOT / 'app/core/config.py',
               PARENT / 'versioned-source-materialization.json',
               PARENT / 'prospective-acceptance-v12-r2-independent-full-audit.json']
    for directory in (BASE, ADAPTER, PARENT / 'training-candidate-epochs4', REMOTE):
        sources.extend(p for p in directory.iterdir() if p.is_file())
    sources.extend(Path(d['path']) for d in SETS)
    fingerprints = [{'path': str(p), 'sha256': sha(p), 'bytes': p.stat().st_size} for p in sorted(set(sources))]
    manifest = {
        'version': 'bounded-fixed-classifiers-80-plus-40-1', 'frozenAtUTC': now(),
        'authorizedUniqueExecution': True, 'scope': 'one pass per scheme on the two fixed datasets; observational benchmark',
        'datasets': datasets, 'GTMapping': GT_MAP, 'sourceGroundTruthChanges': 0,
        'budget': {'A': 120, 'B': 120, 'totalModelAttemptsCap': 240, 'retryCount': 0, 'optimizerUpdates': 0},
        'ordering': 'original source row order, mechanical-v2-80 then natural-v12-r2-40; B fixed pairs, concurrency 2',
        'A': {'name': 'local-epochs4-selected', 'base': str(BASE), 'adapter': str(ADAPTER),
              'adapterSHA256': sha(ADAPTER / 'adapter_model.safetensors'), 'selectedCheckpointBatch': 752,
              'system': str(LOCAL_SYSTEM), 'systemSHA256': sha(LOCAL_SYSTEM), 'wrapper': str(WRAPPER),
              'userContent': 'PREFIX + json.dumps(text, ensure_ascii=False)', 'PREFIX': wrapper.PREFIX,
              'device': 'mps', 'baseDtype': 'float16', 'PEFTAdapterDtypePolicy': 'default, prior diagnostic policy',
              'isTrainable': False, 'useCache': False, 'seed': 42, 'doSample': False,
              'temperature': None, 'topP': None, 'topK': None, 'repetitionPenalty': 1.0,
              'maxNewTokens': 6, 'maxInputTokens': 256, 'eosTokenID': 151645, 'padTokenID': 151643,
              'parseContract': 'decoded generated suffix strip exact Chinese enum; no fallback',
              'metricsFunction': 'existing frozen trainer.metrics'},
        'B': {'name': 'DeepSeek-JSON-thinking-locked-reference', 'protocol': str(REMOTE / 'protocol-skeleton.json'),
              'system': str(REMOTE / 'system-prompt.txt'), 'systemSHA256': protocol['systemPromptSHA256'],
              'configuredModel': protocol['configuredModel'], 'configuredBaseURL': protocol['configuredBaseURL'],
              'endpoint': protocol['endpoint'], 'requestPayload': protocol['requestPayload'],
              'parseContract': protocol['parseContract'], 'parser': protocol['parserSource'],
              'timeoutSeconds': 120, 'trustEnv': False, 'followRedirects': False, 'HTTPTransportRetries': 0,
              'maximumConcurrency': 2, 'httpxVersion': httpx.__version__,
              'currentSettingsMatchLockedProfile': config_matches, 'remoteIdentityLimit': protocol['remoteIdentityLimit'],
              'metricsFunction': 'existing frozen generic helper.metric'},
        'source197Protection': {'path': str(PROTECTION), 'sha256': sha(PROTECTION), 'count': 197,
                                'beforeAllMatch': True, 'files': old},
        'fingerprints': fingerprints,
        'oldStrictValidation': {'decision': 'FAIL', 'observedCorrect': 125, 'cases': 128,
                                'falseHIGH': 1, 'unchanged': True, 'newValidationCalls': 0},
        'testResultsUsedForSelection': False, 'natural40ModelInferencesBeforeThisBenchmark': 0,
        'rawInvalidOutputsOrPrivateThinkingPersisted': False,
        'stop': 'Any environment/auth/unsupported-mode/API/runtime/integrity block stops that scheme with no retry or alternate request.',
    }
    write(HERE / 'freeze-manifest.json', manifest)
    (HERE / 'freeze-manifest.json').chmod(0o444)
    print(json.dumps({'event': 'frozen', 'freezeSHA256': sha(HERE / 'freeze-manifest.json'),
                      'sets': [{'name': d['name'], 'sha256': d['sha256'], 'cases': d['cases'],
                                'maxInputTokens': d['maxUntruncatedInputTokens']} for d in datasets],
                      'AAdapterSHA256': manifest['A']['adapterSHA256'], 'BSettingsMatch': config_matches,
                      'totalCap': 240, 'retryCount': 0}), flush=True)


def verify(frozen):
    for item in frozen['fingerprints'] + frozen['source197Protection']['files']:
        assert sha(item['path']) == item['sha256'], 'Frozen fingerprint changed: ' + item['path']


def percentile(values, fraction):
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lo, hi = math.floor(position), math.ceil(position)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (position - lo)


def run(scheme):
    progress = HERE / (scheme + '-progress.json')
    assert not progress.exists(), 'Scheme already attempted; no rerun'
    frozen = json.loads((HERE / 'freeze-manifest.json').read_text())
    state = {'scheme': scheme, 'PID': os.getpid(), 'startedAtUTC': now(), 'status': 'running',
             'modelAttempts': 0, 'logicalReserved': 0, 'responsesCompleted': 0, 'modelAttemptsCap': 120,
             'retryCount': 0, 'phase': 'setup', 'freezeSHA256': sha(HERE / 'freeze-manifest.json')}
    records = []
    collections = {}
    lock = threading.Lock()
    exit_code = 0
    model = base = tokenizer = None
    original_post = None
    write(progress, state)
    print(json.dumps({'event': 'started', 'scheme': scheme, 'PID': os.getpid(), 'progress': str(progress)}), flush=True)

    def save():
        write(HERE / (scheme + '-case-results.json'), records)
        write(progress, state)

    def reserve(detail, attempted=False):
        with lock:
            assert state['logicalReserved'] < 120
            state['logicalReserved'] += 1
            detail.update({'status': 'reserved', 'reservedAtUTC': now()})
            if attempted:
                state['modelAttempts'] += 1
                detail.update({'status': 'attempted', 'attemptOrdinal': state['modelAttempts'], 'attemptedAtUTC': now()})
            save()

    def completed(detail, predicted, started, **info):
        assert predicted in LABELS + ('__INVALID__',)
        with lock:
            detail.update({'status': 'completed', 'predicted': predicted, 'strictValid': predicted in LABELS,
                           'latencyMs': (time.perf_counter() - started) * 1000, 'completedAtUTC': now(), **info})
            state['responsesCompleted'] += 1
            save()

    try:
        verify(frozen)
        for dataset in frozen['datasets']:
            collection = rows(dataset)
            collections[dataset['name']] = collection
            for row in collection:
                records.append({'set': dataset['name'], 'id': row['id'], 'expectedSource': row['expectedSource'],
                                'expected': row['output'], 'inputSHA256': hashlib.sha256(row['input'].encode()).hexdigest(),
                                'sourceSHA256': dataset['sha256'], 'status': 'notrun', 'predicted': None,
                                'strictValid': None, 'finishReason': None, 'tokenCount': None, 'latencyMs': None,
                                'providerModel': None})
        save()
        if scheme == 'A':
            assert os.environ.get('HF_HUB_OFFLINE') == os.environ.get('TRANSFORMERS_OFFLINE') == '1'
            trainer, wrapper = load_trainer()
            import torch
            from peft import PeftModel
            assert trainer.device_name() == 'mps'
            random.seed(42)
            torch.manual_seed(42)
            tokenizer = trainer.AutoTokenizer.from_pretrained(str(BASE), trust_remote_code=True,
                                                               local_files_only=True, cache_dir=ROOT / 'data/huggingface')
            if tokenizer.pad_token_id is None:
                tokenizer.pad_token = tokenizer.eos_token
            assert tokenizer.eos_token_id == 151645 and tokenizer.pad_token_id == 151643
            base = trainer.AutoModelForCausalLM.from_pretrained(str(BASE), trust_remote_code=True,
                        torch_dtype=torch.float16, low_cpu_mem_usage=True, local_files_only=True,
                        cache_dir=ROOT / 'data/huggingface').to('mps')
            base.config.use_cache = False
            model = PeftModel.from_pretrained(base, str(ADAPTER), is_trainable=False, local_files_only=True)
            model.eval()
            assert not any(parameter.requires_grad for parameter in model.parameters())
            for detail in records:
                state['phase'] = detail['set']
                row = next(r for r in collections[detail['set']] if r['id'] == detail['id'])
                encoded = tokenizer(trainer.prompt_text(tokenizer, row['input']), add_special_tokens=False,
                                    truncation=True, max_length=256, return_tensors='pt')
                inputs, mask = encoded['input_ids'].to('mps'), encoded['attention_mask'].to('mps')
                reserve(detail, attempted=True)
                started = time.perf_counter()
                with torch.inference_mode():
                    generated = model.generate(input_ids=inputs, attention_mask=mask, do_sample=False,
                                temperature=None, top_p=None, top_k=None, max_new_tokens=6,
                                pad_token_id=151643, eos_token_id=151645, repetition_penalty=1.0)
                torch.mps.synchronize()
                suffix = generated[0, inputs.shape[1]:]
                raw = tokenizer.decode(suffix, skip_special_tokens=True).strip()
                predicted = raw if raw in LABELS else '__INVALID__'
                completed(detail, predicted, started, finishReason='eos' if suffix[-1].item() == 151645 else 'length',
                          tokenCount=int(suffix.shape[0]), promptTokenCount=int(inputs.shape[1]),
                          providerModel='local-epochs4-selected:39312e62bae612295d46153242b6159e15e0b5196ba4e3906df779e87f770efd')
                if state['responsesCompleted'] % 20 == 0:
                    print(json.dumps({'event': 'progress', 'scheme': scheme, 'completed': state['responsesCompleted'], 'cap': 120}), flush=True)
        else:
            sys.path.insert(0, str(ROOT))
            import httpx
            from app.core.config import Settings
            from app.schemas.dtos import AiMessage
            from app.services.ai import AiClient
            settings = Settings()
            profile = frozen['B']
            assert settings.ai_provider == 'openai' and settings.openai_model == profile['configuredModel']
            assert settings.openai_base_url == profile['configuredBaseURL'] and settings.ai_max_tokens == 2048 and settings.openai_api_key
            assert httpx.__version__ == profile['httpxVersion']
            assert inspect.signature(httpx.post).parameters['follow_redirects'].default is False
            assert inspect.signature(httpx.HTTPTransport).parameters['retries'].default == 0
            settings = settings.model_copy(update={'ai_temperature': 0.0})
            for name in ('HTTP_PROXY', 'http_proxy', 'HTTPS_PROXY', 'https_proxy', 'ALL_PROXY', 'all_proxy'):
                os.environ[name] = ''
            os.environ['NO_PROXY'] = os.environ['no_proxy'] = '*'
            parser = module(PARSER, 'bounded_frozen_remote_codec')
            system = (REMOTE / 'system-prompt.txt').read_text()
            original_post = httpx.post
            thread_info = threading.local()

            def observed_post(url, **kwargs):
                payload = kwargs['json']
                row, detail = thread_info.row, thread_info.detail
                assert str(url) == profile['endpoint']
                assert set(payload) == {'model', 'messages', 'temperature', 'max_tokens', 'stream'}
                assert payload['model'] == profile['configuredModel'] and payload['temperature'] == 0.0
                assert payload['max_tokens'] == 2048 and payload['stream'] is False and kwargs['timeout'] == 120
                assert payload['messages'] == [{'role': 'system', 'content': system}, {'role': 'user', 'content': row['input']}]
                payload['response_format'] = {'type': 'json_object'}
                payload['thinking'] = {'type': 'enabled'}
                with lock:
                    assert state['modelAttempts'] < 120
                    state['modelAttempts'] += 1
                    detail.update({'status': 'attempted', 'attemptOrdinal': state['modelAttempts'], 'attemptedAtUTC': now()})
                    save()
                response = original_post(url, **kwargs, trust_env=False)
                thread_info.httpStatus = response.status_code
                if response.status_code < 400:
                    data = response.json()
                    usage = data.get('usage', {})
                    thread_info.info = {'providerModel': data.get('model'),
                        'finishReason': data.get('choices', [{}])[0].get('finish_reason'),
                        'tokenCount': usage.get('completion_tokens'), 'promptTokenCount': usage.get('prompt_tokens'),
                        'usage': {k: v for k, v in usage.items() if isinstance(v, (int, float)) and not isinstance(v, bool)},
                        'httpStatus': response.status_code}
                return response

            httpx.post = observed_post
            client = AiClient(settings)

            def classify(detail):
                row = next(r for r in collections[detail['set']] if r['id'] == detail['id'])
                thread_info.row, thread_info.detail = row, detail
                thread_info.info, thread_info.httpStatus = {}, None
                reserve(detail)
                started = time.perf_counter()
                try:
                    raw = client.complete([AiMessage(role='system', content=system), AiMessage(role='user', content=row['input'])])
                    if not isinstance(raw, str):
                        raise RuntimeError('Non-string provider response')
                    predicted = parser.parse_label(raw, thread_info.info.get('finishReason'))
                    completed(detail, predicted, started, **thread_info.info)
                except Exception as error:
                    with lock:
                        detail.update({'status': 'failed', 'errorType': type(error).__name__,
                                       'httpStatus': thread_info.httpStatus,
                                       'latencyMs': (time.perf_counter() - started) * 1000})
                        save()
                    raise

            with ThreadPoolExecutor(max_workers=2) as executor:
                for dataset in frozen['datasets']:
                    state['phase'] = dataset['name']
                    group = [d for d in records if d['set'] == dataset['name']]
                    for index in range(0, len(group), 2):
                        futures = [executor.submit(classify, detail) for detail in group[index:index + 2]]
                        failures = []
                        for future in futures:
                            try:
                                future.result()
                            except Exception as error:
                                failures.append(error)
                        if failures:
                            raise failures[0]
                        if state['responsesCompleted'] % 10 == 0:
                            print(json.dumps({'event': 'progress', 'scheme': scheme, 'set': dataset['name'],
                                              'completed': state['responsesCompleted'], 'attempts': state['modelAttempts'], 'cap': 120}), flush=True)
        assert state['modelAttempts'] == state['responsesCompleted'] == 120
        state['status'] = 'completed'
    except Exception as error:
        exit_code = 1
        if scheme == 'A':
            for detail in records:
                if detail['status'] in ('attempted', 'reserved'):
                    detail.update({'status': 'failed', 'errorType': type(error).__name__})
        state.update({'status': 'blocked', 'errorType': type(error).__name__, 'decision': 'stop-scheme-no-retry'})
        print(json.dumps({'event': 'stopped', 'scheme': scheme, 'errorType': type(error).__name__,
                          'attempts': state['modelAttempts'], 'completed': state['responsesCompleted']}), flush=True)
    finally:
        if original_post is not None:
            import httpx
            httpx.post = original_post
        if model is not None:
            del model, base, tokenizer
            import torch
            torch.mps.empty_cache()
        try:
            verify(frozen)
            state['allFrozenSourcesAndOld197StillMatch'] = True
        except Exception as error:
            exit_code = 1
            state.update({'status': 'blocked', 'errorType': type(error).__name__, 'allFrozenSourcesAndOld197StillMatch': False})
        state.update({'endedAtUTC': now(), 'processExitCode': exit_code,
                      'notRunCases': sum(d['status'] == 'notrun' for d in records),
                      'failedCases': sum(d['status'] == 'failed' for d in records),
                      'resourcesReleased': True})
        save()
        helper = module(METRIC, 'bounded_frozen_metrics') if scheme == 'B' else load_trainer()[0]
        set_results = {}
        for dataset in frozen['datasets']:
            details = [d for d in records if d['set'] == dataset['name'] and d['status'] == 'completed']
            complete = len(details) == dataset['cases']
            measures = None
            if complete:
                measures = (helper.metric(collections[dataset['name']], details) if scheme == 'B'
                            else helper.metrics(collections[dataset['name']], [d['predicted'] for d in details]))
                measures['correct'] = sum(d['predicted'] == d['expected'] for d in details)
                measures['falseHighRiskCount'] = sum(d['predicted'] == '高风险' and d['expected'] != '高风险' for d in details)
                measures['latencyMsP50'] = percentile([d['latencyMs'] for d in details], 0.50)
                measures['latencyMsP95'] = percentile([d['latencyMs'] for d in details], 0.95)
            set_results[dataset['name']] = {'status': 'completed' if complete else 'not-completed',
                'cases': dataset['cases'], 'completed': len(details), 'measurements': measures,
                'sourceSHA256': dataset['sha256'], 'exposure': dataset['exposure']}
        result = {'version': 'bounded-classifier-safe-result-1', **state, 'sets': set_results,
                  'configuredModel': frozen[scheme].get('configuredModel', frozen[scheme]['name']),
                  'providerModelDistribution': dict(Counter(d['providerModel'] for d in records if d['status'] == 'completed')),
                  'usageTotals': {key: sum(d.get('usage', {}).get(key, 0) for d in records)
                                  for key in sorted({key for d in records for key in d.get('usage', {})})},
                  'oldStrictFAILPreserved': state.get('allFrozenSourcesAndOld197StillMatch', False),
                  'GTChanged': False, 'privateThinkingOrRawInvalidOutputsPersisted': False,
                  'testScoresUsedForSelection': False}
        write(HERE / (scheme + '-result.json'), result)
        print(json.dumps({'event': 'terminal', 'scheme': scheme, 'PID': os.getpid(), 'status': state['status'],
                          'attempts': state['modelAttempts'], 'completed': state['responsesCompleted'],
                          'exitCode': exit_code, 'protected197Match': state.get('allFrozenSourcesAndOld197StillMatch', False)}), flush=True)
    return exit_code


if __name__ == '__main__':
    arguments = argparse.ArgumentParser()
    arguments.add_argument('--prepare', action='store_true')
    arguments.add_argument('--scheme', choices=('A', 'B'))
    options = arguments.parse_args()
    if options.prepare:
        prepare()
    else:
        assert options.scheme
        sys.exit(run(options.scheme))
