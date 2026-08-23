#!/usr/bin/env python3
"""Build the ttyd frontend exactly as raise.sh does for bare metal."""
import os
import sys

src, out = sys.argv[1], sys.argv[2]
html = open(os.path.join(src, "index.tmpl.html")).read()
for marker, fname in [("/*@XTERM_CSS@*/", "vendor/xterm-5.5.0.css"),
                      ("/*@XTERM_JS@*/", "vendor/xterm-5.5.0.min.js"),
                      ("/*@FIT_JS@*/", "vendor/xterm-addon-fit-0.10.0.min.js"),
                      ("/*@APP_JS@*/", "app.js")]:
    html = html.replace(marker, open(os.path.join(src, fname)).read())
assert "/*@" not in html, "unspliced marker left in term index"
tmp = out + ".tmp"
open(tmp, "w").write(html)
os.replace(tmp, out)
