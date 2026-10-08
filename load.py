import threading,requests,re,time,collections
B="http://127.0.0.1:5098"
res=collections.Counter(); errs=[]
def worker(i):
    s=requests.Session()
    r=s.post(B+"/login",data={"username":"admin","password":"admin123"},allow_redirects=False); 
    for k in range(12):
        t=s.get(B+"/tasks"); res[t.status_code]+=1
        tok=re.search(r'name="_csrf_token" value="([^"]+)"',t.text).group(1)
        p=s.post(B+"/tasks/add",data={"_csrf_token":tok,"title":f"load {i}-{k}"},allow_redirects=False); res["post%d"%p.status_code]+=1
        if p.status_code>=500: errs.append(p.text[:100])
        for path in ("/","/deals","/clients","/warehouse","/production","/calculator"):
            g=s.get(B+path); res[g.status_code]+=1
            if g.status_code>=500: errs.append(path)
t0=time.time()
th=[threading.Thread(target=worker,args=(i,)) for i in range(25)]
[x.start() for x in th]; [x.join() for x in th]
print(dict(res),"errors:",len(errs),errs[:3],"sec",round(time.time()-t0,1))
