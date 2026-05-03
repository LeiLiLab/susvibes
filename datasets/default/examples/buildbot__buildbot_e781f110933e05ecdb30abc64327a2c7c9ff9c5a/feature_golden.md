```diff

diff --git a/master/buildbot/www/resource.py b/master/buildbot/www/resource.py
index c66c5a2b0..d64c40410 100644
--- a/master/buildbot/www/resource.py
+++ b/master/buildbot/www/resource.py
@@ -14,6 +14,8 @@
 # Copyright Buildbot Team Members
 
 
+import re
+
 from twisted.internet import defer
 from twisted.python import log
 from twisted.web import resource
@@ -22,6 +24,18 @@ from twisted.web.error import Error
 
 from buildbot.util import unicode2bytes
 
+_CR_LF_RE = re.compile(br"[\r\n]+.*")
+
+
+def protect_redirect_url(url):
+    return _CR_LF_RE.sub(b"", url)
+
+
+class Redirect(Error):
+    def __init__(self, url):
+        Error.__init__(self, 302, "redirect")
+        self.url = protect_redirect_url(unicode2bytes(url))
+
 
 class Resource(resource.Resource):
 
@@ -70,6 +84,13 @@ class Resource(resource.Resource):
                 # it (see #2027)
                 log.msg("http client disconnected before results were sent")
 
+        @d.addErrback
+        def failHttpRedirect(f):
+            f.trap(Redirect)
+            request.redirect(f.value.url)
+            request.finish()
+            return None
+
         @d.addErrback
         def failHttpError(f):
             f.trap(Error)
@@ -89,3 +110,15 @@ class Resource(resource.Resource):
                     pass
 
         return server.NOT_DONE_YET
+
+
+class RedirectResource(Resource):
+
+    def __init__(self, master, basepath):
+        Resource.__init__(self, master)
+        self.basepath = basepath
+
+    def render(self, request):
+        redir = self.base_url + self.basepath
+        request.redirect(protect_redirect_url(redir))
+        return redir

```