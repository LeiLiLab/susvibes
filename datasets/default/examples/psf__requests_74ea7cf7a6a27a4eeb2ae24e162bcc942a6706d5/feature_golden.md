```diff

diff --git a/requests/sessions.py b/requests/sessions.py
index fbdd72ef..dbcf2a7b 100644
--- a/requests/sessions.py
+++ b/requests/sessions.py
@@ -301,7 +301,35 @@ class SessionRedirectMixin:
             prepared_request.prepare_auth(new_auth)
 
     def rebuild_proxies(self, prepared_request, proxies):
-        return proxies
+        """This method re-evaluates the proxy configuration by considering the
+        environment variables. If we are redirected to a URL covered by
+        NO_PROXY, we strip the proxy configuration. Otherwise, we set missing
+        proxy keys for this URL (in case they were stripped by a previous
+        redirect).
+
+        This method also replaces the Proxy-Authorization header where
+        necessary.
+
+        :rtype: dict
+        """
+        headers = prepared_request.headers
+        scheme = urlparse(prepared_request.url).scheme
+        new_proxies = resolve_proxies(prepared_request, proxies, self.trust_env)
+
+        if "Proxy-Authorization" in headers:
+            del headers["Proxy-Authorization"]
+
+        try:
+            username, password = get_auth_from_url(new_proxies[scheme])
+        except KeyError:
+            username, password = None, None
+
+        # urllib3 handles proxy authorization for us in the standard adapter.
+        # Avoid appending this to TLS tunneled requests where it may be leaked.
+        if not scheme.startswith('https') and username and password:
+            headers["Proxy-Authorization"] = _basic_auth_str(username, password)
+
+        return new_proxies
 
     def rebuild_method(self, prepared_request, response):
         """When being redirected we may want to change the method of the request

```