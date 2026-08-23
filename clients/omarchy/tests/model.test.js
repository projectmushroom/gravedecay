const fs = require("fs"), vm = require("vm"), assert = require("assert");
const code = fs.readFileSync(require("path").join(__dirname, "..", "Model.js"), "utf8").replace(".pragma library", "");
const model = {}; vm.createContext(model); vm.runInContext(code, model);
assert.deepStrictEqual(JSON.parse(JSON.stringify(model.candidates({Self:{Online:true,DNSName:"one.ts.net.",ID:"1"},Peer:{a:{Online:true,DNSName:"two.ts.net.",StableID:"2"},b:{Online:false,DNSName:"no.ts.net",ID:"3"}}}))), [{id:"1",dns:"one.ts.net",name:"one.ts.net"},{id:"2",dns:"two.ts.net",name:"two.ts.net"}]);
assert(model.summary('{"product":"gravedecay","api_version":1,"node":{},"resources":{},"activity":{},"health":{},"links":{}}'));
assert.equal(model.summary('{"product":"nope"}'), null);
