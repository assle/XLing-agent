"""Current answer-model shadow through AiClient.complete: format8 then val128 once."""
from __future__ import annotations
import hashlib
import importlib.util
import inspect
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
import httpx
ROOT=Path('/Users/assle/dev/mindbridge-py')
CANDIDATE=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT))
from app.core.config import Settings
from app.schemas.dtos import AiMessage
from app.services.ai import AiClient
PROTOCOL_SHA256='c7068ac81542983df233a2d5acc1c66aabf22aa49fb19c661538efe88832ae0a'
LABELS=('正常','焦虑','低落','高风险')
INVALID='__INVALID__'
def sha(path):
 h=hashlib.sha256()
 with path.open('rb') as stream:
  for block in iter(lambda:stream.read(1024*1024),b''):h.update(block)
 return h.hexdigest()
def now():return datetime.now(timezone.utc).isoformat()
def write(path,value):
 temp=path.with_suffix(path.suffix+'.tmp');temp.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n');temp.replace(path)
def rows(path):return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
def verify(protocol,freeze):
 assert sha(CANDIDATE/'protocol.json')==PROTOCOL_SHA256
 for name,h in freeze['candidateFilesSHA256'].items():assert sha(CANDIDATE/name)==h,'Frozen shadow file changed'
 for name,h in protocol['sourceAndBusinessFingerprints'].items():assert sha(Path(name))==h,'Source or business fingerprint changed'
 for name,h in protocol['localModelFilesSHA256'].items():assert sha(Path(name))==h,'Local model fingerprint changed'
def parse_label(raw,finish_reason):
 if not isinstance(raw,str) or finish_reason!='stop':return INVALID
 def unique_object(pairs):
  keys=[key for key,value in pairs]
  if len(keys)!=len(set(keys)):raise ValueError('duplicate JSON key')
  return dict(pairs)
 try:decoded=json.loads(raw,object_pairs_hook=unique_object)
 except (json.JSONDecodeError,ValueError,TypeError):return INVALID
 if not isinstance(decoded,dict) or set(decoded)!={'label'}:return INVALID
 value=decoded['label']
 return value if isinstance(value,str) and value in LABELS else INVALID

