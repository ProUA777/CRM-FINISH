import os,sys,tempfile,re,random,string
d=tempfile.mkdtemp()
os.environ.update(CRM_SECRET_KEY="x",CRM_DEBUG="0",CRM_DATA_DIR=d)
sys.path.insert(0,'/home/claude')
import app as A
A.DB_PATH=d+"/t.db"; A.init_db(); A.app.config["TESTING"]=True
A.app.config["PROPAGATE_EXCEPTIONS"]=False
import traceback
random.seed(1)
posts=[r for r in A.app.url_map.iter_rules() if "POST" in r.methods and r.endpoint!="static"]
skip=("logout","restore","shutdown","backup","reset","login")
GARB=["","0","-1","1e999","nan","inf","99999999999999999999","<script>x</script>","' OR 1=1--","а"*5000,"\x00","2020-13-45","-5.5","1,5"]
users=[("admin","admin123"),("oleh","manager123"),("maister","prod123"),("frezer","frezer123"),("sklad","sklad123"),("buh","buh12345"),("servis","servis123")]
fails={}
total=0
for u,p in users:
    c=A.app.test_client(); c.post("/login",data={"username":u,"password":p})
    c.get("/"); 
    with c.session_transaction() as s: tok=s.get("_csrf_token")
    for r in posts:
        if any(k in r.endpoint for k in skip): continue
        path=re.sub(r"<(?:int:)?\w+>","1",r.rule)
        fields=["name","title","email","phone","amount","quantity","weight_kg","margin_pct","price","qty","date","due_date","stage","status","notes","description","part_description","work_date","quantity_made","rate","hours","planned_hours","value","key","currency","client_id","deal_id","item_id","wc_id","work_center_id","target_price_per_unit","ai_description","username","password","role"]
        for g in GARB:
            data={"_csrf_token":tok}
            for f in random.sample(fields,12): data[f]=g
            try: resp=c.post(path,data=data)
            except Exception as e:
                tb=traceback.extract_tb(e.__traceback__)[-1]; fails.setdefault((r.endpoint,"EXC"),[]).append((u,g[:12],repr(e)[:80],tb.filename.split('/')[-1],tb.lineno)); continue
            total+=1
            if resp.status_code>=500: fails.setdefault((r.endpoint,resp.status_code),[]).append((u,g[:20]))
print("requests:",total)
print("FAILS:",len(fails))
for k,v in fails.items(): print(k,len(v),v[:2])
