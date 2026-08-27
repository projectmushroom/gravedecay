#!/usr/bin/env python3
"""launchd worker: validate, stage, cut over, or restore the prior payload."""
import json, os, re, shutil, subprocess, tempfile, time
ROOT=os.path.realpath(os.environ["GRAVE_ROOT"]); ORIGIN="https://github.com/projectmushroom/gravedecay.git"; CONFIG=os.path.join(ROOT,"config"); REPO=os.path.join(ROOT,"repos","gravedecay")
REQUEST,STATUS,LOCK=(os.path.join(CONFIG,n) for n in ("update-request.json","update-status.json","update.lock")); LOG=os.path.join(ROOT,"logs","updater.log"); TAG=re.compile(r"v(\d+)\.(\d+)\.(\d+)$")
def under(p): return os.path.commonpath((ROOT,os.path.realpath(p)))==ROOT and os.path.realpath(p)!=ROOT
def clean(p):
 if p and under(p) and os.path.exists(p):
  try: shutil.rmtree(p)
  except OSError: pass
def note(x):
 if not under(LOG): return
 os.makedirs(os.path.dirname(LOG),exist_ok=True)
 with open(LOG,"a") as f: f.write(json.dumps(x,separators=(",",":"))+"\n")
 if os.path.getsize(LOG)>65536:
  with open(LOG,"rb") as f: f.seek(-32768,2); tail=f.read()
  with open(LOG+".tmp","wb") as f: f.write(tail)
  os.replace(LOG+".tmp",LOG)
def status(x):
 t=STATUS+".tmp"
 with open(t,"w") as f: json.dump(x,f,separators=(",",":"))
 os.replace(t,STATUS); note(x)
def metadata(channel,target):
 current="" if channel=="edge" else target
 checkout=(target+" "+run("git","rev-parse","--short","HEAD",cwd=REPO).stdout.strip()) if channel=="edge" else target
 t=os.path.join(CONFIG,"release.json.tmp")
 with open(t,"w") as f: json.dump({"current":current,"checkout":checkout,"channel":"edge" if channel=="edge" else "release","kind":"edge" if channel=="edge" else "release","origin":ORIGIN},f,separators=(",",":"))
 os.replace(t,os.path.join(CONFIG,"release.json"))
