```diff

diff --git a/master/buildbot/www/resource.py b/master/buildbot/www/resource.py
index 394b67518..c66c5a2b0 100644
--- a/master/buildbot/www/resource.py
+++ b/master/buildbot/www/resource.py
@@ -23,13 +23,6 @@ from twisted.web.error import Error
 from buildbot.util import unicode2bytes
 
 
-class Redirect(Error):
-
-    def __init__(self, url):
-        Error.__init__(self, 302, "redirect")
-        self.url = url
-
-
 class Resource(resource.Resource):
 
     # if this is true for a class, then instances will have their
@@ -77,13 +70,6 @@ class Resource(resource.Resource):
                 # it (see #2027)
                 log.msg("http client disconnected before results were sent")
 
-        @d.addErrback
-        def failHttpRedirect(f):
-            f.trap(Redirect)
-            request.redirect(f.value.url)
-            request.finish()
-            return None
-
         @d.addErrback
         def failHttpError(f):
             f.trap(Error)
@@ -103,15 +89,3 @@ class Resource(resource.Resource):
                     pass
 
         return server.NOT_DONE_YET
-
-
-class RedirectResource(Resource):
-
-    def __init__(self, master, basepath):
-        Resource.__init__(self, master)
-        self.basepath = basepath
-
-    def render(self, request):
-        redir = self.base_url + self.basepath
-        request.redirect(redir)
-        return redir

```