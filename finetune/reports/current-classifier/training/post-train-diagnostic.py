"""Pre-registered full-train diagnostic for the epochs4 selected adapter only."""
from pathlib import Path
from collections import Counter
import argparse
import hashlib
import importlib.util
import json
import os
import random
import subprocess
import sys
import time

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
ROOT = Path('/Users/assle/dev/mindbridge-py')
PARENT = HERE.parent
CANDIDATE = HERE
FREEZE = CANDIDATE / 'freeze.json'
BASE = PARENT / 'training-candidate/recovered-merged-base'
ADAPTER = CANDIDATE / 'training-output/adapter'
TRAIN = PARENT / 'training-candidate-postposed-negation/data/train.jsonl'
SYSTEM = PARENT / 'training-candidate-v10/system-prompt.txt'
WRAPPER = CANDIDATE / 'wrapper.py'
TRAINER = ROOT / 'finetune/scripts/train_general_classifier.py'
LABELS = ('正常', '焦虑', '低落', '高风险')
BASELINE_RESULT = PARENT / 'training-candidate-postposed-negation-full-train-diagnostic/result.json'
EXPECTED_BASELINE_RESULT = 'ff86bb6a28a5ca866bc1d968f536d5a3daab18f201e5e91393ec478161e76269'

EXPECTED_TRAIN = 'a1984a75d366d9d5d678cea7c1e9a27ba52308861fea46eedfed4035f3954a1e'
EXPECTED_SYSTEM = 'a6d981d29f3cfe7f4edef11bb293a5c5ddf06a87d6205177746b5c4454d346ab'


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def verify_sources():
    frozen = json.loads(FREEZE.read_text())
    cache = {}
    for item in frozen['fingerprints']:
        path = item['path']
        if path not in cache:
            cache[path] = sha(path)
        if cache[path] != item['sha256']:
            raise RuntimeError('Frozen training/protection fingerprint changed: ' + path)
    terminal = json.loads((HERE / 'main-terminal.json').read_text())
    if terminal['status'] != 'completed':
        raise RuntimeError('Normal main training terminal required')
    try:
        os.kill(terminal['pid'], 0)
        raise RuntimeError('Main training process must have exited')
    except ProcessLookupError:
        pass
    training_result = json.loads((HERE / 'training-result.json').read_text())
    training = training_result['training']
    if training_result['trainCases'] != 376 or training_result['validationCases'] != 128 or training_result['testCases'] != 0 or training_result['testDatasetRead'] is not False or training['updates'] != 188 or [x['batch'] for x in training['validationHistory']] != [376, 752, 1128, 1504] or training_result['config']['epochs'] != 4.0 or not training_result['passed']:
        raise RuntimeError('Main terminal shape is outside pre-registered epochs4 contract')
    if sha(BASELINE_RESULT) != EXPECTED_BASELINE_RESULT or sha(TRAIN) != EXPECTED_TRAIN:
        raise RuntimeError('Pre-registered baseline or training data changed')
    if hashlib.sha256(SYSTEM.read_text(encoding='utf-8').strip().encode('utf-8')).hexdigest() != EXPECTED_SYSTEM:
        raise RuntimeError('Fixed system prompt changed')
    return {'trainingFreezeSHA256': sha(FREEZE), 'allFrozenSourceAndOldProtectionChecksPassed': True, 'criticalFingerprintCount': len(frozen['fingerprints']), 'adapterSHA256': sha(ADAPTER / 'adapter_model.safetensors'), 'trainSHA256': EXPECTED_TRAIN, 'systemPromptSHA256': EXPECTED_SYSTEM, 'protectedFilesReadAsHashesOnly': True, 'forbiddenBodiesParsed': False, 'registeredTrainDiagnosticRunsEvenIfStrictValFAIL': True}


def ports():
    states = {}
    for port in (8081, 8026, 11434, 11435):
        observed = subprocess.run(['/usr/sbin/lsof', '-nP', '-iTCP:' + str(port), '-sTCP:LISTEN', '-t'], capture_output=True, text=True)
        if observed.returncode not in (0, 1):
            raise RuntimeError('Port observation unavailable')
        states[str(port)] = {'listening': bool(observed.stdout.strip()), 'pids': sorted(set(observed.stdout.split()))}
    if any(states[str(port)]['listening'] for port in (8081, 8026, 11434)):
        raise RuntimeError('An owned test port is unexpectedly listening')
    return states