def run(*a,cwd=None,env=None): return subprocess.run(a,cwd=cwd,env=env,text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
def latest(repo):
 tags=[x.strip() for x in run("git","tag","-l","v*",cwd=repo).stdout.splitlines() if TAG.fullmatch(x.strip())]
 return max(tags,key=lambda x:tuple(map(int,TAG.fullmatch(x).groups()))) if tags else None
def args_for_components():
 try:
  with open(os.path.join(CONFIG,"components")) as f: values={k:v for k,v in (line.strip().split("=",1) for line in f if "=" in line)}
  d,n,s=values["dashboard"],values["network"],values["serve"]
 except Exception: raise RuntimeError("invalid component metadata")
 if (d,n,s) not in (("1","1","1"),("1","1","0"),("1","0","1"),("1","0","0"),("0","1","1"),("0","1","0")): raise RuntimeError("invalid component metadata")
 # --agents is a per-run opt-in (not sticky), so an unattended update must
 # restate it or it would silently converge the agents layer off. Malformed
 # values fail loudly like the tuple above — never as a silent "off".
 a=values.get("agents","0")
 if a not in ("0","1"): raise RuntimeError("invalid component metadata")
 return (["--dashboard-only"] if n=="0" else ["--network-only"] if d=="0" else [])+(["--no-serve"] if s=="0" else [])+(["--agents"] if a=="1" else [])
def install(repo, extra, channel):
 env=dict(os.environ,GRAVEDECAY_UPDATER="1",GRAVEDECAY_UPDATE_CHANNEL=("release" if channel=="tag" else channel))
 return run(os.path.join(repo,"macos","install.sh"),"--root",ROOT,*extra,cwd=repo,env=env)
def main():
 stage=backup=None; cut=False; extra=[]; owned=False
 try:
  home=os.path.realpath(os.path.expanduser("~"))
  if ROOT==home or os.path.commonpath((home,ROOT))!=home or not os.path.isfile(os.path.join(ROOT,".gravedecay-macos")) or not all(under(p) for p in (REQUEST,STATUS,LOCK,LOG,REPO,CONFIG)): raise RuntimeError("managed root is not a marked home descendant")
  try: os.mkdir(LOCK); owned=True
  except FileExistsError: return
  try:
   with open(REQUEST) as f: req=json.load(f)
   if set(req)!={"channel","tag","requested_at"} or req["channel"] not in ("release","edge","tag") or not isinstance(req["requested_at"],int) or req["requested_at"] < int(time.time())-86400 or req["requested_at"] > int(time.time())+3600 or (req["channel"]=="tag" and (not isinstance(req["tag"],str) or not TAG.fullmatch(req["tag"]))) or (req["channel"]!="tag" and req["tag"] is not None): raise RuntimeError("invalid update request")
   if not os.path.isfile(os.path.join(ROOT,".gravedecay-macos")) or not os.path.isdir(os.path.join(REPO,".git")): raise RuntimeError("managed root/source is missing")
   if run("git","remote","get-url","origin",cwd=REPO).stdout.strip()!=ORIGIN or run("git","status","--porcelain",cwd=REPO).stdout.strip(): raise RuntimeError("managed source is untrusted or dirty")
   old_meta=open(os.path.join(CONFIG,"release.json")).read() if os.path.exists(os.path.join(CONFIG,"release.json")) else None; extra=args_for_components(); parent=os.path.join(ROOT,"staging"); os.makedirs(parent,exist_ok=True); stage=tempfile.mkdtemp(prefix="update-",dir=parent)
   if not under(stage): raise RuntimeError("unsafe staging path")
   status({"state":"running","channel":req["channel"],"tag":req["tag"]})
   if run("git","clone","--quiet",ORIGIN,stage).returncode: raise RuntimeError("fetch failed")
   target=req["tag"] if req["channel"]=="tag" else (latest(stage) if req["channel"]=="release" else run("git","symbolic-ref","-q","--short","refs/remotes/origin/HEAD",cwd=stage).stdout.strip().removeprefix("origin/"))
   if not target or run("git","checkout","--quiet",target,cwd=stage).returncode: raise RuntimeError("requested release is unavailable")
   backup=os.path.join(parent,"previous-"+str(os.getpid()));
   if not under(backup): raise RuntimeError("unsafe backup path")
   os.rename(REPO,backup); cut=True; os.rename(stage,REPO); stage=None
   result=install(REPO,extra,req["channel"])
   if result.returncode: raise RuntimeError("install/restart failed: "+result.stdout[-800:])
   metadata(req["channel"],target); cut=False; clean(backup); backup=None; status({"state":"ok","channel":req["channel"],"tag":req["tag"],"at":int(time.time())})
  except Exception as exc:
   message=str(exc); restored=False
   if cut and backup and os.path.isdir(backup):
    failed=os.path.join(ROOT,"staging","failed-"+str(os.getpid()))
    try:
     if os.path.exists(REPO) and under(failed): os.rename(REPO,failed)
     os.rename(backup,REPO); backup=None
     old=install(REPO,extra,"release")
     restored=(old.returncode==0)
     if restored:
      if old_meta is not None:
       with open(os.path.join(CONFIG,"release.json.tmp"),"w") as f: f.write(old_meta)
       os.replace(os.path.join(CONFIG,"release.json.tmp"),os.path.join(CONFIG,"release.json"))
      clean(failed)
     message+=("; prior payload restored" if restored else "; rollback installer failed: "+old.stdout[-400:])
    except Exception as rollback: message+="; rollback failed: "+str(rollback)
   status({"state":"failed","message":message[-1000:],"restored":restored,"at":int(time.time())})
  finally:
   clean(stage); clean(backup)
 finally:
  if owned:
   clean(LOCK)
   if under(REQUEST) and os.path.exists(REQUEST): os.unlink(REQUEST)
if __name__=="__main__": main()
