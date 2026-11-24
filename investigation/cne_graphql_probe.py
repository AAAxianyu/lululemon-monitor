from curl_cffi import requests
from pathlib import Path
import json,hashlib,datetime,concurrent.futures
root=Path(__file__).resolve().parent
query=(root/'GetPdpDataById.graphql').read_text()
def run(j):
 label,host,loc=j;url='https://'+host+'/cne/graphql';headers={'x-lll-locale':loc.lower().replace('_','-'),'x-user-agent':'pdp-app','Content-Type':'application/json'}
 payload={'operationName':'GetPdpDataById','query':query,'variables':{'id':'lopmi7nqah','category':'','unifiedId':'','locale':loc,'fetchPcmMedia':True,'fetchVariants':True}}
 rec={'label':label,'method':'POST','url':url,'request_headers':headers,'payload':payload,'requested_at_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'client':'curl_cffi chrome; anonymous'}
 try:
  r=requests.post(url,json=payload,headers=headers,impersonate='chrome',timeout=45)
  rec.update(status=r.status_code,bytes=len(r.content),headers=dict(r.headers),sha256=hashlib.sha256(r.content).hexdigest())
  (root/'raw'/f'{label}.body').write_bytes(r.content);print(label,r.status_code,len(r.content),r.text[:500],flush=True)
 except Exception as e:rec['error']=str(e);print(label,str(e),flush=True)
 (root/'raw'/f'{label}.json').write_text(json.dumps(rec,indent=2))
with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:list(ex.map(run,[('au_cne_graphql','www.lululemon.com.au','en-au'),('us_cne_graphql','shop.lululemon.com','en-us')]))
