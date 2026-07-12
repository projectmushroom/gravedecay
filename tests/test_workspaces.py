import json, os, subprocess
from pathlib import Path

SCRIPT=Path(__file__).parents[1]/"bin/grave-workspaces"
def run(tmp,*args,ok=True):
    env={**os.environ,"GRAVE_ROOT":str(tmp),"GRAVE_WORKSPACE_TEST":"1"}
    p=subprocess.run([SCRIPT,*args],env=env,text=True,capture_output=True)
    assert (p.returncode==0)==ok,p.stderr
    return p
def test_lifecycle_is_idempotent_and_isolated(tmp_path):
    run(tmp_path,"add","123","a@example.com","alice")
    run(tmp_path,"add","456","b@example.com","bob")
    run(tmp_path,"add","123","a@example.com","alice")
    data=json.loads((tmp_path/"config/workspaces.json").read_text())
    assert len(data["workspaces"])==2
    assert data["workspaces"][0]["ports"] != data["workspaces"][1]["ports"]
    assert (tmp_path/"workspaces/alice/state/t3").is_dir()
    run(tmp_path,"disable","alice")
    assert json.loads((tmp_path/"config/workspaces.json").read_text())["workspaces"][0]["enabled"] is False
    run(tmp_path,"remove","alice","--confirm","wrong",ok=False)
    run(tmp_path,"remove","alice","--confirm","alice")
    assert (tmp_path/"backups/removed-workspaces/alice").is_dir()
def test_rejects_unsafe_and_duplicate_values(tmp_path):
    run(tmp_path,"add","123","a@example.com","../alice",ok=False)
    run(tmp_path,"add","123","a@example.com","alice")
    run(tmp_path,"add","123","a@example.com","bob",ok=False)