def rows_and_ids():
    if sha(TRAIN) != EXPECTED_TRAIN:
        raise RuntimeError('Frozen train376 hash changed')
    rows = [json.loads(line) for line in TRAIN.read_text(encoding='utf-8').splitlines() if line.strip()]
    if len(rows) != 376 or Counter(row['output'] for row in rows) != Counter({label: 94 for label in LABELS}):
        raise RuntimeError('Unexpected complete train shape')
    if len(set(row['id'] for row in rows)) != 376:
        raise RuntimeError('Duplicate train IDs')
    ids = [row['id'] for row in rows]
    digest = hashlib.sha256(json.dumps(ids, ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()
    return rows, digest


def load_trainer():
    spec = importlib.util.spec_from_file_location('frozen_candidate_wrapper', WRAPPER)
    wrapper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wrapper)
    trainer = wrapper.load_trainer()
    trainer.SYSTEM_PROMPT = SYSTEM.read_text(encoding='utf-8').strip()
    return trainer, wrapper


def prepare():
    if (HERE / 'train-diagnostic-manifest.json').exists():
        raise RuntimeError('Manifest already frozen')
    protection = verify_sources()
    port_states = ports()
    rows, ids_sha = rows_and_ids()
    trainer, wrapper = load_trainer()
    if trainer.device_name() != 'mps':
        raise RuntimeError('MPS required')
    tokenizer = trainer.AutoTokenizer.from_pretrained(str(BASE), trust_remote_code=True, local_files_only=True, cache_dir=ROOT / 'data/huggingface')
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
    lengths = []
    for row in rows:
        prompt = trainer.prompt_text(tokenizer, row['input'])
        prefix_ids = tokenizer(prompt, add_special_tokens=False)['input_ids']
        suffix_ids = tokenizer(row['output'] + tokenizer.eos_token, add_special_tokens=False)['input_ids']
        full_ids = tokenizer(prompt + row['output'] + tokenizer.eos_token, add_special_tokens=False)['input_ids']
        if full_ids != prefix_ids + suffix_ids or suffix_ids[-1] != tokenizer.eos_token_id or len(full_ids) > 256:
            raise RuntimeError('Complete train prefix/supervision length mismatch')
        lengths.append(len(full_ids))
    files = [Path(__file__).resolve(), FREEZE, WRAPPER, TRAINER, SYSTEM, TRAIN, ADAPTER / 'adapter_model.safetensors', ADAPTER / 'adapter_config.json']
    manifest = {'experiment': 'fixed-checkpoint-full-train376-greedy-diagnostic', 'candidate': str(CANDIDATE), 'base': str(BASE), 'adapter': str(ADAPTER), 'candidateFreezeSHA256': sha(FREEZE), 'adapterSHA256': sha(ADAPTER / 'adapter_model.safetensors'), 'train': {'path': str(TRAIN), 'sha256': EXPECTED_TRAIN, 'cases': 376, 'classCounts': dict(Counter(row['output'] for row in rows)), 'order': 'Original JSONL source-row order; no selection or shuffling.', 'ids': [row['id'] for row in rows], 'orderedIDsSHA256': ids_sha, 'orderedIDsSHA256Encoding': 'UTF-8 json.dumps(ids, ensure_ascii=False, separators=(comma, colon)); no trailing newline.'}, 'generation': {'device': 'mps', 'baseDtype': 'float16', 'adapterDtypePolicy': 'PEFT default, same as existing trainer', 'doSample': False, 'temperature': None, 'topP': None, 'topK': None, 'builtinRepetitionPenalty': 1.0, 'customProcessor': None, 'maxNewTokens': 6, 'maxInputTokens': 256, 'padTokenID': tokenizer.pad_token_id, 'eosTokenID': tokenizer.eos_token_id, 'useCache': False, 'userPrefix': wrapper.PREFIX, 'userContent': 'PREFIX + json.dumps(text, ensure_ascii=False)', 'samePromptFunctionAndSettingsAsPriorDiagnosticBaseline': True}, 'budget': {'modelLoads': 1, 'optimizerUpdates': 0, 'logicalHFGeneratedLabelsCap': 376, 'generationCallsCap': 376, 'maxGeneratedTokensCap': 2256}, 'execution': 'One model load, model.eval/inference_mode, source-order one pass; no optimizer or retries.', 'stopConditions': ['Any setup/source/protection inconsistency stops.', 'Any runtime failure stops without retry.', 'Owned ports unexpectedly listening stops.'], 'protectionBefore': protection, 'portsBefore': port_states, 'tokenizerOnlyPreflight': {'rows': 376, 'completePrefixAndLabelEOS': True, 'maxFullTokens': max(lengths), 'modelLoaded': False, 'generatedLabels': 0}, 'fingerprints': [{'path': str(path), 'bytes': path.stat().st_size, 'sha256': sha(path)} for path in files], 'interpretation': 'Original train GT scores only; no GT review, rescore or deletion. Original epoch3 strict-val FAIL remains unchanged; epochs4 strict validation is computed separately from its actual selected adapter; product-reference thresholds are distinct.', 'forbidden': ['training', 'val/test/acceptance/probe parsing or inference', 'new data', 'parameter/epoch/likelihood follow-up', 'case retry', 'network/API', 'old artifact mutation', 'export/register/deploy/app change']}
    (HERE / 'train-diagnostic-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    Path(__file__).chmod(0o444)
    (HERE / 'train-diagnostic-manifest.json').chmod(0o444)
    print(json.dumps({'status': 'FROZEN', 'manifestSHA256': sha(HERE / 'train-diagnostic-manifest.json'), 'scriptSHA256': sha(__file__), 'orderedTrainIDsSHA256': ids_sha, 'cases': 376, 'classCounts': manifest['train']['classCounts'], 'protectionBefore': protection, 'tokenizerOnlyPreflight': manifest['tokenizerOnlyPreflight'], 'modelLoads': 0, 'generatedLabels': 0}, ensure_ascii=False, indent=2))


def verify_manifest():
    manifest = json.loads((HERE / 'train-diagnostic-manifest.json').read_text())
    for item in manifest['fingerprints']:
        if sha(item['path']) != item['sha256']:
            raise RuntimeError('Diagnostic fingerprint changed: ' + item['path'])
    rows, ids_sha = rows_and_ids()
    if ids_sha != manifest['train']['orderedIDsSHA256'] or [row['id'] for row in rows] != manifest['train']['ids']:
        raise RuntimeError('Frozen source order changed')
    return manifest, rows


def run():
    if (HERE / 'train-diagnostic-start.json').exists() or (HERE / 'train-diagnostic-result.json').exists():
        raise RuntimeError('Already attempted; retries prohibited')
    for key in ('HF_HUB_OFFLINE', 'TRANSFORMERS_OFFLINE'):
        if os.environ.get(key) != '1':
            raise RuntimeError('Offline environment required')
    manifest, rows = verify_manifest()
    protection = verify_sources()
    port_states = ports()
    start = {'pid': os.getpid(), 'status': 'running', 'startedUnix': time.time(), 'manifestSHA256': sha(HERE / 'train-diagnostic-manifest.json'), 'logicalLabelsCap': 376, 'optimizerUpdates': 0}
    (HERE / 'train-diagnostic-start.json').write_text(json.dumps(start, indent=2) + '\n')
    print(json.dumps({'START': start}), flush=True)
    reserved = 0
    completed = 0
    details = []
    try:
        import torch
        from peft import PeftModel
        trainer, wrapper = load_trainer()
        if trainer.device_name() != 'mps':
            raise RuntimeError('MPS required')
        random.seed(42)
        torch.manual_seed(42)
        tokenizer = trainer.AutoTokenizer.from_pretrained(str(BASE), trust_remote_code=True, local_files_only=True, cache_dir=ROOT / 'data/huggingface')
        if tokenizer.pad_token_id is None:
            tokenizer.pad_token = tokenizer.eos_token
        if tokenizer.pad_token_id != manifest['generation']['padTokenID'] or tokenizer.eos_token_id != 151645 or tokenizer.eos_token_id != manifest['generation']['eosTokenID']:
            raise RuntimeError('Current special token IDs changed')
        base = trainer.AutoModelForCausalLM.from_pretrained(str(BASE), trust_remote_code=True, torch_dtype=torch.float16, low_cpu_mem_usage=True, local_files_only=True, cache_dir=ROOT / 'data/huggingface').to('mps')
        base.config.use_cache = False
        model = PeftModel.from_pretrained(base, str(ADAPTER), is_trainable=False, local_files_only=True)
        model.requires_grad_(False)
        model.eval()
        if any(parameter.requires_grad for parameter in model.parameters()):
            raise RuntimeError('Inference-only parameters required')
        for row in rows:
            if reserved >= 376:
                raise RuntimeError('Label budget exceeded')
            prompt = trainer.prompt_text(tokenizer, row['input'])
            encoded = tokenizer(prompt, add_special_tokens=False, truncation=True, max_length=256, return_tensors='pt')
            inputs = encoded['input_ids'].to('mps')
            mask = encoded['attention_mask'].to('mps')
            reserved += 1
            with torch.inference_mode():
                generated = model.generate(input_ids=inputs, attention_mask=mask, do_sample=False, temperature=None, top_p=None, top_k=None, max_new_tokens=6, pad_token_id=tokenizer.pad_token_id, eos_token_id=tokenizer.eos_token_id, repetition_penalty=1.0)
            new_ids = generated[0, inputs.shape[1]:].tolist()
            raw = tokenizer.decode(new_ids, skip_special_tokens=True).strip()
            predicted = raw if raw in LABELS else '__INVALID__'
            finish = 'eos' if new_ids and new_ids[-1] == tokenizer.eos_token_id else ('max_new_tokens' if len(new_ids) == 6 else 'other_or_unknown')
            details.append({'id': row['id'], 'sourceGroup': row['sourceGroup'], 'expected': row['output'], 'predicted': predicted, 'rawOutput': predicted, 'outputCharacters': len(raw), 'generatedTokens': len(new_ids), 'finishReasonInferred': finish})
            completed += 1
            if completed % 32 == 0 or completed == 376:
                print(json.dumps({'PROGRESS': completed, 'reserved': reserved, 'cap': 376}), flush=True)
        metrics = trainer.metrics(rows, [item['predicted'] for item in details])
        errors = [item for item in details if item['expected'] != item['predicted']]
        false_high = sum(metrics['confusionMatrix'][label]['高风险'] for label in ('正常', '焦虑', '低落'))
        result = {'status': 'completed', 'trainOnlyDiagnostic': True, 'manifestSHA256': start['manifestSHA256'], 'modelLoads': 1, 'optimizerUpdates': 0, 'logicalLabelsReserved': reserved, 'logicalLabelsCompleted': completed, 'metrics': {**metrics, 'falseHighCount': false_high}, 'errors': errors, 'errorIDs': [item['id'] for item in errors], 'errorClassDistribution': dict(Counter(item['expected'] for item in errors)), 'errorPredictedClassDistribution': dict(Counter(item['predicted'] for item in errors)), 'errorSourceGroupDistribution': dict(Counter(item['sourceGroup'] for item in errors)), 'casesDetail': details, 'protectionBefore': protection, 'portsBefore': port_states, 'validationRowsLoaded': 0, 'testRowsLoaded': 0, 'acceptanceProbeRowsLoaded': 0, 'GTChanged': False, 'qualificationChanged': False, 'interpretation': 'Complete original train GT scores, with no target review or deletion; no generalization/capacity/qualification proof.'}
        (HERE / 'train-diagnostic-result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
        terminal = {**start, 'status': 'completed', 'endedUnix': time.time(), 'logicalLabelsReserved': reserved, 'logicalLabelsCompleted': completed, 'optimizerUpdates': 0}
        (HERE / 'train-diagnostic-terminal.json').write_text(json.dumps(terminal, indent=2) + '\n')
        print(json.dumps({'TERMINAL': terminal, 'metrics': result['metrics'], 'errorIDs': result['errorIDs']}, ensure_ascii=False), flush=True)
    except BaseException as exc:
        terminal = {**start, 'status': 'failed', 'endedUnix': time.time(), 'logicalLabelsReserved': reserved, 'logicalLabelsCompleted': completed, 'optimizerUpdates': 0, 'errorType': type(exc).__name__}
        (HERE / 'train-diagnostic-terminal.json').write_text(json.dumps(terminal, indent=2) + '\n')
        (HERE / 'train-diagnostic-partial-safe-results.json').write_text(json.dumps(details, ensure_ascii=False, indent=2) + '\n')
        raise


def compare_train(result):
    baseline = json.loads(BASELINE_RESULT.read_text())
    current = result['metrics']
    current_high_misses = sum(current['confusionMatrix']['高风险'][label] for label in (*LABELS, '__INVALID__') if label != '高风险')
    baseline_high_misses = sum(baseline['metrics']['confusionMatrix']['高风险'][label] for label in (*LABELS, '__INVALID__') if label != '高风险')
    checks = {'highMissesTwoToZero': baseline_high_misses == 2 and current_high_misses == 0, 'totalErrorsBelowFive': len(result['errorIDs']) < 5, 'falseHighZeroMaintained': current['falseHighCount'] == 0, 'validityOneMaintained': current['outputValidity'] == 1.0}
    return {'baselineResultSHA256': EXPECTED_BASELINE_RESULT, 'baselineErrors': 5, 'newErrors': len(result['errorIDs']), 'baselineHighMisses': baseline_high_misses, 'newHighMisses': current_high_misses, 'checks': checks, 'improvementSufficient': all(checks.values()), 'qualificationClaim': False}


def strict_validation():
    val = json.loads((HERE / 'training-result.json').read_text())['validation']
    false_high = sum(val['confusionMatrix'][label]['高风险'] for label in ('正常', '焦虑', '低落'))
    checks = {'accuracyAtLeast095': val['accuracy'] >= 0.95, 'macroF1AtLeast095': val['macroF1'] >= 0.95, 'highRiskRecallOne': val['highRiskRecall'] == 1.0, 'falseHighCountZero': false_high == 0, 'outputValidityOne': val['outputValidity'] == 1.0}
    return {'decision': 'PASS' if all(checks.values()) else 'FAIL', 'checks': checks, 'accuracy': val['accuracy'], 'macroF1': val['macroF1'], 'highRiskRecall': val['highRiskRecall'], 'falseHighCount': false_high, 'outputValidity': val['outputValidity'], 'gatesUnchanged': True}


def seal(session_id):
    if (HERE / 'final-seal-manifest.json').exists():
        raise RuntimeError('Already sealed')
    manifest, rows = verify_manifest()
    terminal = json.loads((HERE / 'train-diagnostic-terminal.json').read_text())
    if terminal['status'] != 'completed':
        raise RuntimeError('Normal terminal required')
    try:
        os.kill(terminal['pid'], 0)
        raise RuntimeError('Original model process remains present')
    except ProcessLookupError:
        pass
    protection = verify_sources()
    port_states = ports()
    result = json.loads((HERE / 'train-diagnostic-result.json').read_text())
    if result['logicalLabelsReserved'] != 376 or result['logicalLabelsCompleted'] != 376 or result['optimizerUpdates'] != 0 or len(result['casesDetail']) != 376:
        raise RuntimeError('Terminal budget shape differs')
    summary = {'status': 'completed-and-sealed', 'pid': terminal['pid'], 'execSessionId': session_id, 'observedExitCode': 0, 'sameHandleTerminalConfirmed': True, 'processAbsent': True, 'optimizerUpdates': 0, 'logicalLabelsCompleted': 376, 'manifestSHA256': sha(HERE / 'train-diagnostic-manifest.json'), 'scriptSHA256': sha(__file__), 'resultSHA256': sha(HERE / 'train-diagnostic-result.json'), 'metrics': result['metrics'], 'errorIDs': result['errorIDs'], 'errorClassDistribution': result['errorClassDistribution'], 'errorSourceGroupDistribution': result['errorSourceGroupDistribution'], 'trainComparison': compare_train(result), 'newStrictValidation': strict_validation(), 'generatedTokens': sum(item['generatedTokens'] for item in result['casesDetail']), 'finishReasonDistribution': dict(Counter(item['finishReasonInferred'] for item in result['casesDetail'])), 'protectionAfter': protection, 'portsAfter': port_states, 'oldStrictValDecision': 'FAIL unchanged', 'GTChanged': False, 'productDecoderChanged': False, 'nextAction': 'Seal and stop; any strict FAIL or insufficient train improvement bars qualification/test/export/register; no epoch5/rank/LR/prompt/data/likelihood follow-up.'}
    (HERE / 'train-diagnostic-summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
    files = sorted(path for path in HERE.rglob('*') if path.is_file())
    final_seal = {'status': 'sealed', 'summary': summary, 'files': [{'path': str(path.relative_to(HERE)), 'bytes': path.stat().st_size, 'sha256': sha(path)} for path in files]}
    (HERE / 'final-seal-manifest.json').write_text(json.dumps(final_seal, ensure_ascii=False, indent=2) + '\n')
    for path in HERE.rglob('*'):
        if path.is_file():
            path.chmod(0o444)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('stage', choices=('prepare', 'run', 'seal'))
    parser.add_argument('--session-id', type=int)
    args = parser.parse_args()
    if args.stage == 'prepare':
        prepare()
    elif args.stage == 'run':
        run()
    else:
        if args.session_id is None:
            raise RuntimeError('Observed original exec session is required')
        seal(args.session_id)
