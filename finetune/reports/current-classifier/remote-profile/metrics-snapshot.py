"""One bounded general3B task-SYSTEM shadow: format8 then validation128, no heldout."""
from __future__ import annotations
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

CANDIDATE = Path(__file__).resolve().parent
PROTOCOL_SHA256 = '6fcc8b201bcfc2033b9df47ea9c00aae09875a5c39af157a860b0449bf9b4b29'
LABELS = ('正常', '焦虑', '低落', '高风险')
INVALID = '__INVALID__'

def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''):
            h.update(block)
    return h.hexdigest()

def now():
    return datetime.now(timezone.utc).isoformat()

def write(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)

def load_rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

def verify(protocol, frozen):
    assert sha(CANDIDATE/'protocol.json') == PROTOCOL_SHA256
    for name, expected in frozen['candidateFilesSHA256'].items():
        assert sha(CANDIDATE/name) == expected, 'Frozen shadow file changed: '+name
    for name, expected in protocol['fixedSourceAndBusinessFingerprints'].items():
        assert sha(Path(name)) == expected, 'Frozen source/business fingerprint changed'
    for name, expected in protocol['localGenericModelFilesSHA256'].items():
        assert sha(Path(name)) == expected, 'Frozen generic model changed'

def metric(rows, details):
    matrix = {label:{prediction:0 for prediction in LABELS+(INVALID,)} for label in LABELS}
    for row, detail in zip(rows, details):
        assert row['id'] == detail['id']
        matrix[row['output']][detail['predicted']] += 1
    per = {}
    for label in LABELS:
        tp = matrix[label][label]
        fp = sum(matrix[other][label] for other in LABELS if other != label)
        fn = sum(matrix[label][other] for other in LABELS+(INVALID,) if other != label)
        precision, recall = tp/max(1,tp+fp), tp/max(1,tp+fn)
        per[label] = {'precision':precision,'recall':recall,'f1':2*precision*recall/max(1e-12,precision+recall)}
    return {'cases':len(rows),'correct':sum(d['expected']==d['predicted'] for d in details),
            'accuracy':sum(d['expected']==d['predicted'] for d in details)/len(rows),
            'macroF1':sum(per[label]['f1'] for label in LABELS)/4,
            'highRiskRecall':per['高风险']['recall'],
            'falseHighRiskCount':sum(matrix[expected]['高风险'] for expected in LABELS if expected!='高风险'),
            'outputValidity':sum(d['predicted'] in LABELS for d in details)/len(rows),
            'perClass':per,'confusionMatrix':matrix}