def main():
 progress=CANDIDATE/'progress.json'
 if progress.exists():raise RuntimeError('Already started; no rerun or retry allowed')
 protocol=json.loads((CANDIDATE/'protocol.json').read_text());freeze=json.loads((CANDIDATE/'freeze-manifest.json').read_text())
 state={'PID':os.getpid(),'startedAtUTC':now(),'status':'running','phase':'source-check','logicalRequests':0,'httpAttempts':0,'completedResponses':0,'logicalCap':136,'httpCap':136,'retryCount':0,'heldoutRequests':0,'logicalByPhase':{'format-precheck':0,'validation':0},'httpByPhase':{'format-precheck':0,'validation':0},'completedByPhase':{'format-precheck':0,'validation':0},'configuredModel':protocol['configuredModel'],'protocolSHA256':PROTOCOL_SHA256,'freezeSHA256':sha(CANDIDATE/'freeze-manifest.json')}
 write(progress,state);print(json.dumps({'event':'started','PID':state['PID'],'progress':str(progress),'logicalAndHTTPCap':136}),flush=True)
 records=[];response_info={};original_post=httpx.post;exit_code=0
 try:
  verify(protocol,freeze)
  settings=Settings()
  assert settings.ai_provider=='openai' and settings.openai_model==protocol['configuredModel'] and settings.openai_base_url.rstrip('/')==protocol['configuredBaseURL'].rstrip('/') and settings.ai_max_tokens==2048 and bool(settings.openai_api_key),'Critical configured answer model unavailable'
  assert httpx.__version__==protocol['httpxVersion']
  assert inspect.signature(httpx.post).parameters['follow_redirects'].default is False
  assert inspect.signature(httpx.HTTPTransport).parameters['retries'].default==0
  settings=settings.model_copy(update={'ai_temperature':0.0})
  for name in ('HTTP_PROXY','http_proxy','HTTPS_PROXY','https_proxy','ALL_PROXY','all_proxy'):os.environ[name]=''
  os.environ['NO_PROXY']='*';os.environ['no_proxy']='*'
  system=(CANDIDATE/'system-prompt.txt').read_text();assert hashlib.sha256(system.encode()).hexdigest()==protocol['systemPromptSHA256']
  helper_path=CANDIDATE.parent/'generic-3b-validation-shadow/run-shadow.py'
  spec=importlib.util.spec_from_file_location('frozen_metric_helper',helper_path);helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)
  def observed_post(url,**kwargs):
   assert str(url)==protocol['endpoint'],'Unexpected endpoint; stop'
   payload=kwargs['json']
   assert set(payload)=={'model','messages','temperature','max_tokens','stream'}
   assert payload['model']==protocol['configuredModel'] and payload['temperature']==0.0 and payload['max_tokens']==2048 and payload['stream'] is False
   assert payload['messages']==[{'role':'system','content':system},{'role':'user','content':current_row['input']}]
   assert kwargs['timeout']==120
   if state['httpAttempts']>=136:raise RuntimeError('HTTP cap reached; no extra request')
   state['httpAttempts']+=1;state['httpByPhase'][state['phase']]+=1;write(progress,state)
   payload['response_format']={'type':'json_object'}
   payload['thinking']={'type':'enabled'}
   assert set(payload)=={'model','messages','temperature','max_tokens','stream','response_format','thinking'}
   response=original_post(url,**kwargs)
   response_info['httpStatus']=response.status_code
   if response.status_code<400:
    data=response.json();usage=data.get('usage',{})
    response_info['usage']={k:v for k,v in usage.items() if isinstance(v,(int,float)) and not isinstance(v,bool)}
    response_info['finishReason']=data.get('choices',[{}])[0].get('finish_reason')
    response_info['responseModel']=data.get('model')
   return response
  httpx.post=observed_post;client=AiClient(settings)
  def classify(row,phase):
   nonlocal current_row
   if state['logicalRequests']>=136:raise RuntimeError('Logical request cap reached')
   current_row=row;response_info.clear();state['phase']=phase;state['logicalRequests']+=1;state['logicalByPhase'][phase]+=1;state['lastRequestedId']=row['id'];state['lastRequestAtUTC']=now();write(progress,state)
   start=time.perf_counter()
   raw=client.complete([AiMessage(role='system',content=system),AiMessage(role='user',content=row['input'])])
   if not isinstance(raw,str):raise RuntimeError('Non-string returned content; stop with no retry')
   stripped=raw.strip();prediction=parse_label(raw,response_info.get('finishReason'))
   detail={'id':row['id'],'expected':row['output'],'predicted':prediction,'rawOutput':raw,'rawStrippedOutput':stripped,'rawLegal':prediction in LABELS,'phase':phase,'latencyMs':(time.perf_counter()-start)*1000,**response_info}
   records.append(detail);state['completedResponses']+=1;state['completedByPhase'][phase]+=1;state['lastResponseAtUTC']=now();write(CANDIDATE/'raw-responses.json',records);write(progress,state)
   return detail
  current_row={}
  pre=rows(CANDIDATE/'format-precheck-8.jsonl');assert len(pre)==8 and [r['id'] for r in pre]==protocol['precheck']['ids']
  details8=[classify(r,'format-precheck') for r in pre];format_ok=all(d['rawLegal'] for d in details8)
  write(CANDIDATE/'format-precheck-result.json',{'cases':8,'rawLegalOutputs':sum(d['rawLegal'] for d in details8),'passed':format_ok,'casesDetail':details8,'accuracyUsedAsGate':False})
  print(json.dumps({'event':'format-precheck-completed','rawLegalOutputs':sum(d['rawLegal'] for d in details8),'passed':format_ok,'logicalRequests':state['logicalRequests'],'httpAttempts':state['httpAttempts']}),flush=True)
  if not format_ok:state.update({'status':'rejected','decision':'stop-at-format-precheck','validationRowsEvaluated':0})
  else:
   val_path=Path(protocol['validation']['path']);assert sha(val_path)==protocol['validation']['sha256'];val=rows(val_path);assert len(val)==128
   details=[]
   for row in val:
    details.append(classify(row,'validation'))
    if len(details)%16==0:print(json.dumps({'event':'validation-progress','completed':len(details),'total':128,'logicalRequests':state['logicalRequests'],'httpAttempts':state['httpAttempts']}),flush=True)
   measurements=helper.metric(val,details);g=protocol['validation']['gate']
   checks={'accuracyAtLeastMinimum':measurements['accuracy']>=g['accuracyMinimum'],'macroF1AtLeastMinimum':measurements['macroF1']>=g['macroF1Minimum'],'highRiskRecallExactlyRequired':measurements['highRiskRecall']==g['highRiskRecallRequired'],'falseHighRiskWithinMaximum':measurements['falseHighRiskCount']<=g['falseHighRiskCountMaximum'],'outputValidityExactlyRequired':measurements['outputValidity']==g['outputValidityRequired']};passed=all(checks.values())
   write(CANDIDATE/'validation-result.json',{'measurements':measurements,'frozenThresholds':g,'checks':checks,'passed':passed,'casesDetail':details,'heldoutRowsEvaluated':0})
   state.update({'status':'completed','passed':passed,'validationRowsEvaluated':128,'decision':'validation-pass-notify-root-only' if passed else 'validation-fail-seal-and-stop'})
   print(json.dumps({'event':'validation-completed','measurements':measurements,'checks':checks,'passed':passed},ensure_ascii=False),flush=True)
  assert state['httpAttempts']==state['logicalRequests']==state['completedResponses']<=136
  verify(protocol,freeze);state['sourceBusinessLocalModelsStillMatch']=True
 except Exception as error:
  exit_code=1;status=response_info.get('httpStatus')
  state.update({'status':'blocked','decision':'stop-no-retry','errorType':type(error).__name__,'lastHTTPStatus':status,'blockCategory':'authentication' if status in (401,403) else 'provider-environment-or-integrity','heldoutRequests':0})
  print(json.dumps({'event':'stopped','errorType':type(error).__name__,'httpStatus':status,'logicalRequests':state['logicalRequests'],'httpAttempts':state['httpAttempts'],'retryCount':0}),flush=True)
 finally:
  httpx.post=original_post;state['completedAtUTC']=now();state['processExitCode']=exit_code;write(progress,state)
  result={'version':'deepseek-json-thinking-enabled-validation-shadow-safe-result-1','status':state['status'],'decision':state.get('decision'),'configuredModel':protocol['configuredModel'],'modelEquivalenceToCLSOrHalfB':False,'outputContract':'JSON exact unique label key; strict four enums; finishReason stop','thinking':'enabled','temperatureDeterminismClaim':False,'PID':state['PID'],'processExitCode':exit_code,'logicalRequests':state['logicalRequests'],'httpAttempts':state['httpAttempts'],'completedResponses':state['completedResponses'],'logicalByPhase':state['logicalByPhase'],'httpByPhase':state['httpByPhase'],'completedByPhase':state['completedByPhase'],'heldoutRequests':0,'retryCount':0,'temperature':0,'answerMaxTokens':2048,'credentialHandling':'only in process Settings and transient Authorization header; no credentials serialized or printed','sourceBusinessLocalModelsStillMatch':state.get('sourceBusinessLocalModelsStillMatch',False),'protocolSHA256':PROTOCOL_SHA256,'freezeSHA256':sha(CANDIDATE/'freeze-manifest.json'),'configurationDifference':protocol['configurationDifference'],'limitations':protocol['confounds'],'responseModelDistribution':dict(__import__('collections').Counter(d.get('responseModel') for d in records)),'usageTotals':{key:sum(d.get('usage',{}).get(key,0) for d in records) for key in sorted({key for d in records for key in d.get('usage',{})})},'clinicalRelease':False,'applicationEnvTagsGroundTruthUnmodified':True}
  if (CANDIDATE/'validation-result.json').exists():result['validationGate']={k:v for k,v in json.loads((CANDIDATE/'validation-result.json').read_text()).items() if k!='casesDetail'}
  write(CANDIDATE/'safe-result-summary.json',result)
  for p in CANDIDATE.iterdir():
   if p.is_file():p.chmod(0o444)
  seal={'sealedAtUTC':now(),'decision':state.get('decision'),'files':[{'file':p.name,'sha256':sha(p),'bytes':p.stat().st_size,'mode':'0444'} for p in sorted(CANDIDATE.iterdir()) if p.is_file() and p.name!='final-seal-manifest.json']};write(CANDIDATE/'final-seal-manifest.json',seal);(CANDIDATE/'final-seal-manifest.json').chmod(0o444)
 return exit_code
if __name__=='__main__':sys.exit(main())
