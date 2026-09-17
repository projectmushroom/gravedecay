import copy
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path

GATEWAY=SourceFileLoader('serve_policy',str(Path(__file__).parents[1]/'dashboard/gateway.py')).load_module()

class ServePolicy(unittest.TestCase):
    def setUp(self):
        self.origin='box.tail.ts.net:443'; self.token='x'*64
        self.config={'TCP':{'443':{'HTTPS':True},'3000':{'HTTPS':True}},
                     'Web':{self.origin:{'Handlers':{'/':{'Proxy':f'http://127.0.0.1:4710/_grave_proxy/{self.token}'}}},
                            'box.tail.ts.net:3000':{'Handlers':{'/':{'Proxy':'http://127.0.0.1:3000'}}}}}
    def check(self,config): GATEWAY.check_serve(config,'box.tail.ts.net.',4710,self.token)
    def test_exact_gateway_and_independent_preview_pass(self):
        before=copy.deepcopy(self.config); self.check(self.config); self.assertEqual(self.config,before)
    def test_any_extra_origin_mount_or_wrong_root_fails(self):
        for mount in ('/net','/net/','/net/events','/grave','/term','/unexpected'):
            config=copy.deepcopy(self.config)
            config['Web'][self.origin]['Handlers'][mount]={'Proxy':'http://127.0.0.1:4714'}
            with self.subTest(mount=mount),self.assertRaises(ValueError): self.check(config)
        for target in ('http://127.0.0.1:4710','http://127.0.0.1:4714',
                       'http://127.0.0.1:4710/_grave_proxy/wrong'):
            config=copy.deepcopy(self.config); config['Web'][self.origin]['Handlers']['/']['Proxy']=target
            with self.subTest(target=target),self.assertRaises(ValueError): self.check(config)
    def test_missing_https_funnel_alias_and_foreground_override_fail(self):
        variants=[{}, {'TCP':{'443':{'TCPForward':'127.0.0.1:4714'}}},
                  {'AllowFunnel':{self.origin:True}},
                  {'Foreground':{'session':{'Web':{self.origin:{'Handlers':{'/net':{'Proxy':'http://127.0.0.1:4714'}}}}}}},
                  {'Services':{'svc:other':{'TCP':{'443':{'HTTPS':True}}}}}]
        for change in variants:
            config=copy.deepcopy(self.config)
            if not change: config['Web']={}
            else: config.update(change)
            with self.subTest(change=change),self.assertRaises(ValueError): self.check(config)
        config=copy.deepcopy(self.config); config['Web']['alias.tail.ts.net:443']=config['Web'][self.origin]
        with self.assertRaises(ValueError): self.check(config)

if __name__=='__main__': unittest.main()