def main():
    progress_path = CANDIDATE/'progress.json'
    if progress_path.exists():
        raise RuntimeError('Shadow already started; no rerun or retry allowed')
    protocol = json.loads((CANDIDATE/'protocol.json').read_text())
    frozen = json.loads((CANDIDATE/'freeze-manifest.json').read_text())
    verify(protocol, frozen)
    system = (CANDIDATE/'system-prompt.txt').read_text()
    assert hashlib.sha256(system.encode()).hexdigest() == protocol['systemPromptSHA256']
    assert protocol['requestBodyTemplate']['options'] == {'temperature':0,'num_predict':6}
    assert protocol['logicalLabelRequestCap'] == 136 and protocol['retryCount'] == 0
    pre = load_rows(CANDIDATE/'format-precheck-8.jsonl')
    assert len(pre)==8 and [row['id'] for row in pre] == protocol['formatPrecheck']['ids']
    state = {'PID':os.getpid(),'startedAtUTC':now(),'status':'running','phase':'format-precheck',
             'logicalRequestsAttempted':0,'logicalResponsesCompleted':0,
             'attemptedByPhase':{'format-precheck':0,'validation':0},
             'completedByPhase':{'format-precheck':0,'validation':0},
             'logicalRequestCap':136,'retryCount':0,'heldoutRequests':0,
             'modelTag':protocol['modelTag'],'modelDigest':protocol['modelDigest'],
             'protocolSHA256':PROTOCOL_SHA256,'freezeManifestSHA256':sha(CANDIDATE/'freeze-manifest.json')}
    write(progress_path,state)
    print(json.dumps({'event':'started','PID':state['PID'],'progress':str(progress_path),'cap':136}),flush=True)
    request_records=[]
    def classify(row, phase):
        if state['logicalRequestsAttempted'] >= 136:
            raise RuntimeError('Frozen request cap reached; no extra request')
        body={'model':protocol['modelTag'],'messages':[{'role':'system','content':system},{'role':'user','content':row['input']}],
              'stream':False,'options':{'temperature':0,'num_predict':6}}
        assert 'format' not in body
        state['phase']=phase
        state['logicalRequestsAttempted']+=1
        state['attemptedByPhase'][phase]+=1
        state['lastRequestedId']=row['id']
        state['lastRequestAtUTC']=now()
        write(progress_path,state)
        started=time.perf_counter()
        encoded=json.dumps(body,ensure_ascii=False).encode()
        req=Request(protocol['endpoint'],data=encoded,headers={'Content-Type':'application/json'},method='POST')
        # urllib has no configured retry, alternate endpoint or model fallback.
        with urlopen(req,timeout=protocol['requestTimeoutSeconds']) as response:
            payload=json.loads(response.read())
        if payload.get('error'):
            raise RuntimeError('Ollama returned an environment/model error; no retry')
        if payload.get('model')!=protocol['modelTag'] or not payload.get('done'):
            raise RuntimeError('Unexpected model or incomplete non-stream response; no retry')
        raw=payload['message']['content']
        if not isinstance(raw,str):
            raise RuntimeError('Non-string model content; no retry')
        stripped=raw.strip()
        predicted=stripped if stripped in LABELS else INVALID
        detail={'id':row['id'],'expected':row['output'],'predicted':predicted,'rawOutput':raw,
                'rawStrippedOutput':stripped,'rawLegal':predicted in LABELS,'phase':phase,
                'latencyMs':(time.perf_counter()-started)*1000,
                'responseModel':payload['model'],'doneReason':payload.get('done_reason'),
                'evalCount':payload.get('eval_count'),'promptEvalCount':payload.get('prompt_eval_count')}
        request_records.append(detail)
        state['logicalResponsesCompleted']+=1
        state['completedByPhase'][phase]+=1
        state['lastResponseAtUTC']=now()
        write(CANDIDATE/'raw-responses.json',request_records)
        write(progress_path,state)
        return detail
    exit_code=0
    try:
        pre_details=[classify(row,'format-precheck') for row in pre]
        format_ok=all(detail['rawLegal'] for detail in pre_details)
        write(CANDIDATE/'format-precheck-result.json',{'cases':8,'rawLegalOutputs':sum(d['rawLegal'] for d in pre_details),'passed':format_ok,'casesDetail':pre_details,'accuracyUsedAsGate':False})
        print(json.dumps({'event':'format-precheck-completed','rawLegalOutputs':sum(d['rawLegal'] for d in pre_details),'passed':format_ok}),flush=True)
        if not format_ok:
            state.update({'status':'rejected','decision':'stop-at-format-precheck','validationRowsEvaluated':0})
        else:
            val_path=Path(protocol['validation']['path'])
            assert sha(val_path)==protocol['validation']['sha256']
            val=load_rows(val_path)
            assert len(val)==128
            details=[]
            for row in val:
                details.append(classify(row,'validation'))
                if len(details)%16==0:
                    print(json.dumps({'event':'validation-progress','completed':len(details),'total':128,'logicalRequests':state['logicalRequestsAttempted']}),flush=True)
            measurements=metric(val,details)
            threshold=protocol['validation']['gate']
            checks={'accuracyAtLeastMinimum':measurements['accuracy']>=threshold['accuracyMinimum'],
                    'macroF1AtLeastMinimum':measurements['macroF1']>=threshold['macroF1Minimum'],
                    'highRiskRecallExactlyRequired':measurements['highRiskRecall']==threshold['highRiskRecallRequired'],
                    'falseHighRiskWithinMaximum':measurements['falseHighRiskCount']<=threshold['falseHighRiskCountMaximum'],
                    'outputValidityExactlyRequired':measurements['outputValidity']==threshold['outputValidityRequired']}
            passed=all(checks.values())
            write(CANDIDATE/'validation-result.json',{'measurements':measurements,'frozenThresholds':threshold,'checks':checks,'passed':passed,'casesDetail':details,'heldoutRowsEvaluated':0})
            state.update({'status':'completed','passed':passed,'validationRowsEvaluated':128,
                          'decision':'validation-pass-notify-root-only' if passed else 'validation-fail-seal-and-stop'})
            print(json.dumps({'event':'validation-completed','measurements':measurements,'checks':checks,'passed':passed},ensure_ascii=False),flush=True)
        verify(protocol,frozen)
        state['fixedSourceBusinessAndGenericWeightsStillMatch']=True
    except Exception as error:
        exit_code=1
        state.update({'status':'blocked','decision':'stop-no-retry-on-auth-or-environment-or-integrity-block',
                      'errorType':type(error).__name__,'error':str(error),'heldoutRequests':0})
        print(json.dumps({'event':'stopped','errorType':type(error).__name__,'attempted':state['logicalRequestsAttempted'],'completed':state['logicalResponsesCompleted'],'retryCount':0}),flush=True)
    finally:
        state['completedAtUTC']=now()
        state['processExitCode']=exit_code
        write(progress_path,state)
        write(CANDIDATE/'safe-result-summary.json',{'version':'generic3b-validation-shadow-safe-summary-1',
            'modelTag':protocol['modelTag'],'modelDigest':protocol['modelDigest'],'nativeRegisteredCLSEquivalence':False,
            'systemPromptSHA256':protocol['systemPromptSHA256'],'validationSHA256':protocol['validation']['sha256'],
            'status':state['status'],'decision':state['decision'],'processPID':state['PID'],'processExitCode':exit_code,
            'logicalRequestsAttempted':state['logicalRequestsAttempted'],'logicalResponsesCompleted':state['logicalResponsesCompleted'],
            'attemptedByPhase':state['attemptedByPhase'],'completedByPhase':state['completedByPhase'],'retryCount':0,
            'heldoutRequests':0,'fixedSourceBusinessAndGenericWeightsStillMatch':state.get('fixedSourceBusinessAndGenericWeightsStillMatch',False),
            'freezeManifestSHA256':sha(CANDIDATE/'freeze-manifest.json'),'protocolSHA256':PROTOCOL_SHA256,
            'limitations':protocol['confounds'],'clinicalRelease':False,'downloadedRegisteredOrDeployed':False})
        for path in CANDIDATE.iterdir():
            if path.is_file():path.chmod(0o444)
        seal={'decision':state['decision'],'sealedAtUTC':now(),'files':[{'file':path.name,'sha256':sha(path),'bytes':path.stat().st_size,'mode':'0444'} for path in sorted(CANDIDATE.iterdir()) if path.is_file() and path.name!='final-seal-manifest.json']}
        write(CANDIDATE/'final-seal-manifest.json',seal)
        (CANDIDATE/'final-seal-manifest.json').chmod(0o444)
    return exit_code

if __name__=='__main__':
    sys.exit(main())
