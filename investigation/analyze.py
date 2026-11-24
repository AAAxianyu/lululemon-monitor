from pathlib import Path
from collections import Counter
import json,re,hashlib
ROOT=Path(__file__).resolve().parent
raw=ROOT/'raw'
records=[]
for p in sorted(raw.glob('*.json')):
 j=json.loads(p.read_text())
 if 'url' not in j:continue
 b=p.with_suffix('.body')
 if b.exists():
  assert j.get('sha256')==hashlib.sha256(b.read_bytes()).hexdigest(),p
  ct=str(j.get('headers',{}).get('content-type',j.get('headers',{}).get('Content-Type','')))
  if 'image/' not in ct:j['response_excerpt']=b.read_text(errors='replace')[:400]
 j['response_file']=str(b.relative_to(ROOT));records.append(j)
(ROOT/'request_log.json').write_text(json.dumps(records,indent=2,ensure_ascii=False))
summary={'target':{'productId':'lopmi7nqah','name':'Scuba Evolve Classic Full-Zip Hoodie','color':'Lavender Rock','colorId':'77631','colorGroup':'LAVR'},'regions':{}}
for region in ['au','us','ca']:
 n=region+'_product_api';j=json.loads((raw/(n+'.body')).read_text());a=j['data']['attributes'];colors={str(c['id']).lstrip('0'):c for c in a['colors']}
 d={'name':a['name'],'productId':j['data']['id'],'colors':a['colors'],'colorCount':len(a['colors']),'skuCount':len(a['skus']),'styles':{s['id']:[{'id':c['id'],'colorId':c['colorId']} for c in s['colors']] for s in a['styles']},'newColorsBadging':a.get('newColorsBadging'),'badgesWithoutColorRecord':[c for c in a.get('newColorsBadging',[]) if str(c).lstrip('0') not in colors],'targetColors':[c for c in a['colors'] if c['id']=='77631' or re.search('Lavender|LAVR',str(c),re.I)],'targetSkus':[s for s in a['skus'] if s['color']['id']=='77631' or re.search('Lavender|LAVR',str(s['color']),re.I)]}
 summary['regions'][region]=d
 (ROOT/(n+'.pretty.json')).write_text(json.dumps(j,indent=2,ensure_ascii=False))
 if region=='au':
  stock=json.loads((raw/'au_stock_locale.body').read_text())['data'];ids={s['id'] for s in a['skus']};d['inventoryCount']=len(stock);d['inventoryExtraIds']=sorted({s['id'] for s in stock}-ids)
  d['colorInventory']=[{'id':c['id'],'name':c['name'],'skuCount':sum(s['color']['id']==c['id'] for s in a['skus']),'availableSkuCount':sum(s['color']['id']==c['id'] and s['availability']['isAvailable'] for s in a['skus'])} for c in a['colors']]
hk=json.loads((raw/'hk_variation_lavr.body').read_text());p=hk['product'];vc=next(a['values'] for a in p['variationAttributes'] if a['id']=='color')
summary['hongKong']={'requestedQuery':hk['queryString'],'productName':p['productName'],'selectedColor':p['selectedColor'],'selectedProductUrl':p['selectedProductUrl'],'colorCount':len(vc),'colors':[{'id':v['id'],'name':v['displayValue'],'selected':v['selected'],'selectable':v['selectable']} for v in vc],'targetColors':[v for v in vc if str(v['id']).lstrip('0')=='77631'],'jsonLdSkuCount':len(json.loads((ROOT/'hk_productgroup.json').read_text())['hasVariant'])}
summary['images']=[{k:j.get(k) for k in ['label','url','status','bytes','sha256','decoded_image','response_excerpt']} for j in records if j.get('label','').startswith(('img_','render_','imageset_'))]
(ROOT/'findings.json').write_text(json.dumps(summary,indent=2,ensure_ascii=False))
print(json.dumps({'requests':len(records),'httpStatuses':dict(Counter(j.get('status','error') for j in records)),'regions':{k:{f:v[f] for f in ['colorCount','skuCount','targetColors','targetSkus','badgesWithoutColorRecord']} for k,v in summary['regions'].items()},'auColorInventory':summary['regions']['au']['colorInventory'],'inventoryExtraIds':summary['regions']['au']['inventoryExtraIds'],'hk':summary['hongKong']},ensure_ascii=False,indent=2))
