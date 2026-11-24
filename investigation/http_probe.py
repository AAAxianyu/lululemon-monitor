from curl_cffi import requests
import json,hashlib,pathlib,datetime,concurrent.futures,sys
ROOT=pathlib.Path(__file__).resolve().parent

def fetch(label,url,headers=None):
 rec={'label':label,'url':url,'method':'GET','client':'curl_cffi; impersonate=chrome; no cookies or authentication','requested_at_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'request_headers':headers or {}}
 try:
  r=requests.get(url,impersonate='chrome',headers=headers or {},timeout=40)
  b=r.content;rec.update(status=r.status_code,final_url=r.url,headers=dict(r.headers),bytes=len(b),sha256=hashlib.sha256(b).hexdigest())
  (ROOT/'raw'/f'{label}.body').write_bytes(b)
  print(json.dumps({k:rec[k] for k in ['label','url','status','bytes','sha256']}|{'type':r.headers.get('content-type'),'snippet':r.text[:140] if not r.headers.get('content-type','').startswith('image/') else ''},ensure_ascii=False),flush=True)
 except Exception as e:rec['error']=str(e);print(json.dumps(rec),flush=True)
 (ROOT/'raw'/f'{label}.json').write_text(json.dumps(rec,indent=2))
 return rec
if __name__=='__main__':
 jobs=json.loads(pathlib.Path(sys.argv[1]).read_text())
 with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:list(pool.map(lambda j:fetch(*j),jobs))
