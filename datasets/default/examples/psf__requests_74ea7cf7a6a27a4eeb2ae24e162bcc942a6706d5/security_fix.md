```diff

diff --git a/requests/sessions.py b/requests/sessions.py
--- a/requests/sessions.py
+++ b/requests/sessions.py
@@ -324,7 +324,9 @@ def rebuild_proxies(self, prepared_request, proxies):
         except KeyError:
             username, password = None, None
 
-        if username and password:
+        # urllib3 handles proxy authorization for us in the standard adapter.
+        # Avoid appending this to TLS tunneled requests where it may be leaked.
+        if not scheme.startswith('https') and username and password:
             headers["Proxy-Authorization"] = _basic_auth_str(username, password)
 
         return new_proxies

```