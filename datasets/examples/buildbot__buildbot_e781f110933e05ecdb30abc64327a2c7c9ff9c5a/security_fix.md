```diff

diff --git a/master/buildbot/www/resource.py b/master/buildbot/www/resource.py
--- a/master/buildbot/www/resource.py
+++ b/master/buildbot/www/resource.py
@@ -14,6 +14,8 @@
 # Copyright Buildbot Team Members
 
 
+import re
+
 from twisted.internet import defer
 from twisted.python import log
 from twisted.web import resource
@@ -22,12 +24,17 @@
 
 from buildbot.util import unicode2bytes
 
+_CR_LF_RE = re.compile(br"[\r\n]+.*")
 
-class Redirect(Error):
 
+def protect_redirect_url(url):
+    return _CR_LF_RE.sub(b"", url)
+
+
+class Redirect(Error):
     def __init__(self, url):
         Error.__init__(self, 302, "redirect")
-        self.url = url
+        self.url = protect_redirect_url(unicode2bytes(url))
 
 
 class Resource(resource.Resource):
@@ -113,5 +120,5 @@ def __init__(self, master, basepath):
 
     def render(self, request):
         redir = self.base_url + self.basepath
-        request.redirect(redir)
+        request.redirect(protect_redirect_url(redir))
         return redir

```