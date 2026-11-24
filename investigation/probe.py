import urllib.request, urllib.error, json, hashlib, pathlib, datetime, concurrent.futures, sys
ROOT=pathlib.Path(__file__).resolve().parent

def fetch(label,url,headers=None):
 h={'User-Agent':'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36','Accept':'*/*'}
 h.update(headers or {})
 rec={'label':label,'url':url,'method':'GET','requested_at_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'request_headers':h}
 try:
  try:r=urllib.request.urlopen(urllib.request.Request(url,headers=h),timeout=35)
  except urllib.error.HTTPError as e:r=e
  body=r.read(); rec.update(status=r.status,final_url=r.geturl(),headers=dict(r.headers),bytes=len(body),sha256=hashlib.sha256(body).hexdigest())
  (ROOT/'raw'/f'{label}.body').write_bytes(body)
  ctype=r.headers.get('Content-Type',''); snippet=body[:200].decode('utf-8',errors='replace') if any(x in ctype for x in ['text','json','xml','javascript']) else ''
  print(json.dumps({k:rec[k] for k in ['label','url','status','bytes','sha256']}|{'type':ctype,'snippet':snippet},ensure_ascii=False),flush=True)
 except Exception as e:rec['error']=str(e);print(json.dumps(rec),flush=True)
 (ROOT/'raw'/f'{label}.json').write_text(json.dumps(rec,indent=2))
 return rec
if __name__=='__main__':
 jobs=json.loads(pathlib.Path(sys.argv[1]).read_text())
 with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(lambda j:fetch(*j),jobs))
