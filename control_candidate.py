import sys,json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from lululemon_monitor import check,PRODUCT_ID,PRODUCT_NAME

r=check(Path(__file__).resolve().parent/'.private/direct-control/http',us_size='XS')
if r.get('has_request_error') or r.get('product_name')!=PRODUCT_NAME: raise RuntimeError('Control API failure')
p=json.loads(Path(next(q['body_file'] for q in r['requests'] if q['label']=='product')).read_text())
i=json.loads(Path(next(q['body_file'] for q in r['requests'] if q['label']=='inventory')).read_text())
a=p['data']['attributes']
keys={str(s['key']) for s in a['sizes'] if any(t.get('region')=='US' and t.get('label')=='XS' for t in s.get('sizeSystems',[]))}
rows=[s for s in a['skus'] if str(s['id'])=='159402216' and s['color']['name']=='Black'
      and str(s['color']['id']).lstrip('0')=='1' and s['color']['colorGroup']=='BLK'
      and str(s['sizeTokenKey']) in keys and s['size']=='6-8']
if len(rows)!=1: raise RuntimeError('Control identity mismatch')
s=rows[0]; stock=next(x['attributes'] for x in i['data'] if str(x['id'])==str(s['id']))
if stock.get('isAvailable') is not True: raise RuntimeError('Control sold out')
price=s['price']; amount=price.get('salePrice') or price.get('listPrice')
c={'product_id':PRODUCT_ID,'product_name':PRODUCT_NAME,'sku':str(s['id']),'color':s['color'],
   'us_size':'XS','au_size':'6-8','quantity':1,'checked_at_utc':r['checked_at_utc'],
   'is_available':True,'stock_source':'availability_endpoint','is_final_sale':s.get('isFinalSale'),
   'price':price,'unit_price':str(amount)}
print(json.dumps(c))
